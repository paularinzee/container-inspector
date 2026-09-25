# 🩺 Container Doctor

**Container Doctor** is An automated DevOps companion that monitors Docker container logs, leverages the Anthropic Claude API to diagnose errors in real time, applies safe automated fixes (restarts), and broadcasts structured alerts to Slack. It also includes an embedded Flask API for health checks and audit trails.

---

## 🛠 Features

- **Automated Log Monitoring**: Periodically scans target container logs for common error patterns (e.g., exceptions, tracebacks, OOM kills, timeouts, connection refused).  
- **AI-Powered Diagnostics**: Uses Claude (claude-sonnet-4-20250514) to analyze logs and return structured JSON containing the root cause, severity level, step-by-step fix, configuration suggestions, and restart safety flags.
- **Smart Auto-Fixing**: Automatically restarts high-severity containers only if Claude marks the action as safe and safety thresholds are met (prevents infinite restart loops by capping restarts at 3 per hour).  
- **Rich Slack Integration**: Dispatches color-coded, block-formatted alerts to Slack via webhook detailing severity, root cause, and suggested remediation steps.  
- **Built-in Rate Limiting**: Protects API budgets by enforcing a maximum number of diagnoses per hour.  
- **Health & History API**: Exposes endpoints to check the operational health of the monitoring service and review recent diagnostic history.  
 

---

## API Endpoints

The built-in Flask server runs on port 8080:
- GET /health
Returns the current health status, Docker connection state, monitored containers, total diagnoses performed, active rate limits, and fix counts.

- GET /history
Returns a JSON array of the last 50 recorded diagnoses.

---


## 🚀 Getting Started

### Prerequisites

- Python 3.9+
- Docker installed and running locally with accessible socket permissions.
- An active Anthropic API key set in your environment (ANTHROPIC_API_KEY).
---

### Running the Container

1. Set your environment variables:

```bash
ANTHROPIC_API_KEY=sk-ant-...
TARGET_CONTAINERS="my-web-app,my-database-worker"
CHECK_INTERVAL=10
LOG_LINES=50
AUTO_FIX=true
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/YOUR/WEBHOOK/URL
MAX_DIAGNOSES_PER_HOUR=20
```

2. Start the service in detached mode:

```bash
docker compose up -d
```
## Author

[Paul Nnaji](https://github.com/paularinzee)

## License

[MIT](./LICENSE)
