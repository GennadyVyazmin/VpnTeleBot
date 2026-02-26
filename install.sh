#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
SERVICE_NAME="vpn-telebot"
ENV_FILE="$PROJECT_DIR/.env"

echo "=== VPN TeleBot installer ==="
echo "Project dir: $PROJECT_DIR"
echo

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

ask_non_empty() {
  local prompt="$1"
  local value=""
  while [[ -z "$value" ]]; do
    read -r -p "$prompt: " value
  done
  printf "%s" "$value"
}

ask_numeric() {
  local prompt="$1"
  local value=""
  while true; do
    read -r -p "$prompt: " value
    if [[ "$value" =~ ^[0-9]+$ ]]; then
      printf "%s" "$value"
      return 0
    fi
    echo "Value must be numeric."
  done
}

if ask_yes_no "Install system packages (python3, venv, pip, sqlite3)?" "y"; then
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv python3-pip sqlite3
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y python3 python3-pip python3-virtualenv sqlite
  elif command -v yum >/dev/null 2>&1; then
    yum install -y python3 python3-pip
  else
    echo "No supported package manager found. Install Python dependencies manually."
  fi
fi

if [[ -f "$ENV_FILE" ]]; then
  if ask_yes_no ".env already exists. Overwrite it?" "n"; then
    cp "$ENV_FILE" "$ENV_FILE.bak.$(date +%Y%m%d_%H%M%S)"
  else
    echo "Keeping existing .env"
  fi
fi

if [[ ! -f "$ENV_FILE" ]] || ask_yes_no "Write/update TELEGRAM_BOT_TOKEN and SUPER_ADMIN_ID in .env?" "y"; then
  BOT_TOKEN="$(ask_non_empty "Enter TELEGRAM_BOT_TOKEN")"
  SUPER_ADMIN_ID="$(ask_numeric "Enter SUPER_ADMIN_ID")"

  cat > "$ENV_FILE" <<EOF
TELEGRAM_BOT_TOKEN=$BOT_TOKEN
SUPER_ADMIN_ID=$SUPER_ADMIN_ID
EOF
  chmod 600 "$ENV_FILE"
  echo ".env saved: $ENV_FILE"
fi

mkdir -p "$PROJECT_DIR/bacup_database"

python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$PROJECT_DIR/requirements.txt"

echo
if command -v systemctl >/dev/null 2>&1; then
  UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
  cat > "$UNIT_FILE" <<EOF
[Unit]
Description=VPN TeleBot
After=network.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$VENV_DIR/bin/python $PROJECT_DIR/main.py
Restart=always
RestartSec=3
User=root

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable "$SERVICE_NAME"

  if ask_yes_no "Start/restart service now?" "y"; then
    systemctl restart "$SERVICE_NAME"
    systemctl --no-pager --full status "$SERVICE_NAME" || true
  fi
else
  echo "systemd not found. Run manually:"
  echo "  $VENV_DIR/bin/python $PROJECT_DIR/main.py"
fi

echo
echo "Install complete."
