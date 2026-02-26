#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="vpn-telebot"
VENV_DIR="$PROJECT_DIR/.venv"
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

echo "=== VPN TeleBot uninstall ==="
echo "Project dir: $PROJECT_DIR"

ask_yes_no() {
  local prompt="$1"
  local default="${2:-n}"
  local answer
  local suffix="[y/N]"
  if [[ "$default" == "y" ]]; then
    suffix="[Y/n]"
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

if command -v systemctl >/dev/null 2>&1; then
  if systemctl list-unit-files | grep -q "^${SERVICE_NAME}.service"; then
    echo "Stopping and disabling ${SERVICE_NAME}.service ..."
    systemctl stop "$SERVICE_NAME" || true
    systemctl disable "$SERVICE_NAME" || true
  fi

  if [[ -f "$UNIT_FILE" ]]; then
    echo "Removing $UNIT_FILE ..."
    rm -f "$UNIT_FILE"
    systemctl daemon-reload
  fi
fi

if [[ -d "$VENV_DIR" ]] && ask_yes_no "Remove virtualenv directory ($VENV_DIR)?" "y"; then
  rm -rf "$VENV_DIR"
fi

if ask_yes_no "Remove project directory ($PROJECT_DIR)? WARNING: this can include local DB files." "n"; then
  rm -rf "$PROJECT_DIR"
  echo "Project directory removed."
else
  echo "Project directory kept."
fi

echo "Uninstall complete."
