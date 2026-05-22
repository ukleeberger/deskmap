#!/usr/bin/env bash
set -euo pipefail

REPO="$(dirname "$(readlink -f "$0")")"
BIN="$HOME/.local/bin"
DESKTOP="$(xdg-user-dir DESKTOP)"

mkdir -p "$BIN"

ln -sf "$REPO/deskmap.sh" "$BIN/deskmap"
chmod +x "$REPO/deskmap.sh"

ln -sf "$REPO/deskmap.desktop" "$DESKTOP/deskmap.desktop"

echo "Installed: $BIN/deskmap → $REPO/deskmap.sh"
echo "Desktop:   $DESKTOP/deskmap.desktop"
