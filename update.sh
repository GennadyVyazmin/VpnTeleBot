#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
SERVICE_NAME="vpn-telebot"
SAFE_DIR="$PROJECT_DIR/.db_safe"
REPO_BRANCH="${REPO_BRANCH:-master}"

cd "$PROJECT_DIR"

echo "=== VPN TeleBot update ==="
echo "Project dir: $PROJECT_DIR"

ask_yes_no() {
  local prompt="$1"
  local default="${2:-y}"
  local answer
  local suffix="[Y/n]"
  if [[ "$default" == "n" ]]; then
    suffix="[y/N]"
  fi

  while true; do
    read -r -p "$prompt $suffix: " answer
    answer="${answer:-$default}"
    case "${answer,,}" in
      y|yes) return 0 ;;
      n|no) return 1 ;;
      *) echo "Please answer yes or no." ;;
    esac
  done
}

mkdir -p "$SAFE_DIR"

echo "Saving DB files to $SAFE_DIR ..."
cp -a users.db users.db-shm users.db-wal "$SAFE_DIR"/ 2>/dev/null || true

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "Updating git repository from branch: $REPO_BRANCH ..."
  git fetch origin
  git pull --ff-only origin "$REPO_BRANCH"
else
  echo "Warning: not a git repository, skipping git pull."
fi

echo "Restoring DB files after update..."
cp -a "$SAFE_DIR"/users.db* "$PROJECT_DIR"/ 2>/dev/null || true

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  echo "Virtualenv not found, creating..."
  python3 -m venv "$VENV_DIR"
fi

echo "Updating Python dependencies..."
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$PROJECT_DIR/requirements.txt"

if command -v systemctl >/dev/null 2>&1; then
  if systemctl list-unit-files | grep -q "^${SERVICE_NAME}.service"; then
    echo "Restarting ${SERVICE_NAME}.service ..."
    systemctl daemon-reload
    systemctl restart "$SERVICE_NAME"
    systemctl --no-pager --full status "$SERVICE_NAME" || true
  else
    echo "Service ${SERVICE_NAME}.service not found. Start manually:"
    echo "  $VENV_DIR/bin/python $PROJECT_DIR/main.py"
  fi
else
  echo "systemd not found. Start manually:"
  echo "  $VENV_DIR/bin/python $PROJECT_DIR/main.py"
fi

if ask_yes_no "Delete temporary DB safe copy folder ($SAFE_DIR)?" "y"; then
  rm -rf "$SAFE_DIR"
fi

echo "Update complete."
