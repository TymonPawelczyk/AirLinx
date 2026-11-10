#!/usr/bin/env bash
# Instaluje ikony AirLinx do motywu hicolor użytkownika (~/.local/share/icons/hicolor).
# Użycie: bash install-icons.sh            (instalacja)
#         bash install-icons.sh --system   (do /usr/share/icons/hicolor, wymaga sudo)
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)/hicolor"
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor"
[[ "${1:-}" == "--system" ]] && DEST="/usr/share/icons/hicolor"
mkdir -p "$DEST"
cp -r "$SRC"/. "$DEST"/
if command -v gtk-update-icon-cache >/dev/null; then
  gtk-update-icon-cache -f -t "$DEST" >/dev/null 2>&1 || true
fi
echo "Zainstalowano ikony AirLinx w $DEST"
