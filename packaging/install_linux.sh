#!/usr/bin/env sh
# Install the MED17.7.5 Flash Tool on a Linux desktop (per-user, no root).
#
# Run this from the folder that contains the downloaded binary, e.g.:
#   sh install_linux.sh ./med17flasher-desktop-linux
#
# It copies the binary to ~/.local/bin, installs the icon and a .desktop entry
# so the app shows up in your application menu, and makes the binary executable.
set -eu

BIN_SRC="${1:-./med17flasher-desktop-linux}"
if [ ! -f "$BIN_SRC" ]; then
    echo "error: binary not found: $BIN_SRC" >&2
    echo "usage: sh install_linux.sh <path-to-med17flasher-desktop-linux>" >&2
    exit 1
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
APP_DIR="${HOME}/.local/share/applications"
ICON_DIR="${HOME}/.local/share/icons/hicolor/512x512/apps"

mkdir -p "$BIN_DIR" "$APP_DIR" "$ICON_DIR"

install -m 0755 "$BIN_SRC" "${BIN_DIR}/med17flasher-desktop"

# Icon + .desktop entry (best effort; the app still runs from the CLI without them).
[ -f "${HERE}/icon.png" ] && install -m 0644 "${HERE}/icon.png" "${ICON_DIR}/med17flasher.png"
if [ -f "${HERE}/med17flasher.desktop" ]; then
    install -m 0644 "${HERE}/med17flasher.desktop" "${APP_DIR}/med17flasher.desktop"
    command -v update-desktop-database >/dev/null 2>&1 && \
        update-desktop-database "$APP_DIR" >/dev/null 2>&1 || true
fi

echo "Installed to ${BIN_DIR}/med17flasher-desktop"
case ":${PATH}:" in
    *":${BIN_DIR}:"*) : ;;
    *) echo "note: add ${BIN_DIR} to your PATH to launch it by name" >&2 ;;
esac
echo "Launch it from your app menu ('MED17 Flash Tool') or run: med17flasher-desktop"
