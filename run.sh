#!/usr/bin/env bash
# Sets up the venv if needed, starts the web UI, and opens it in your browser
# once it's actually up. Ctrl+C stops the server.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

HOST="${EMAIL_FINDER_HOST:-127.0.0.1}"
PORT="${EMAIL_FINDER_PORT:-5000}"
URL="http://${HOST}:${PORT}"

if [ ! -d .venv ]; then
    echo "Setting up virtual environment (first run only)..."
    python3 -m venv .venv
    # shellcheck disable=SC1091
    source .venv/bin/activate
    pip install -q -r requirements.txt
else
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi

# Open the browser in the background once the server is actually accepting
# connections, without blocking the server from starting in the foreground.
(
    for _ in $(seq 1 30); do
        if curl -s -o /dev/null "$URL" 2>/dev/null; then
            if command -v xdg-open >/dev/null 2>&1; then
                xdg-open "$URL" >/dev/null 2>&1 &
            elif command -v open >/dev/null 2>&1; then
                open "$URL" >/dev/null 2>&1 &
            else
                echo "Open this in your browser: $URL"
            fi
            break
        fi
        sleep 0.5
    done
) &

exec python3 app.py
