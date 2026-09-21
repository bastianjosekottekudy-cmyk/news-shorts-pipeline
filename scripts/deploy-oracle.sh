#!/usr/bin/env bash
# Automated deployment script for Oracle Cloud Always Free (or any Ubuntu/Debian server)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "=== [1/5] Checking System Dependencies ==="
if ! command -v ffmpeg &>/dev/null || ! command -v python3 &>/dev/null; then
    echo "Installing system dependencies (ffmpeg, python3-venv, git)..."
    sudo apt update
    sudo apt install -y ffmpeg python3-venv python3-pip git
fi

echo "=== [2/5] Setting up Python Virtual Environment ==="
cd "$REPO_ROOT"
if [ ! -d "$REPO_ROOT/.venv" ]; then
    python3 -m venv "$REPO_ROOT/.venv"
fi
"$REPO_ROOT/.venv/bin/pip" install --upgrade pip
"$REPO_ROOT/.venv/bin/pip" install -r "$REPO_ROOT/requirements.txt"

echo "=== [3/5] Setting up Systemd User Service ==="
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
mkdir -p "$SYSTEMD_USER_DIR"
cp "$SCRIPT_DIR/news-shorts.service" "$SYSTEMD_USER_DIR/news-shorts.service"

# Ensure user lingering is enabled so services run 24/7 without active SSH login
loginctl enable-linger "$USER"

systemctl --user daemon-reload
systemctl --user enable news-shorts.service
systemctl --user restart news-shorts.service

echo "=== [4/5] Verifying Service Health ==="
sleep 2
systemctl --user status news-shorts.service --no-pager

echo "=== [5/5] Deployment Complete! ==="
echo "News Shorts Dashboard running at: http://127.0.0.1:8081"
echo "View logs with: journalctl --user -u news-shorts.service -f"
