#!/usr/bin/env bash
# Linux equivalent of run_nightly.ps1 / the Windows Scheduled Task.
# Point cron (or systemd timer) at this to run the nightly scan.

set -uo pipefail

SCRIPT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="$SCRIPT_ROOT/data"
# CV directory: set JOB_SEARCH_CV_DIR or default to <repo>/cvs
CV_DIR="${JOB_SEARCH_CV_DIR:-$SCRIPT_ROOT/cvs}"
LOG_FILE="$DATA_DIR/nightly_wrapper.log"
ENV_FILE="$SCRIPT_ROOT/.env.nightly"
LOCK_FILE="$DATA_DIR/.nightly.lock"

log() {
    printf '%s - %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >> "$LOG_FILE"
}

mkdir -p "$DATA_DIR"
exec 200>"$LOCK_FILE"
flock -n 200 || { log "ABORT: previous run still in progress (lock held)."; exit 1; }

if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
else
    log "WARNING: $ENV_FILE not found, failure notifications will be skipped."
fi

send_failure_email() {
    local subject="$1"
    local body="$2"
    [ -n "${JOB_SEARCH_SMTP_USER:-}" ] && [ -n "${JOB_SEARCH_SMTP_PASS:-}" ] || return 0
    python3 - "$subject" "$body" <<'PYEOF'
import sys, smtplib, os
from email.mime.text import MIMEText
subject, body = sys.argv[1], sys.argv[2]
user = os.environ["JOB_SEARCH_SMTP_USER"]
pw = os.environ["JOB_SEARCH_SMTP_PASS"]
msg = MIMEText(body)
msg["Subject"] = subject
msg["From"] = user
msg["To"] = user
try:
    s = smtplib.SMTP("smtp.gmail.com", 587, timeout=15)
    s.starttls()
    s.login(user, pw)
    s.sendmail(user, user, msg.as_string())
    s.quit()
except Exception as e:
    print(f"failure email send failed: {e}", file=sys.stderr)
PYEOF
}

log "Starting nightly scan..."

# ---- Docker readiness gate (systemd-managed on Linux, should already be up) ----
docker_ready=false
for _ in $(seq 1 10); do
    if docker info >/dev/null 2>&1; then
        docker_ready=true
        break
    fi
    sleep 15
done

if [ "$docker_ready" != "true" ]; then
    log "ABORT: Docker engine not responding."
    send_failure_email "Job Search: SCAN FAILED" "Docker engine did not respond. Nightly scan did not run."
    exit 1
fi

# ---- Image presence check ----
if [ -z "$(docker images -q job-search-mcp:latest 2>/dev/null)" ]; then
    log "ABORT: image job-search-mcp:latest not found. Rebuild with: docker build -t job-search-mcp ."
    send_failure_email "Job Search: SCAN FAILED" "Docker image job-search-mcp:latest is missing. Rebuild required."
    exit 1
fi

# ---- Run the scan (up to 3 attempts) ----
attempt=1
max_attempts=3
exit_code=1
while [ "$attempt" -le "$max_attempts" ]; do
    log "Scan attempt $attempt/$max_attempts..."
    timeout 1800 docker run --rm \
        -v "$DATA_DIR:/data" \
        -v "$CV_DIR:/cvs:ro" \
        -v "$SCRIPT_ROOT/config.json:/app/config.json" \
        -v "$SCRIPT_ROOT/run_scan.py:/app/run_scan.py" \
        -e "BROWSER_HEADLESS=true" \
        -e "SMTP_HOST=smtp.gmail.com" \
        -e "SMTP_PORT=587" \
        -e "SMTP_USER=$JOB_SEARCH_SMTP_USER" \
        -e "SMTP_PASS=$JOB_SEARCH_SMTP_PASS" \
        -e "NOTIFY_TO=$JOB_SEARCH_SMTP_USER" \
        --entrypoint python \
        job-search-mcp:latest \
        run_scan.py >> "$LOG_FILE" 2>&1

    exit_code=$?
    if [ "$exit_code" -eq 0 ]; then
        break
    fi
    log "Attempt $attempt failed with exit code $exit_code."
    attempt=$((attempt + 1))
    [ "$attempt" -le "$max_attempts" ] && sleep 300
done

if [ "$exit_code" -eq 0 ]; then
    log "Scan completed successfully."
else
    log "Scan failed after $max_attempts attempts (exit code $exit_code)."
    send_failure_email "Job Search: SCAN FAILED" "run_scan.py failed after $max_attempts attempts. Exit code $exit_code. Check $LOG_FILE."
fi

exit "$exit_code"
