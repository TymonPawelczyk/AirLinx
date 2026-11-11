#!/usr/bin/env bash
# Run a command in an isolated "pc" network namespace, with the virtual TV in a second namespace that has
# its own IP (10.77.0.2). Rootless: uses unprivileged user namespaces, no sudo, nothing on the host changes.
#
#   tools/virtual_tv/netns.sh -- bash                          # a shell that sees only the virtual TV
#   tools/virtual_tv/netns.sh --dev-trust -- .venv/bin/python -m airlinx --debug
#   tools/virtual_tv/netns.sh --tv-args "--cast --dlna --show" -- .venv/bin/python -m pytest -m vtv
#   tools/virtual_tv/netns.sh --check
#
# Neither namespace has a route to the real LAN, so a bug cannot reach a real TV. If the kernel forbids
# unprivileged namespaces (kernel.unprivileged_userns_clone=0), see docs/VIRTUAL_TV.md for the sudo variant.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
py="${PYTHON:-$repo/.venv/bin/python}"
[ -x "$py" ] || py=python3
cd "$repo"
exec "$py" -m tools.virtual_tv.netns "$@"
