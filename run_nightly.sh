#!/usr/bin/env bash
# Linux equivalent of run_nightly.ps1 / the Windows Scheduled Task.
# Point cron or the systemd timer in systemd/ at this to run the nightly scan.

set -uo pipefail

SCRIPT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="$SCRIPT_ROOT/data"
# CV directory: set JOB_SEARCH_CV_DIR or default to <repo>/cvs
CV_DIR="${JOB_SEARCH_CV_DIR:-$SCRIPT_ROOT/cvs}"
LOG_FILE="$DATA_DIR/nightly_wrapper.log"
ENV_FILE="$SCRIPT_ROOT/.env.nightly"
LOCK_FILE="$DATA_DIR/.nightly.lock"

mkdir -p "$DATA_DIR"

log() {
    printf '%s - %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >> "$LOG_FILE"
}

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
# .env.nightly may set JOB_SEARCH_CV_DIR, so resolve the CV directory again.
CV_DIR="${JOB_SEARCH_CV_DIR:-$CV_DIR}"

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

# Optional hook: if data/sync_applied_jobslist.sh exists, it is run before and
# after the scan to merge previously_applied.json with copies kept by other
# agents or machines. Failures are logged and emailed but never abort the run.
sync_applied() {
    local phase="$1" sync_code
    [ -x "$DATA_DIR/sync_applied_jobslist.sh" ] || return 0
    log "Syncing applied-jobs list ($phase)..."
    if "$DATA_DIR/sync_applied_jobslist.sh" >> "$LOG_FILE" 2>&1; then
        log "Sync completed ($phase)."
    else
        sync_code=$?
        log "Sync failed ($phase, exit code $sync_code)."
        send_failure_email "Job Search: SYNC FAILED ($phase)" "sync_applied_jobslist.sh failed with exit code $sync_code during the $phase sync. Remote copies were left untouched if the failure happened before the push. Check $LOG_FILE."
    fi
}

log "Starting nightly scan..."

# ---- Pre-scan sync: pull in what other agents applied to on their earlier runs ----
# so this scan skips those companies. If it fails, the scan still runs on the local list.
sync_applied pre-scan

# ---- Engine selection + readiness gate ----
# This runs unattended from cron, so it must not depend on a desktop login.
# The native system daemon (default context) survives logout and reboot and is
# preferred. Docker Desktop (desktop-linux) only lives inside a graphical
# session, so it is a fallback for when the scan happens to run while logged in.
# ~/.docker/config.json pins currentContext to desktop-linux, which is why the
# context is set explicitly here rather than inherited.
pick_engine() {
    local ctx
    for ctx in default desktop-linux; do
        if DOCKER_CONTEXT="$ctx" docker info >/dev/null 2>&1; then
            export DOCKER_CONTEXT="$ctx"
            if [ "$ctx" = "default" ]; then
                log "Engine: native daemon (default context)."
            else
                log "Engine: Docker Desktop (desktop-linux) - tied to an active login."
            fi
            return 0
        fi
    done
    return 1
}

docker_ready=false
for _ in $(seq 1 10); do
    if pick_engine; then
        docker_ready=true
        break
    fi
    sleep 15
done

if [ "$docker_ready" != "true" ]; then
    log "ABORT: no Docker engine responded (tried default, desktop-linux)."
    send_failure_email "Job Search: SCAN FAILED" \
        "No Docker engine responded on either the default or desktop-linux context. If Docker Desktop is the only engine installed, it does not run while logged out - install docker-ce for an always-on daemon."
    exit 1
fi

# ---- Image presence check ----
if [ -z "$(docker images -q job-search-mcp:latest 2>/dev/null)" ]; then
    log "ABORT: image job-search-mcp:latest not found in context ${DOCKER_CONTEXT}. Each engine keeps its own image store; rebuild with: DOCKER_CONTEXT=${DOCKER_CONTEXT} docker build -t job-search-mcp $SCRIPT_ROOT"
    send_failure_email "Job Search: SCAN FAILED" "Docker image job-search-mcp:latest is missing. Rebuild required."
    exit 1
fi

# ---- Run the scan (up to 3 attempts) ----
attempt=1
max_attempts=3
exit_code=1
while [ "$attempt" -le "$max_attempts" ]; do
    log "Scan attempt $attempt/$max_attempts..."
    timeout 3600 docker run --rm \
        -v "$DATA_DIR:/data" \
        -v "$CV_DIR:/cvs:ro" \
        -v "$SCRIPT_ROOT/config.json:/app/config.json:ro" \
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

# ---- LinkedIn Easy Apply queue: find up to 10 jobs for review, submit nothing ----
# Runs after the scan so it skips anything the scan just applied to or queued.
# Submission happens later via /linkedin-apply. Failure is logged, never fatal.
log "Building LinkedIn Easy Apply queue..."
if li_out="$(python3 "$SCRIPT_ROOT/linkedin_queue.py" build 2>&1)"; then
    printf '%s\n' "$li_out" >> "$LOG_FILE"
    li_new="$(printf '%s\n' "$li_out" | sed -n 's/^NEW=//p')"
    log "LinkedIn queue: ${li_new:-0} new."
    if [ "${li_new:-0}" -gt 0 ]; then
        send_failure_email "Job Search: ${li_new} LinkedIn jobs queued for review" \
            "$(printf '%s\n\nRun /linkedin-apply in Claude Code to review and submit.' "$li_out")"
    fi
else
    printf '%s\n' "$li_out" >> "$LOG_FILE"
    log "LinkedIn queue build failed."
fi

# ---- Post-scan sync: push tonight's local applications out to the other copies ----
# Runs whether or not the scan succeeded: a partial scan may still have logged
# applications. A sync failure is reported but does not change the scan's exit code.
sync_applied post-scan

exit "$exit_code"
