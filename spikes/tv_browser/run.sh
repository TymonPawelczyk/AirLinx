#!/usr/bin/env bash
# Start serwera testowego AirLinx (przeglądarka TV). Ctrl+C kończy (zabija też ffmpeg).
set -euo pipefail
cd "$(dirname "$0")"
PY=../../.venv/bin/python
IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}')
IP=${IP:-<IP-komputera>}
echo "TV (Internet):  http://${IP}:8090/            (menu)"
echo "                http://${IP}:8090/?test=auto  (MSE -> WebCodecs -> WebRTC, po 30 s)"
echo "PC (pełny ekran na przechwytywanym monitorze): http://localhost:8090/clock.html"
echo "Wyniki: $(pwd)/results/"
exec "$PY" server.py
