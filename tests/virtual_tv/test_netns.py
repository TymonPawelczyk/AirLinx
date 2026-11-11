"""The isolated network (rootless netns): the PC and the virtual TV get different addresses, real mDNS/SSDP
discovery finds the TV, and no packet can leave towards the real LAN. The heavy lifting is in
netns_scenario.py, which runs inside the namespace; this file launches it and judges the result."""
import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from tools.virtual_tv import announce, netns

ROOT = Path(__file__).resolve().parents[2]
_ok, _why = netns.available()
needs_ns = pytest.mark.skipif(not _ok, reason=f"rootless network namespaces unavailable: {_why}")


@needs_ns
def test_discovery_mirror_and_playback_over_an_isolated_network_with_a_separate_tv_address(tmp_path):
    r = subprocess.run(
        [sys.executable, "-m", "tools.virtual_tv.netns", "--dev-trust", "--out", str(tmp_path / "tv"), "--",
         sys.executable, "tests/virtual_tv/netns_scenario.py"],
        cwd=ROOT, capture_output=True, text=True, timeout=240)
    rows = []
    for line in r.stdout.splitlines():
        if line.startswith("{"):
            rows.append(json.loads(line))
    failed = [x for x in rows if not x["ok"]]
    assert r.returncode == 0 and not failed and len(rows) >= 20, (
        f"rc={r.returncode} failed={failed}\n--- stdout ---\n{r.stdout[-3000:]}\n--- stderr ---\n{r.stderr[-3000:]}")
    names = {x["check"] for x in rows}
    for must in ("192.168.1.1 is unreachable", "mDNS + SSDP discovery finds the TV with both Cast and DLNA",
                 "loopback is rejected by manual add", "live within 10 s", "unknown token -> 404"):
        assert must in names


def host_has_real_interfaces() -> bool:
    return any(not n.startswith(announce.ALLOWED_PREFIXES) for _, n in socket.if_nameindex())


@pytest.mark.skipif(not host_has_real_interfaces(), reason="this host has no non-loopback interface")
def test_announcing_is_refused_on_the_host_network():
    with pytest.raises(announce.NotIsolatedError):
        announce.assert_isolated()
    r = subprocess.run([sys.executable, "-m", "tools.virtual_tv", "--cast", "--advertise", "--cast-port", "0"],
                       cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert r.returncode == 3 and "refusing to announce" in r.stderr


@needs_ns
def test_inside_the_namespace_only_loopback_and_the_veth_are_visible_and_announcing_is_allowed():
    code = ("import socket; from tools.virtual_tv import announce; "
            "print(announce.visible_interfaces()); announce.assert_isolated(); print('ok')")
    r = subprocess.run(["unshare", "-rn", sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                       timeout=30)
    assert r.returncode == 0 and "['lo']" in r.stdout and r.stdout.strip().endswith("ok"), r.stdout + r.stderr


def test_no_announcement_code_path_bypasses_the_guard():
    """Both multicast senders must call assert_isolated() in start(); there is deliberately no off switch."""
    src = (ROOT / "tools" / "virtual_tv" / "announce.py").read_text()
    assert src.count("assert_isolated()") >= 3          # def + MdnsAnnouncer.start + SsdpResponder.start
    for forbidden in ("--no-guard", "--force", "skip_guard", "AIRLINX_VTV_ALLOW"):
        assert forbidden not in src
    main_src = (ROOT / "tools" / "virtual_tv" / "__main__.py").read_text()
    assert "assert_isolated" in main_src and "no-guard" not in main_src
