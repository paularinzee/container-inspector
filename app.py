import docker
import json
import time
import logging
import os
import requests
from datetime import datetime, timedelta
from collections import defaultdict
from threading import Thread
from flask import Flask, jsonify
from anthropic import Anthropic

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

client = Anthropic()
docker_client = None

# --- Config ---
TARGET_CONTAINERS = os.getenv("TARGET_CONTAINERS", "").split(",")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "10"))
LOG_LINES = int(os.getenv("LOG_LINES", "50"))
AUTO_FIX = os.getenv("AUTO_FIX", "true").lower() == "true"
SLACK_WEBHOOK = os.getenv("SLACK_WEBHOOK_URL", "")
MAX_DIAGNOSES = int(os.getenv("MAX_DIAGNOSES_PER_HOUR", "20"))

# --- State tracking ---
diagnosis_history = []
fix_history = defaultdict(list)
last_error_seen = {}
rate_limit_counter = defaultdict(int)
rate_limit_reset = datetime.now() + timedelta(hours=1)

app = Flask(__name__)


def get_docker_client():
    """Lazily initialize Docker client."""
    global docker_client
    if docker_client is None:
        docker_client = docker.from_env()
    return docker_client


def get_container_logs(container_name):
    """Fetch last N lines from a container."""
    try:
        container = get_docker_client().containers.get(container_name)
        logs = container.logs(
            tail=LOG_LINES,
            timestamps=True
        ).decode("utf-8")
        return logs
    except docker.errors.NotFound:
        logger.warning(f"Container '{container_name}' not found. Skipping.")
        return None
    except docker.errors.APIError as e:
        logger.error(f"Docker API error for {container_name}: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error fetching logs for {container_name}: {e}")
        return None


def detect_errors(logs):
    """Check if logs contain error patterns."""
    error_patterns = [
        "error", "exception", "traceback", "failed", "crash",
        "fatal", "panic", "segmentation fault", "out of memory",
        "killed", "oomkiller", "connection refused", "timeout",
        "permission denied", "no such file", "errno"
    ]
    logs_lower = logs.lower()
    found = []
    for pattern in error_patterns:
        if pattern in logs_lower:
            found.append(pattern)
    return found


def is_new_error(container_name, logs):
    """Check if this is a new error or the same one we already diagnosed."""
    log_hash = hash(logs[-200:])  # Hash last 200 chars
    if last_error_seen.get(container_name) == log_hash:
        return False
    last_error_seen[container_name] = log_hash
    return True


def check_rate_limit():
    """Ensure we don't spam Claude with too many requests."""
    global rate_limit_counter, rate_limit_reset

    now = datetime.now()
    if now > rate_limit_reset:
        rate_limit_counter.clear()
        rate_limit_reset = now + timedelta(hours=1)

    total = sum(rate_limit_counter.values())
    if total >= MAX_DIAGNOSES:
        logger.warning(f"Rate limit reached ({total}/{MAX_DIAGNOSES} per hour). Skipping diagnosis.")
        return False
    return True


def diagnose_with_claude(container_name, logs, error_patterns):
    """Send logs to Claude for diagnosis."""
    if not check_rate_limit():
        return None

    rate_limit_counter[container_name] += 1

    prompt = f"""You are a DevOps expert analyzing container logs.

Container: {container_name}
Timestamp: {datetime.now().isoformat()}
Detected patterns: {', '.join(error_patterns)}

Recent logs:
---
{logs}
---

Analyze these logs and respond with ONLY valid JSON (no markdown, no explanation):
{{
    "root_cause": "One sentence explaining exactly what went wrong",
    "severity": "low|medium|high",
    "suggested_fix": "Step-by-step fix the operator should apply",
    "auto_restart_safe": true or false,
    "config_suggestions": ["ENV_VAR=value", "..."],
    "likely_recurring": true or false,
    "estimated_impact": "What breaks if this isn't fixed"
}}
"""

    try:
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=600,
            messages=[
                {"role": "user", "content": prompt}
            ]
        )
        return message.content[0].text
    except Exception as e:
        logger.error(f"Claude API error: {e}")
        return None


def parse_diagnosis(diagnosis_text):
    """Extract JSON from Claude's response."""
    if not diagnosis_text:
        return None
    try:
        start = diagnosis_text.find("{")
        end = diagnosis_text.rfind("}") + 1
        if start >= 0 and end > start:
            json_str = diagnosis_text[start:end]
            return json.loads(json_str)
    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error: {e}")
        logger.debug(f"Raw response: {diagnosis_text}")
    except Exception as e:
        logger.error(f"Failed to parse diagnosis: {e}")
    return None


