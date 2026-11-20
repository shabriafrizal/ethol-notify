#!/usr/bin/env bash
# Install or move the ETHOL PENS Telegram notifier.
#
# Usage:
#   ./install.sh [TARGET_DIR] [--systemd]
#
# TARGET_DIR defaults to ~/ethol-notify. Existing .env and state files in the
# destination are never overwritten.
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: ./install.sh [TARGET_DIR] [--systemd]

  TARGET_DIR   Destination directory (default: ~/ethol-notify)
  --systemd    Install and start the systemd service (uses sudo)

The package's .env and state/ are copied only when they are not already
present in the destination. The generated service unit uses the target
directory and the current user.
USAGE
}

TARGET=""
WITH_SYSTEMD=0
for arg in "$@"; do
  case "$arg" in
    --systemd)
      WITH_SYSTEMD=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    -*)
      echo "Unknown option: $arg" >&2
      usage >&2
      exit 2
      ;;
    *)
      TARGET="$arg"
      ;;
  esac
done

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${TARGET:-${ETHOL_INSTALL_DIR:-$HOME/ethol-notify}}"
mkdir -p "$TARGET"
TARGET="$(cd "$TARGET" && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required." >&2
  exit 1
fi

if [ "$SOURCE_DIR" != "$TARGET" ]; then
  install -m 0644 "$SOURCE_DIR/ethol_notify.py" "$TARGET/ethol_notify.py"
  install -m 0644 "$SOURCE_DIR/README.md" "$TARGET/README.md"
  install -m 0644 "$SOURCE_DIR/.env.example" "$TARGET/.env.example"
  install -m 0755 "$SOURCE_DIR/install.sh" "$TARGET/install.sh"
  if [ -f "$SOURCE_DIR/.gitignore" ]; then
    install -m 0644 "$SOURCE_DIR/.gitignore" "$TARGET/.gitignore"
  fi

  # Never overwrite an existing .env.
  if [ -f "$TARGET/.env" ]; then
    echo "Keeping existing $TARGET/.env"
  elif [ -f "$SOURCE_DIR/.env" ]; then
    install -m 0600 "$SOURCE_DIR/.env" "$TARGET/.env"
  else
    install -m 0600 "$SOURCE_DIR/.env.example" "$TARGET/.env"
    echo "Created $TARGET/.env from .env.example; fill in the credentials."
  fi

  # Keep existing cookies/database if the destination already has state.
  mkdir -p "$TARGET/state"
  for f in cookies.txt state.db; do
    if [ -f "$SOURCE_DIR/state/$f" ] && [ ! -e "$TARGET/state/$f" ]; then
      install -m 0600 "$SOURCE_DIR/state/$f" "$TARGET/state/$f"
    fi
  done
else
  echo "Running inside the target directory; keeping files as-is."
fi

mkdir -p "$TARGET/state"
chmod 700 "$TARGET/state"
chmod 600 "$TARGET/.env"
for f in cookies.txt state.db; do
  if [ -f "$TARGET/state/$f" ]; then
    chmod 600 "$TARGET/state/$f"
  fi
done

# Generate a unit whose paths match this installation. Use a temporary file
# because SOURCE_DIR and TARGET can be the same directory.
RUN_USER="$(id -un)"
RUN_GROUP="$(id -gn)"
ESCAPED_TARGET="${TARGET//&/\\&}"
SERVICE_TMP="$(mktemp)"
trap 'rm -f "$SERVICE_TMP"' EXIT
sed \
  -e "s|/home/ubuntu/ethol-notify|$ESCAPED_TARGET|g" \
  -e "s|^User=.*|User=$RUN_USER|" \
  -e "s|^Group=.*|Group=$RUN_GROUP|" \
  "$SOURCE_DIR/ethol-notify.service" > "$SERVICE_TMP"
install -m 0644 "$SERVICE_TMP" "$TARGET/ethol-notify.service"

echo
echo "Installed to: $TARGET"
echo "Next steps:"
echo "  cd \"$TARGET\""
echo "  python3 ethol_notify.py --test-telegram"
echo "  python3 ethol_notify.py --loop"

if [ "$WITH_SYSTEMD" = 1 ]; then
  if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemctl was not found; skipping service installation." >&2
    exit 1
  fi
  sudo install -m 0644 "$TARGET/ethol-notify.service" \
    /etc/systemd/system/ethol-notify.service
  sudo systemctl daemon-reload
  sudo systemctl enable --now ethol-notify
  echo
  sudo systemctl status ethol-notify --no-pager || true
fi
