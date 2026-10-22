#!/usr/bin/env bash
# Launches the job-search MCP server (stdio) for Claude Code.
# Sources SMTP creds from .env.nightly - the single source shared with run_nightly.sh -
# and maps JOB_SEARCH_SMTP_* to the SMTP_* names the container reads.

set -euo pipefail

SCRIPT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_ROOT/.env.nightly"

if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
else
    echo "mcp_launch.sh: $ENV_FILE missing - email fallback will fail" >&2
fi

exec docker run --rm -i \
    -v "$SCRIPT_ROOT/data:/data" \
    -v "${JOB_SEARCH_CV_DIR:-$SCRIPT_ROOT/cvs}:/cvs:ro" \
    -v "$SCRIPT_ROOT/config.json:/app/config.json:ro" \
    -e "SMTP_HOST=smtp.gmail.com" \
    -e "SMTP_PORT=587" \
    -e "SMTP_USER=${JOB_SEARCH_SMTP_USER:-}" \
    -e "SMTP_PASS=${JOB_SEARCH_SMTP_PASS:-}" \
    -e "NOTIFY_TO=${JOB_SEARCH_SMTP_USER:-}" \
    job-search-mcp:latest