def apply_fix(container_name, diagnosis):
    """Apply auto-fixes if safe."""
    if not AUTO_FIX:
        logger.info(f"Auto-fix disabled globally. Skipping {container_name}.")
        return False

    if not diagnosis.get("auto_restart_safe"):
        logger.info(f"Claude says restart is unsafe for {container_name}. Skipping.")
        return False

    # Don't restart the same container more than 3 times per hour
    recent_fixes = [
        t for t in fix_history[container_name]
        if t > datetime.now() - timedelta(hours=1)
    ]
    if len(recent_fixes) >= 3:
        logger.warning(
            f"Container {container_name} already restarted {len(recent_fixes)} "
            f"times this hour. Something deeper is wrong. Skipping."
        )
        send_slack_alert(
            container_name, diagnosis,
            extra="REPEATED FAILURE: This container has been restarted 3+ times "
                  "in the last hour. Manual intervention needed."
        )
        return False

    try:
        container = get_docker_client().containers.get(container_name)
        logger.info(f"Restarting container {container_name}...")
        container.restart(timeout=30)
        fix_history[container_name].append(datetime.now())
        logger.info(f"Container {container_name} restarted successfully")

        # Verify it's actually running after restart
        time.sleep(5)
        container.reload()
        if container.status != "running":
            logger.error(f"Container {container_name} failed to start after restart")
            return False

        return True
    except Exception as e:
        logger.error(f"Failed to restart {container_name}: {e}")
        return False


def send_slack_alert(container_name, diagnosis, extra=""):
    """Send diagnosis to Slack."""
    if not SLACK_WEBHOOK:
        return

    severity_emoji = {
        "low": "🟡",
        "medium": "🟠",
        "high": "🔴"
    }

    severity = diagnosis.get("severity", "unknown")
    emoji = severity_emoji.get(severity, "⚪")

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{emoji} Container Doctor Alert: {container_name}"
            }
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Severity:* {severity}"},
                {"type": "mrkdwn", "text": f"*Container:* `{container_name}`"},
                {"type": "mrkdwn", "text": f"*Root Cause:* {diagnosis.get('root_cause', 'Unknown')}"},
                {"type": "mrkdwn", "text": f"*Fix:* {diagnosis.get('suggested_fix', 'N/A')}"},
            ]
        }
    ]

    if diagnosis.get("config_suggestions"):
        suggestions = "\n".join(
            f"• `{s}`" for s in diagnosis["config_suggestions"]
        )
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Config Suggestions:*\n{suggestions}"
            }
        })

    if extra:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*⚠️ {extra}*"}
        })

    try:
        requests.post(SLACK_WEBHOOK, json={"blocks": blocks}, timeout=10)
    except Exception as e:
        logger.error(f"Slack notification failed: {e}")


# --- Health Check Endpoint ---
@app.route("/health")
def health():
    """Health check endpoint for the doctor itself."""
    try:
        get_docker_client().ping()
        docker_ok = True
    except:
        docker_ok = False

    return jsonify({
        "status": "healthy" if docker_ok else "degraded",
        "docker_connected": docker_ok,
        "monitoring": TARGET_CONTAINERS,
        "total_diagnoses": len(diagnosis_history),
        "fixes_applied": {k: len(v) for k, v in fix_history.items()},
        "rate_limit_remaining": MAX_DIAGNOSES - sum(rate_limit_counter.values()),
        "uptime_check": datetime.now().isoformat()
    })


@app.route("/history")
def history():
    """Return recent diagnosis history."""
    return jsonify(diagnosis_history[-50:])


def monitor_containers():
    """Main monitoring loop."""
    logger.info(f"Container Doctor starting up")
    logger.info(f"Monitoring: {TARGET_CONTAINERS}")
    logger.info(f"Check interval: {CHECK_INTERVAL}s")
    logger.info(f"Auto-fix: {AUTO_FIX}")
    logger.info(f"Rate limit: {MAX_DIAGNOSES}/hour")

    while True:
        for container_name in TARGET_CONTAINERS:
            container_name = container_name.strip()
            if not container_name:
                continue

            logs = get_container_logs(container_name)
            if not logs:
                continue

            error_patterns = detect_errors(logs)
            if not error_patterns:
                continue

            # Skip if we already diagnosed this exact error
            if not is_new_error(container_name, logs):
                continue

            logger.warning(
                f"Errors detected in {container_name}: {error_patterns}"
            )

            diagnosis_text = diagnose_with_claude(
                container_name, logs, error_patterns
            )
            if not diagnosis_text:
                continue

            diagnosis = parse_diagnosis(diagnosis_text)
            if not diagnosis:
                logger.error("Failed to parse Claude's response. Skipping.")
                continue

            # Record it
            diagnosis_history.append({
                "container": container_name,
                "timestamp": datetime.now().isoformat(),
                "diagnosis": diagnosis,
                "patterns": error_patterns
            })

            logger.info(
                f"Diagnosis for {container_name}: "
                f"severity={diagnosis.get('severity')}, "
                f"cause={diagnosis.get('root_cause')}"
            )

            # Auto-fix only on high severity
            fixed = False
            if diagnosis.get("severity") == "high":
                fixed = apply_fix(container_name, diagnosis)

            # Always notify Slack
            send_slack_alert(
                container_name, diagnosis,
                extra="Auto-restarted" if fixed else ""
            )

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    # Run Flask health endpoint in background
    flask_thread = Thread(
        target=lambda: app.run(host="0.0.0.0", port=8080, debug=False),
        daemon=True
    )
    flask_thread.start()
    logger.info("Health endpoint running on :8080")

    try:
        monitor_containers()
    except KeyboardInterrupt:
        logger.info("Container Doctor shutting down")