"""Runs INSIDE the isolated network (tools/virtual_tv/netns.py): the PC is 10.77.0.1, the virtual TV is a
separate process at 10.77.0.2, and nothing else is reachable. Real mDNS/SSDP discovery, a real Controller,
real MirrorSession / CastMediaPlayer / DlnaPlayer / MediaServer. Prints one JSON line per check and exits 0
only when every check passed.

Not a pytest file (no test_ prefix): tests/virtual_tv/test_netns.py launches it through netns.py.
"""
import asyncio
import hashlib
import json
import os
import socket
import sys
import tempfile
import time
import traceback
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))

import pytest  # noqa: E402

import vtvkit  # noqa: E402
from airlinx.core.cast import castv2  # noqa: E402
from airlinx.core.discovery import Discovery  # noqa: E402

HOST_IP, TV_IP = os.environ["VTV_HOST_IP"], os.environ["VTV_TV_IP"]
TV_OUT = Path(os.environ["VTV_TV_OUT"])
READY = json.loads(Path(os.environ["VTV_READY"]).read_text())
checks: list[dict] = []


def check(name: str, ok: bool, detail="") -> None:
    checks.append({"check": name, "ok": bool(ok), "detail": detail})
    print(json.dumps(checks[-1], default=str), flush=True)


def events(sub: str) -> list[dict]:
    p = TV_OUT / sub / "events.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="vtv-scn-"))
    os.environ["AIRLINX_KNOWN_TVS"] = str(tmp / "known_tvs.json")

    # 1. the isolation itself
    ifaces = sorted(n for _, n in socket.if_nameindex())
    check("only lo and the veth to the TV exist here", ifaces == ["lo", "vtv-host"], ifaces)
    for target in (("192.168.1.1", 8009), ("8.8.8.8", 53)):
        try:
            socket.create_connection(target, 1.0).close()
            check(f"{target[0]} is unreachable", False, "connected!")
        except OSError as e:
            check(f"{target[0]} is unreachable", True, repr(e))

    # 2. developer trust through the real environment path
    subjects = castv2.configure_dev_trust()
    check("developer trust enabled from AIRLINX_DEV + AIRLINX_DEV_CAST_ROOT", bool(subjects) and castv2.dev_trust_active(),
          subjects)

    # 3. discovery: the TV must be found by mDNS + SSDP at an address that is not loopback
    found: list = []
    disc = Discovery(lambda devs: found.append(list(devs)), interface_ip=HOST_IP, rescan_interval=5)
    await disc.start()
    t0 = time.monotonic()
    dev = None
    while time.monotonic() - t0 < 25:
        dev = next((d for d in disc.devices if d.ip == TV_IP and {"cast_mirror", "dlna"} <= d.caps), None)
        if dev:
            break
        await asyncio.sleep(0.25)
    check("mDNS + SSDP discovery finds the TV with both Cast and DLNA", dev is not None,
          {"after_s": round(time.monotonic() - t0, 1), "devices": [vars(d) for d in disc.devices]})
    assert dev is not None
    check("discovered device fields", dev.id == READY["cast"]["device_id"] and dev.name == "Wirtualny TV"
          and dev.cast_port == READY["cast"]["port"] and dev.dlna_location == f"http://{TV_IP}:9197/dmr"
          and dev.ip != "127.0.0.1", vars(dev))
    check("loopback is rejected by manual add", await disc.add_manual("127.0.0.1") is None)
    man = await disc.add_manual(TV_IP)
    check("manual add by IP works (eureka_info)", man is not None and man.ip == TV_IP, man and vars(man))
    await disc.stop()

    # 4. controller on top of the real discovery
    mp = pytest.MonkeyPatch()
    vtvkit.patch_capture(mp)
    ctl, evs = vtvkit.make_controller(tmp, [], discovery=lambda cb: Discovery(cb, interface_ip=HOST_IP,
                                                                              rescan_interval=5))
    await ctl.start()
    for _ in range(100):
        if any(d.ip == TV_IP and "dlna" in d.caps and d.cast_port for d in ctl.devices):
            break
        await asyncio.sleep(0.25)
    d = ctl.device()
    check("controller selected the discovered TV", d.ip == TV_IP, vars(d))
    check("AppState shows the developer-trust warning flag", ctl.state().dev_cast_root is True)

    # 5. mirroring over a real (non-loopback) path
    t_start = time.monotonic()
    res = await ctl.mirror_start()
    check("mirror.start ok over cast", res == {"ok": True, "protocol": "cast"}, res)
    await vtvkit.wait_until(lambda: ctl.mirror.state == "live", 10, "live")
    check("live within 10 s", time.monotonic() - t_start <= 10, round(time.monotonic() - t_start, 1))
    await asyncio.sleep(4)
    st = ctl.mirror.stats
    await ctl.mirror_stop()
    ev = events("cast")
    kinds = [e["kind"] for e in ev]
    conn = next(e for e in ev if e["kind"] == "connection")
    check("TV saw the control connection from the PC's veth address", conn["peer"] == HOST_IP, conn)
    check("TV got device-auth, LAUNCH, OFFER->ANSWER, STOP",
          all(k in kinds for k in ("deviceauth", "launched", "answer", "stop")), kinds)
    vid = TV_OUT / "cast" / "video.h264"
    info = vtvkit.ffprobe_frames(vid, "h264")["streams"][0]
    check("TV recorded decodable H.264", info["codec_name"] == "h264" and int(info["nb_read_frames"]) >= 150
          and vtvkit.decode_errors(vid, "h264") == "", info)
    check("sender stats: fps >= 50", st is not None and st.fps >= 50, st and st.fps)

    # 6. file over Cast (DMR) and DLNA: the media URL must carry the PC's address, not loopback
    clip = vtvkit.make_clip(tmp / "clip.mp4", 3)
    (item,) = await ctl.media_add(paths=[str(clip)])
    res = await ctl.media_play(item.id, protocol="cast")
    check("media.play over cast", res.get("ok") is True, res)
    await vtvkit.wait_until(lambda: any(e["kind"] == "download_done" for e in events("cast")), 15, "TV download")
    load = next(e for e in events("cast") if e["kind"] == "load")
    check("Cast media URL host is the PC's address", urlparse(load["url"]).hostname == HOST_IP, load["url"])
    saved = sorted((TV_OUT / "cast" / "media").glob("*"))
    check("TV downloaded the exact file", bool(saved) and sha(saved[-1]) == sha(clip))
    await vtvkit.wait_until(lambda: ctl.playback.state in ("ended", "idle"), 15, "cast clip end")
    await ctl.media_stop()

    res = await ctl.media_play(item.id, protocol="dlna")
    check("media.play over dlna", res.get("ok") is True, res)
    await vtvkit.wait_until(lambda: any(e["kind"] == "fetch" for e in events("dlna")), 15, "DLNA fetch")
    fe = next(e for e in events("dlna") if e["kind"] == "fetch")
    check("DLNA media URL host is the PC's address", urlparse(fe["url"]).hostname == HOST_IP, fe["url"])
    check("DLNA renderer identified itself like a Samsung and fetched everything",
          fe["ua"].startswith("SEC_HHP") and fe["bytes"] == clip.stat().st_size, fe)
    url = fe["url"]
    await ctl.media_stop()

    # 7. what a stranger sees: a wrong token is a 404 on the media server
    import aiohttp
    async with aiohttp.ClientSession() as s:
        bad = url.rsplit("/", 2)[0] + "/not-a-token/media.mp4"
        async with s.get(bad) as r:
            check("unknown token -> 404", r.status == 404, r.status)
    await ctl.shutdown()
    mp.undo()


if __name__ == "__main__":
    try:
        asyncio.run(asyncio.wait_for(main(), 150))
    except BaseException:  # noqa: BLE001
        traceback.print_exc()
        check("scenario completed", False, "exception")
    bad = [c for c in checks if not c["ok"]]
    print(f"SUMMARY {len(checks) - len(bad)}/{len(checks)} checks passed", flush=True)
    sys.exit(1 if bad else 0)
