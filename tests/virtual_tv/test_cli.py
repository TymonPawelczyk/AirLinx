"""python -m tools.virtual_tv as a user runs it: banner, ready file, root PEM, failure modes, clean SIGINT."""
import json
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from cryptography import x509

ROOT = Path(__file__).resolve().parents[2]


def start(tmp_path, *args):
    ready = tmp_path / "ready.json"
    p = subprocess.Popen([sys.executable, "-m", "tools.virtual_tv", "--cast-port", "0", "--dlna-port", "0",
                          "--eureka-port", "0", "--out", str(tmp_path / "out"), "--ready-file", str(ready), *args],
                         cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    end = time.monotonic() + 15
    while not ready.exists():
        if p.poll() is not None or time.monotonic() > end:
            raise AssertionError(f"did not start: {p.stdout.read() if p.stdout else ''}")
        time.sleep(0.05)
    return p, json.loads(ready.read_text())


def stop(p) -> str:
    p.send_signal(signal.SIGINT)
    out, _ = p.communicate(timeout=10)
    return out


def test_cast_and_dlna_start_print_instructions_and_stop_cleanly_on_sigint(tmp_path):
    p, info = start(tmp_path, "--cast", "--dlna", "--name", "Salon (test)")
    try:
        with socket.create_connection(("127.0.0.1", info["cast"]["port"]), 2):
            pass
        assert info["dlna"]["location"].startswith("http://127.0.0.1:") and info["name"] == "Salon (test)"
        root = x509.load_pem_x509_certificate(Path(info["root_pem"]).read_bytes())
        assert "NOT A REAL CAST ROOT" in root.subject.rfc4514_string()
        assert b"PRIVATE KEY" not in Path(info["root_pem"]).read_bytes(), "only the public root may be written"
    finally:
        out = stop(p)
    assert p.returncode == 0
    assert "AIRLINX_DEV=1 AIRLINX_DEV_CAST_ROOT=" in out and "netns.sh" in out
    assert "Samsung quirks" in out and "TEST root CA" in out and "Ctrl+C" in out
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", info["cast"]["port"]), 0.5)


@pytest.mark.parametrize("mode", ["auth", "cert", "busy", "pending", "hang"])
def test_failure_modes_are_accepted_and_announced(tmp_path, mode):
    p, info = start(tmp_path, "--cast", "--fail", mode)
    try:
        assert info["cast"]["fail"] == mode
        socket.create_connection(("127.0.0.1", info["cast"]["port"]), 2).close()
    finally:
        out = stop(p)
    assert f"[failure mode: {mode}]" in out


def test_noport_mode_really_listens_on_nothing(tmp_path):
    p, info = start(tmp_path, "--cast", "--dlna", "--fail", "noport")
    try:
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", info["cast"]["port"]), 0.5)
        socket.create_connection(("127.0.0.1", int(info["dlna"]["location"].split(":")[2].split("/")[0])), 2).close()
    finally:
        stop(p)


def test_needs_cast_or_dlna_and_rejects_unknown_failure_modes(tmp_path):
    r = subprocess.run([sys.executable, "-m", "tools.virtual_tv"], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 2 and "--cast" in r.stderr
    r = subprocess.run([sys.executable, "-m", "tools.virtual_tv", "--cast", "--fail", "nope"], cwd=ROOT,
                       capture_output=True, text=True)
    assert r.returncode == 2
