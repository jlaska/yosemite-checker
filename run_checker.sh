#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$SCRIPT_DIR/config.json"
CHECKER="$SCRIPT_DIR/yosemite_checker_v2.py"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

send_pushover() {
    local user_key="$1" api_token="$2" title="$3" message="$4" priority="${5:-0}"
    curl -s \
        --form-string "token=$api_token" \
        --form-string "user=$user_key" \
        --form-string "title=$title" \
        --form-string "message=$message" \
        --form-string "priority=$priority" \
        --form-string "sound=siren" \
        https://api.pushover.net/1/messages.json > /dev/null
}

main() {
    log "Starting Yosemite availability check"

    local user_key api_token
    user_key=$(python3 -c "import json; print(json.load(open('$CONFIG'))['pushover']['user_key'])")
    api_token=$(python3 -c "import json; print(json.load(open('$CONFIG'))['pushover']['api_token'])")

    if [[ -z "$user_key" || -z "$api_token" ]]; then
        log "ERROR: Pushover credentials not set in config.json"
        exit 1
    fi

    local output
    local exit_code=0
    # Capture stdout (JSON) only; stderr (progress logs) flows through to the log file
    local headless_flag=""
    [[ "${HEADLESS:-1}" == "0" ]] && headless_flag="--no-headless"
    output=$(uv run "$CHECKER" --config "$CONFIG" -o json $headless_flag) || exit_code=$?

    if [[ $exit_code -eq 0 ]]; then
        log "Availability found!"

        # Build a human-readable summary from the JSON output
        local summary
        summary=$(echo "$output" | python3 -c "
import json, sys
data = json.load(sys.stdin)
lines = []
for r in data.get('results', []):
    lines.append(f\"{r['property']} — {r['room_type']} — {r['price']} ({r['dates']})\")
total = data.get('total_rooms', 0)
lines.append(f\"\nTotal: {total} room(s) available\")
print('\n'.join(lines))
")
        log "$summary"
        send_pushover "$user_key" "$api_token" \
            "🏕 Yosemite Availability!" \
            "$summary" 1
        log "Pushover notification sent"
    elif [[ $exit_code -eq 1 ]]; then
        log "No availability found"
    else
        log "ERROR: checker exited with code $exit_code"
        log "$output"
        send_pushover "$user_key" "$api_token" \
            "⚠ Yosemite Checker Error" \
            "Exit code $exit_code. Check logs for details."
    fi
}

main "$@"
