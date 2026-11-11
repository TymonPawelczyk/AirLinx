"""Rootless, LAN-isolated network for the virtual TV: two network namespaces joined by a veth pair.

    python -m tools.virtual_tv.netns [options] -- <command ...>

    +-------------- user namespace (you are "root" only inside it, no sudo) ---------------+
    |  netns "pc"  (the command runs here)         netns "tv"  (the virtual TV runs here)  |
    |  vtv-host 10.77.0.1/24  <------ veth ------>  vtv-tv 10.77.0.2/24                   |
    +-----------------------------------------------------------------------------------+

Neither namespace has a route to the real network: a stray packet cannot reach a real TV. The TV gets
an address different from the PC's, so everything that loopback hides is exercised: discovery rejects
loopback, media tokens are bound to the client IP, the media URL carries the PC's address.

Works with unprivileged user namespaces (``kernel.unprivileged_userns_clone=1``, the Ubuntu default) and
``unshare``, ``nsenter`` and ``ip`` from util-linux/iproute2. Nothing is changed on the host.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOST_IP, TV_IP, PREFIX = "10.77.0.1", "10.77.0.2", 24
ROOT = Path(__file__).resolve().parents[2]


def available() -> tuple[bool, str]:
    """Can this machine create the namespaces without root? (reason when not)"""
    for tool in ("unshare", "nsenter", "ip"):
        if shutil.which(tool) is None and not os.path.exists(f"/usr/sbin/{tool}"):
            return False, f"{tool} not found"
    r = subprocess.run(["unshare", "-rn", "true"], capture_output=True, text=True)
    if r.returncode != 0:
        return False, (r.stderr.strip() or "unprivileged user namespaces are disabled")
    return True, ""


def run_isolated(cmd: list[str], *, tv_args: list[str], tv_out: Path, env_extra: dict | None = None,
                 timeout: float | None = None, dev_trust: bool = False) -> subprocess.CompletedProcess:
    """Run ``cmd`` in the isolated 'pc' namespace with a virtual TV (``tv_args``) running in the 'tv' one."""
    spec = {"cmd": cmd, "tv_args": tv_args, "tv_out": str(tv_out), "env": env_extra or {}, "dev_trust": dev_trust}
    return subprocess.run(
        ["unshare", "-rn", "--kill-child", sys.executable, "-m", "tools.virtual_tv.netns", "--inner", json.dumps(spec)],
        cwd=ROOT, timeout=timeout, text=True)


def _sh(*a: str, ns_pid: int | None = None, check: bool = True) -> None:
    cmd = (["nsenter", "-t", str(ns_pid), "-n"] if ns_pid else []) + list(a)
    subprocess.run(cmd, check=check, capture_output=True)


def _wait_port(ip: str, port: int, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with contextlib.suppress(OSError):
            socket.create_connection((ip, port), 0.3).close()
            return True
        time.sleep(0.1)
    return False


def _inner(spec: dict) -> int:
    """Runs as 'root' of the new user namespace, inside the new network namespace ('pc')."""
    _sh("ip", "link", "set", "lo", "up")
    holder = subprocess.Popen(["unshare", "-n", "sleep", "infinity"])
    tv_proc = None
    try:
        time.sleep(0.2)
        _sh("ip", "link", "add", "vtv-host", "type", "veth", "peer", "name", "vtv-tv", "netns", str(holder.pid))
        _sh("ip", "addr", "add", f"{HOST_IP}/{PREFIX}", "dev", "vtv-host")
        _sh("ip", "link", "set", "vtv-host", "up", "multicast", "on")
        _sh("ip", "link", "set", "lo", "up", ns_pid=holder.pid)
        _sh("ip", "addr", "add", f"{TV_IP}/{PREFIX}", "dev", "vtv-tv", ns_pid=holder.pid)
        _sh("ip", "link", "set", "vtv-tv", "up", "multicast", "on", ns_pid=holder.pid)
        out = Path(spec["tv_out"])
        out.mkdir(parents=True, exist_ok=True)
        tv_cmd = ["nsenter", "-t", str(holder.pid), "-n", sys.executable, "-m", "tools.virtual_tv",
                  "--host", TV_IP, "--advertise", "--out", str(out), "--root-pem", str(out / "root.pem"),
                  "--ready-file", str(out / "ready.json"), *spec["tv_args"]]
        (out / "ready.json").unlink(missing_ok=True)
        tv_proc = subprocess.Popen(tv_cmd, cwd=ROOT, stdout=open(out / "tv.log", "w"), stderr=subprocess.STDOUT)
        end = time.monotonic() + 20
        while not (out / "ready.json").exists():
            if tv_proc.poll() is not None or time.monotonic() > end:
                print(f"virtual TV did not start, see {out / 'tv.log'}", file=sys.stderr)
                return 70
            time.sleep(0.1)
        env = dict(os.environ, VTV_NETNS="1", VTV_TV_IP=TV_IP, VTV_HOST_IP=HOST_IP, VTV_TV_OUT=str(out),
                   VTV_READY=str(out / "ready.json"), **spec["env"])
        if spec.get("dev_trust"):
            env.update(AIRLINX_DEV="1", AIRLINX_DEV_CAST_ROOT=str(out / "root.pem"))
        return subprocess.run(spec["cmd"], env=env, cwd=ROOT).returncode
    finally:
        if tv_proc and tv_proc.poll() is None:
            tv_proc.terminate()
            with contextlib.suppress(Exception):
                tv_proc.wait(5)
            if tv_proc.poll() is None:
                tv_proc.kill()
        holder.kill()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--inner"]:
        return _inner(json.loads(argv[1]))
    ap = argparse.ArgumentParser(prog="python -m tools.virtual_tv.netns", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tv-args", default="--cast --dlna", help="arguments of the virtual TV (default: --cast --dlna)")
    ap.add_argument("--out", default=None, help="directory for the TV's recordings and logs (default: temp dir)")
    ap.add_argument("--dev-trust", action="store_true",
                    help="start the command with AIRLINX_DEV=1 and AIRLINX_DEV_CAST_ROOT=<test root CA> "
                         "(developer mode of AirLinx, see docs/VIRTUAL_TV.md)")
    ap.add_argument("--check", action="store_true", help="only report whether unprivileged namespaces work")
    ap.add_argument("cmd", nargs=argparse.REMAINDER, help="-- command to run in the 'pc' namespace")
    a = ap.parse_args(argv)
    ok, why = available()
    if a.check:
        print("OK: rootless network namespaces work" if ok else f"UNAVAILABLE: {why}")
        return 0 if ok else 1
    if not ok:
        print(f"unprivileged network namespaces are unavailable ({why}); see docs/VIRTUAL_TV.md", file=sys.stderr)
        return 2
    cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    if not cmd:
        ap.error("give a command after --, e.g. -- bash   or   -- python -m airlinx --debug")
    out = Path(a.out) if a.out else Path(tempfile.mkdtemp(prefix="vtv-netns-"))
    print(f"[netns] pc={HOST_IP}  virtual TV={TV_IP}  recordings: {out}", file=sys.stderr)
    r = run_isolated(cmd, tv_args=shlex.split(a.tv_args), tv_out=out, dev_trust=a.dev_trust)
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
