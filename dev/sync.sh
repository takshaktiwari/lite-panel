#!/usr/bin/env bash
# dev/sync.sh — rsync local checkout → test server, then restart the panel
#
# Usage (from repo root):
#   bash dev/sync.sh [--no-restart] [--watch]
#
# Configuration:
#   Reads IP and key path from server-details.md at the repo root.
#   Expected format (one key: value per line):
#     ip: <server-ip>
#     key in folder: <keyfile.pem>   (relative to repo root)
#
# The SSH user is always 'ubuntu' (see PROJECT.md — server-details.md says
# 'root' but that is wrong; ubuntu has passwordless sudo).
#
# Flags:
#   --no-restart   sync files but do not restart the systemd units
#   --watch        keep running; re-sync whenever a local file changes
#                  (requires `fswatch` on macOS: brew install fswatch)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DETAILS="$REPO_ROOT/server-details.md"
KEY_DEFAULT="$REPO_ROOT/test-server-key.pem"

# ---------------------------------------------------------------------------
# Parse server-details.md
# ---------------------------------------------------------------------------
if [[ ! -f "$DETAILS" ]]; then
  echo "error: $DETAILS not found — create it with 'ip: <addr>' on a line." >&2
  exit 1
fi

SERVER_IP=$(grep -i '^ip:' "$DETAILS" | head -1 | sed 's/^ip:[[:space:]]*//')
KEY_NAME=$(grep -i '^key in folder:' "$DETAILS" | head -1 | sed 's/^key in folder:[[:space:]]*//')

[[ -n "$SERVER_IP" ]] || { echo "error: 'ip:' line not found in server-details.md" >&2; exit 1; }
[[ -n "$KEY_NAME"  ]] && KEY_FILE="$REPO_ROOT/$KEY_NAME" || KEY_FILE="$KEY_DEFAULT"

SSH_USER="ubuntu"   # see PROJECT.md — server-details.md incorrectly says 'root'
SSH_OPTS="-i $KEY_FILE -o StrictHostKeyChecking=no -o ConnectTimeout=10"
REMOTE="${SSH_USER}@${SERVER_IP}"
REMOTE_DIR="/opt/lite-panel"

# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------
DO_RESTART=true
WATCH_MODE=false
for arg in "$@"; do
  case "$arg" in
    --no-restart) DO_RESTART=false ;;
    --watch)      WATCH_MODE=true ;;
    -h|--help)
      sed -n '2,/^set /p' "$0" | grep '^#' | sed 's/^# \?//'
      exit 0 ;;
    *) echo "Unknown flag: $arg" >&2; exit 1 ;;
  esac
done

# ---------------------------------------------------------------------------
# rsync function
# ---------------------------------------------------------------------------
do_sync() {
  echo "[$(date +%T)] syncing → $REMOTE:$REMOTE_DIR"
  # --rsync-path="sudo rsync": /opt is root-owned; the ubuntu user needs sudo
  # on the remote side to write there. passwordless sudo is already configured
  # on the test server.
  rsync -az --delete \
    --rsync-path="sudo rsync" \
    --exclude='.git/' \
    --exclude='.venv/' \
    --exclude='venv/' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='dev.db' \
    --exclude='*.pem' \
    --exclude='server-details.md' \
    -e "ssh $SSH_OPTS" \
    "$REPO_ROOT/" "$REMOTE:$REMOTE_DIR/"
  echo "[$(date +%T)] sync done"
}

# ---------------------------------------------------------------------------
# restart function
# ---------------------------------------------------------------------------
do_restart() {
  echo "[$(date +%T)] restarting services on $SERVER_IP"
  # shellcheck disable=SC2029
  ssh $SSH_OPTS "$REMOTE" \
    "sudo systemctl restart lite-panel.service lite-panel-adminer.service && echo 'restarted ok'"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
[[ -f "$KEY_FILE" ]] || { echo "error: key file not found: $KEY_FILE" >&2; exit 1; }
chmod 600 "$KEY_FILE"

if [[ "$WATCH_MODE" == true ]]; then
  if ! command -v fswatch &>/dev/null; then
    echo "error: --watch requires fswatch (brew install fswatch on macOS)" >&2
    exit 1
  fi
  echo "Watching for changes... (Ctrl-C to stop)"
  do_sync
  [[ "$DO_RESTART" == true ]] && do_restart
  fswatch -0 --exclude='\.git' --exclude='__pycache__' --exclude='\.pyc$' \
              --exclude='dev\.db' "$REPO_ROOT" \
  | while IFS= read -r -d '' _event; do
      do_sync
      [[ "$DO_RESTART" == true ]] && do_restart
    done
else
  do_sync
  [[ "$DO_RESTART" == true ]] && do_restart
fi
