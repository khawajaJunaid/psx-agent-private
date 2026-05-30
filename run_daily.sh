#!/usr/bin/env bash
# Daily scout runner — sleeps SCOUT_DELAY_MINUTES after cron fires, then runs scout.py --auto.
# Cron fires at 04:30 UTC (= 09:30 PKT, Mon–Fri); default delay is 15 minutes.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"

DELAY=${SCOUT_DELAY_MINUTES:-15}

echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] Sleeping ${DELAY}m before scout (PSX opens in ~${DELAY}m)..."
sleep $((DELAY * 60))

cd "$SCRIPT_DIR"

# Load .env if present (ANTHROPIC_API_KEY etc.)
if [[ -f "$SCRIPT_DIR/.env" ]]; then
    set -o allexport
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/.env"
    set +o allexport
fi

echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] Running: python3 scout.py --auto"
python3 "$SCRIPT_DIR/scout.py" --auto
EXIT_CODE=$?
echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] scout.py --auto exited with code $EXIT_CODE"
exit $EXIT_CODE
