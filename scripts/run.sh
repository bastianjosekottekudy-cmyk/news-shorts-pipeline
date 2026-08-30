#!/usr/bin/env bash
# Start the News Shorts Pipeline dashboard and scheduler on Linux
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"

if [ ! -f "$REPO_ROOT/.venv/bin/python" ]; then
    echo "Virtual environment not found. Initializing..."
    python3 -m venv "$REPO_ROOT/.venv"
    "$REPO_ROOT/.venv/bin/pip" install -r "$REPO_ROOT/requirements.txt"
fi

echo "Starting News Shorts Pipeline..."
echo "Dashboard: http://127.0.0.1:8081"
exec "$REPO_ROOT/.venv/bin/python" -m src.main
