#!/usr/bin/env bash
# AirLinx — instalacja per-user do ~/.local (bez sudo).
#
#   ./install.sh                 instaluje: kod → ~/.local/share/airlinx/app, launcher ~/.local/bin/airlinx,
#                                plik .desktop, ikony hicolor
#   ./install.sh --autostart     j.w. + autostart w zasobniku (~/.config/autostart)
#   ./install.sh --uninstall     usuwa wszystko powyższe (config ~/.config/airlinx zostaje)
#
# Python: zawsze własny venv ~/.local/share/airlinx/venv (--system-site-packages, przez uv albo python -m venv;
# zależności z PyPI). Nadpisanie: AIRLINX_PYTHON=/ścieżka; AIRLINX_SKIP_DEPS=1 pomija pip (testy/offline).
set -euo pipefail

APP_ID="io.github.airlinx"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
BIN_DIR="$HOME/.local/bin"
PREFIX="$DATA_HOME/airlinx"
LAUNCHER="$BIN_DIR/airlinx"
DESKTOP="$DATA_HOME/applications/$APP_ID.desktop"
AUTOSTART="$CONFIG_HOME/autostart/$APP_ID.desktop"
ICONS="$DATA_HOME/icons/hicolor"

AUTOSTART_FLAG=0; UNINSTALL=0
for a in "$@"; do
  case "$a" in
    --autostart) AUTOSTART_FLAG=1 ;;
    --venv) ;;  # zgodność wstecz: venv jest teraz domyślny
    --uninstall) UNINSTALL=1 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "Nieznana opcja: $a" >&2; exit 2 ;;
  esac
done

icon_files() { (cd "$SRC/data/icons/hicolor" && find . -type f); }

refresh_caches() {
  command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -f -t "$ICONS" >/dev/null 2>&1 || true
  command -v update-desktop-database >/dev/null && update-desktop-database "$DATA_HOME/applications" >/dev/null 2>&1 || true
}

if [[ $UNINSTALL == 1 ]]; then
  rm -f "$LAUNCHER" "$DESKTOP" "$AUTOSTART"
  while IFS= read -r f; do rm -f "$ICONS/$f"; done < <(icon_files)
  rm -rf "$PREFIX"
  refresh_caches
  echo "AirLinx odinstalowany. (Ustawienia zostały w $CONFIG_HOME/airlinx)"
  exit 0
fi

# --- kod ----------------------------------------------------------------------
mkdir -p "$PREFIX/app" "$BIN_DIR" "$(dirname "$DESKTOP")" "$ICONS"
rm -rf "$PREFIX/app"; mkdir -p "$PREFIX/app"   # venv w $PREFIX/venv zostaje między aktualizacjami
cp -r "$SRC/airlinx" "$SRC/ui" "$SRC/data" "$PREFIX/app/"
find "$PREFIX/app" -name '__pycache__' -type d -prune -exec rm -rf {} +

# --- python: własny venv w $PREFIX (niezależny od katalogu repo) ---------------------
DEPS=(pychromecast zeroconf aiohttp async-upnp-client cryptography protobuf)
if [[ -n "${AIRLINX_PYTHON:-}" ]]; then
  PY="$AIRLINX_PYTHON"                       # świadome nadpisanie (np. do testów)
else
  BASE_PY="${AIRLINX_BASE_PYTHON:-/usr/bin/python3}"   # systemowy Python z PyGObject (Gtk/WebKit2)
  [[ -x "$BASE_PY" ]] || BASE_PY="$(command -v python3)"
  VENV="$PREFIX/venv"
  if [[ ! -x "$VENV/bin/python" ]]; then
    if command -v uv >/dev/null; then
      uv venv -q --python "$BASE_PY" --system-site-packages "$VENV"
    else
      "$BASE_PY" -m venv --system-site-packages "$VENV"
    fi
  fi
  if [[ "${AIRLINX_SKIP_DEPS:-0}" != 1 ]]; then
    if command -v uv >/dev/null; then
      VIRTUAL_ENV="$VENV" uv pip install -q --python "$VENV/bin/python" "${DEPS[@]}"
    else
      "$VENV/bin/python" -m pip install -q "${DEPS[@]}"
    fi
  fi
  PY="$VENV/bin/python"
  "$PY" -c 'import gi; gi.require_version("WebKit2", "4.1")' 2>/dev/null \
    || echo "Uwaga: brak PyGObject/WebKit2 4.1 w $BASE_PY (pakiety: python3-gi gir1.2-webkit2-4.1)." >&2
fi

cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
# AirLinx launcher (wygenerowany przez install.sh)
export PYTHONPATH="$PREFIX/app\${PYTHONPATH:+:\$PYTHONPATH}"
cd "$PREFIX/app"
exec "$PY" -m airlinx "\$@"
EOF
chmod +x "$LAUNCHER"

# --- desktop + ikony --------------------------------------------------------------
# Exec= w cudzysłowach (spacja w $HOME dzieliłaby polecenie na dwa argumenty). Wg Desktop Entry Spec w cudzysłowach
# poprzedza się backslashem " ` $ \, potem każdy backslash się podwaja (reguła wartości string), a % zapisuje jako %%.
desktop_exec() {
  local s=$1
  s=${s//\\/\\\\}; s=${s//\"/\\\"}; s=${s//\`/\\\`}; s=${s//\$/\\\$}
  s=${s//\\/\\\\}; s=${s//%/%%}
  printf '"%s"' "$s"
}
# render_desktop SZABLON CEL [ARGUMENTY]: podstawia @EXEC@ bez seda (ścieżka może zawierać | albo &)
render_desktop() {
  local exec_value line
  exec_value="$(desktop_exec "$LAUNCHER")${3:+ $3}"
  while IFS= read -r line || [[ -n $line ]]; do
    while [[ $line == *@EXEC@* ]]; do line="${line%%@EXEC@*}${exec_value}${line#*@EXEC@}"; done
    printf '%s\n' "$line"
  done < "$1" > "$2"
}
render_desktop "$SRC/data/$APP_ID.desktop" "$DESKTOP"
cp -r "$SRC/data/icons/hicolor/." "$ICONS/"

if [[ $AUTOSTART_FLAG == 1 ]]; then
  mkdir -p "$(dirname "$AUTOSTART")"
  render_desktop "$SRC/data/$APP_ID-autostart.desktop" "$AUTOSTART" --no-window
fi

refresh_caches
echo "AirLinx zainstalowany: $LAUNCHER (python: $PY)"
[[ $AUTOSTART_FLAG == 1 ]] && echo "Autostart: $AUTOSTART"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) echo "Uwaga: $BIN_DIR nie jest w PATH." ;; esac
