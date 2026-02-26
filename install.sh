#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="vpn-telebot"
REPO_OWNER="${REPO_OWNER:-GennadyVyazmin}"
REPO_NAME="${REPO_NAME:-VpnTeleBot}"
REPO_BRANCH="${REPO_BRANCH:-master}"
REPO_TARBALL_URL="https://codeload.github.com/${REPO_OWNER}/${REPO_NAME}/tar.gz/refs/heads/${REPO_BRANCH}"
DEFAULT_INSTALL_DIR="/opt/VpnTeleBot"

echo "=== VPN TeleBot installer ==="
echo "Bootstrap mode: this script can install from scratch."
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

ask_with_default() {
  local prompt="$1"
  local default="$2"
  local value
  read -r -p "$prompt [$default]: " value
  printf "%s" "${value:-$default}"
}

validate_install_dir() {
  local dir="$1"
  if [[ "$dir" != /* ]]; then
    echo "Install directory must be an absolute path: $dir"
    return 1
  fi
  if [[ "$dir" =~ [[:space:]] ]]; then
    echo "Install directory must not contain spaces: $dir"
    return 1
  fi
  return 0
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

read_env_value() {
  local key="$1"
  local file="$2"
  grep -E "^${key}=" "$file" 2>/dev/null | tail -n 1 | cut -d'=' -f2-
}

fetch_file() {
  local url="$1"
  local out="$2"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$url" -o "$out"
    return 0
  fi
  if command -v wget >/dev/null 2>&1; then
    wget -qO "$out" "$url"
    return 0
  fi
  echo "Neither curl nor wget found."
  return 1
}

if ask_yes_no "Install system packages (python3, venv, pip, sqlite3, tar, curl/wget)?" "y"; then
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv python3-pip sqlite3 tar curl wget
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y python3 python3-pip python3-virtualenv sqlite tar curl wget
  elif command -v yum >/dev/null 2>&1; then
    yum install -y python3 python3-pip tar curl wget
  else
    echo "No supported package manager found. Install Python dependencies manually."
  fi
fi

PROJECT_DIR=""
if [[ -f "$SCRIPT_DIR/main.py" && -f "$SCRIPT_DIR/requirements.txt" ]]; then
  PROJECT_DIR="$SCRIPT_DIR"
  echo "Detected project files next to install.sh, using: $PROJECT_DIR"
else
  TARGET_DIR="$(ask_with_default "Enter install directory" "$DEFAULT_INSTALL_DIR")"
  validate_install_dir "$TARGET_DIR"
  PROJECT_DIR="$TARGET_DIR"

  echo "Downloading project from:"
  echo "  $REPO_TARBALL_URL"

  TMP_DIR="$(mktemp -d)"
  ARCHIVE_PATH="$TMP_DIR/${REPO_NAME}.tar.gz"

  fetch_file "$REPO_TARBALL_URL" "$ARCHIVE_PATH"
  tar -xzf "$ARCHIVE_PATH" -C "$TMP_DIR"

  EXTRACTED_DIR="$(find "$TMP_DIR" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
  if [[ -z "$EXTRACTED_DIR" ]]; then
    echo "Failed to extract project archive."
    exit 1
  fi

  mkdir -p "$PROJECT_DIR"
  cp -a "$EXTRACTED_DIR"/. "$PROJECT_DIR"/
  rm -rf "$TMP_DIR"
  echo "Project downloaded to: $PROJECT_DIR"
fi

VENV_DIR="$PROJECT_DIR/.venv"
ENV_FILE="$PROJECT_DIR/.env"

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

SUPER_ADMIN_ID_VALUE="$(read_env_value "SUPER_ADMIN_ID" "$ENV_FILE")"
if [[ -z "${SUPER_ADMIN_ID_VALUE:-}" || ! "$SUPER_ADMIN_ID_VALUE" =~ ^[0-9]+$ ]]; then
  SUPER_ADMIN_ID_VALUE="149999149"
fi

mkdir -p "$PROJECT_DIR/bacup_database"

python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$PROJECT_DIR/requirements.txt"

# Первичная инициализация БД и супер-админа на этапе установки.
python3 - <<EOF
import sqlite3
from pathlib import Path

project_dir = Path(r"$PROJECT_DIR")
db_path = project_dir / "users.db"
super_admin_id = int("$SUPER_ADMIN_ID_VALUE")

conn = sqlite3.connect(str(db_path))
cur = conn.cursor()
cur.execute('''CREATE TABLE IF NOT EXISTS admins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER UNIQUE NOT NULL,
    username TEXT NOT NULL,
    added_by INTEGER NOT NULL,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)''')
cur.execute(
    "INSERT OR IGNORE INTO admins (user_id, username, added_by) VALUES (?, ?, ?)",
    (super_admin_id, "Супер-админ", super_admin_id),
)
conn.commit()
conn.close()
print(f"DB initialized: {db_path}")
print(f"Super admin ensured: {super_admin_id}")
EOF

echo
if command -v systemctl >/dev/null 2>&1; then
  UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
  cat > "$UNIT_FILE" <<EOF
[Unit]
Description=VPN TeleBot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_DIR
EnvironmentFile=-$ENV_FILE
ExecStart=$VENV_DIR/bin/python $PROJECT_DIR/main.py
Restart=always
RestartSec=3
User=root

[Install]
WantedBy=multi-user.target
EOF

  if command -v systemd-analyze >/dev/null 2>&1; then
    if ! systemd-analyze verify "$UNIT_FILE"; then
      echo "Generated systemd unit is invalid: $UNIT_FILE"
      echo "Review the file and rerun install."
      exit 1
    fi
  fi

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
