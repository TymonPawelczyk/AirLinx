#!/usr/bin/env bash
# AirLinx – test natywnego Cast mirroringu na TV (jedna komenda).
# Pokazuje na monitorze DP-4 duży zegar (ms), streamuje ekran przez Cast Streaming
# (app "Chrome Mirroring" 0F5096E8) – wideo H.264 + audio Opus z monitora domyślnego wyjścia
# (wyłącz: --no-audio). Na końcu ZAWSZE wysyła STOP; nie startuje, gdy na TV działa inna aplikacja.
#
# Adres TV: zmienna AIRLINX_TV_IP albo opcja --tv w dodatkowych opcjach.
# Użycie:  ./run_test.sh [sekundy=30] [targetDelay_ms=100] [dodatkowe opcje cast_mirror.py]
#   np.    ./run_test.sh 30 50 --bitrate 8000000 --fps 60
# Pomiar glass-to-glass: zrób jedno zdjęcie telefonem obejmujące monitor i TV;
# różnica odczytów zegara (monitor - TV) = opóźnienie end-to-end.
set -euo pipefail
cd "$(dirname "$0")"
SECS="${1:-30}"; DELAY="${2:-100}"; shift $(( $# >= 2 ? 2 : $# )) || true
PY="${PY:-../../.venv/bin/python}"
export DISPLAY="${DISPLAY:-:1}"
mkdir -p logs
LOG="logs/run_$(date +%Y%m%d_%H%M%S).log"

ffplay -hide_banner -loglevel error -autoexit -window_title "AirLinx clock" -left 200 -top 900 \
  -f lavfi -i "color=c=black:s=1400x320:r=60:d=$((SECS + 15)),drawtext=fontsize=220:fontcolor=white:x=30:y=40:text='%{pts\:hms}'" &
CLOCK=$!
trap 'kill $CLOCK 2>/dev/null || true' EXIT

"$PY" cast_mirror.py --seconds "$SECS" --delay "$DELAY" --log "$LOG" "$@" \
  | grep --line-buffered -E "ANSWER|STAT|SUMMARY|ms: |STOP|apps after|ERROR|WARN"
echo "Pełny log: $(pwd)/$LOG"
