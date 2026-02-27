#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
SERVICE_NAME="vpn-telebot"
SAFE_DIR="$PROJECT_DIR/.db_safe"

REPO_OWNER="${REPO_OWNER:-GennadyVyazmin}"
REPO_NAME="${REPO_NAME:-VpnTeleBot}"
REPO_BRANCH="${REPO_BRANCH:-master}"
REPO_TARBALL_URL="https://codeload.github.com/${REPO_OWNER}/${REPO_NAME}/tar.gz/refs/heads/${REPO_BRANCH}"

cd "$PROJECT_DIR"

echo "=== VPN TeleBot update ==="
echo "Project dir: $PROJECT_DIR"
echo "Source branch: $REPO_BRANCH"

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

mkdir -p "$SAFE_DIR"

echo "Saving runtime files to $SAFE_DIR ..."
cp -a .env "$SAFE_DIR"/ 2>/dev/null || true
cp -a users.db users.db-shm users.db-wal "$SAFE_DIR"/ 2>/dev/null || true

TMP_DIR="$(mktemp -d)"
ARCHIVE_PATH="$TMP_DIR/${REPO_NAME}.tar.gz"
EXTRACTED_DIR=""

echo "Downloading source archive..."
fetch_file "$REPO_TARBALL_URL" "$ARCHIVE_PATH"
tar -xzf "$ARCHIVE_PATH" -C "$TMP_DIR"
EXTRACTED_DIR="$(find "$TMP_DIR" -mindepth 1 -maxdepth 1 -type d | head -n 1)"

if [[ -z "$EXTRACTED_DIR" || ! -d "$EXTRACTED_DIR" ]]; then
  echo "Failed to extract source archive."
  rm -rf "$TMP_DIR"
  exit 1
fi

echo "Updating all .py and .sh files from branch '$REPO_BRANCH' ..."
updated_count=0
while IFS= read -r src_file; do
  rel_path="${src_file#"$EXTRACTED_DIR/"}"
  dst_file="$PROJECT_DIR/$rel_path"
  mkdir -p "$(dirname "$dst_file")"
  cp -f "$src_file" "$dst_file"
  updated_count=$((updated_count + 1))
done < <(find "$EXTRACTED_DIR" -type f \( -name "*.py" -o -name "*.sh" \))

echo "Updated files: $updated_count"

if [[ -f "$EXTRACTED_DIR/requirements.txt" ]]; then
  cp -f "$EXTRACTED_DIR/requirements.txt" "$PROJECT_DIR/requirements.txt"
  echo "requirements.txt updated"
fi

rm -rf "$TMP_DIR"

echo "Restoring runtime files after update..."
cp -a "$SAFE_DIR"/.env "$PROJECT_DIR"/ 2>/dev/null || true
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
    systemctl restart "${SERVICE_NAME}.service"
    systemctl --no-pager --full status "${SERVICE_NAME}.service" || true
  else
    echo "Service ${SERVICE_NAME}.service not found. Start manually:"
    echo "  $VENV_DIR/bin/python $PROJECT_DIR/main.py"
  fi
else
  echo "systemd not found. Start manually:"
  echo "  $VENV_DIR/bin/python $PROJECT_DIR/main.py"
fi

if ask_yes_no "Delete temporary safe copy folder ($SAFE_DIR)?" "y"; then
  rm -rf "$SAFE_DIR"
fi

echo "Update complete."
