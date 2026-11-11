"""Every failure mode of the virtual TV, seen through the real Controller: the error code (or state) the user
gets, and what must NOT happen (no silent downgrade to DLNA, no retry of an authentication failure)."""
import asyncio
import json

import pytest
import pytest_asyncio

from airlinx.core import models
from airlinx.core.cast import castv2 as cv

import vtvkit
from tools.virtual_tv.dlna_renderer import DlnaRenderer
from vtvkit import errors_of, make_clip, make_controller, make_device, wait_until


@pytest_asyncio.fixture
async def dlna(tmp_path):
    r = DlnaRenderer(port=0, out_dir=tmp_path / "dlna")
    await r.start()
    yield r
    await r.stop()


def device_with_dlna(tv, r: DlnaRenderer, ip="127.0.0.1") -> models.Device:
    d = make_device(tv, ip=ip)
    d.dlna_location = r.location
    d.caps.add("dlna")
    return d


async def started(tmp_path, dev):
    ctl, events = make_controller(tmp_path, [dev])
    await ctl.start()
    assert ctl.selected_device_id == dev.id
    return ctl, events


def pin_exists(dev) -> bool:
    return dev.id in json.loads(cv.known_tvs_path().read_text()) if cv.known_tvs_path().exists() else False


async def mirror_live(ctl, timeout=10):
    await wait_until(lambda: ctl.mirror.state == "live", timeout, "mirror live")


# ------------------------------------------------------------------------------------------ tv_auth_failed
@pytest.mark.asyncio
async def test_forged_device_auth_is_tv_auth_failed_before_anything_is_sent_and_never_downgrades(
        tmp_path, make_tv, dlna, synthetic):
    tv = await make_tv(fail="auth")
    ctl, events = await started(tmp_path, device_with_dlna(tv, dlna))
    with pytest.raises(models.AirlinxError) as ei:
        await ctl.mirror_start()
    assert ei.value.code == "tv_auth_failed"
    assert ctl.mirror.state == "error"
    # nothing but the device-auth challenge reached the "TV": no CONNECT, no LAUNCH, no OFFER
    assert [r for r in tv.received if r[0] != cv.NS_DEVICEAUTH] == []
    assert tv.events_of("deviceauth")[0].data["tampered"] is True
    assert tv.events_of("connection") and len(tv.events_of("connection")) == 1, "an auth failure must not be retried"
    assert dlna.actions == [], "must not fall back to unauthenticated DLNA after a failed Cast identity check"
    assert not pin_exists(ctl.device()), "a forged identity must not be pinned"
    # same on the media path
    clip = make_clip(tmp_path / "c.mp4", 2)
    (item,) = await ctl.media_add(paths=[str(clip)])
    with pytest.raises(models.AirlinxError) as ei:
        await ctl.media_play(item.id)
    assert ei.value.code == "tv_auth_failed" and dlna.actions == []
    await ctl.shutdown()


# ------------------------------------------------------------------------------------------ tv_cert_changed
@pytest.mark.asyncio
async def test_changed_device_identity_is_tv_cert_changed_with_a_trust_again_retry(
        tmp_path, make_tv, dlna, synthetic):
    tv = await make_tv(fail="cert")                       # identity A for connection 1, identity B afterwards
    dev = device_with_dlna(tv, dlna)
    ctl, events = await started(tmp_path, dev)
    # the first connection pins identity A (pin_key = device id)
    ch = await cv.CastChannel.open("127.0.0.1", tv.port, heartbeat=False, pin_key=dev.id)
    assert ch.auth_result == "pinned"
    await ch.close()
    assert pin_exists(dev)

    with pytest.raises(models.AirlinxError) as ei:
        await ctl.mirror_start()
    assert ei.value.code == "tv_cert_changed"
    err = [e for e in errors_of(events) if e["code"] == "tv_cert_changed"]
    assert err and err[0]["retry"] == {"method": "devices.trustAgain", "params": {"id": dev.id}}
    assert [r for r in tv.received if r[0] not in (cv.NS_DEVICEAUTH, cv.NS_CONNECTION)] == [], \
        "no LAUNCH/OFFER may be sent to a TV whose identity changed"
    assert dlna.actions == [], "tv_cert_changed must not fall back to DLNA"

    # the user confirms the replacement: pins again, then streaming works
    res = await ctl.devices_trust_again(dev.id)
    assert res["removed"] is True
    assert (await ctl.mirror_start())["ok"] is True
    await mirror_live(ctl)
    await ctl.mirror_stop()
    await ctl.shutdown()


@pytest.mark.asyncio
async def test_tls_certificate_rotation_alone_is_accepted(tmp_path, make_tv, synthetic):
    """The TLS certificate rotates every few days on a real TV; only the device identity is pinned."""
    a = await make_tv(device_id="aa" * 16)
    dev = make_device(a)
    ch = await cv.CastChannel.open("127.0.0.1", a.port, heartbeat=False, pin_key=dev.id)
    await ch.close()
    b = await make_tv(device_id="aa" * 16)                # new process, new TLS certificate, same device identity
    assert a.tls.der != b.tls.der and a.identity is b.identity
    ch = await cv.CastChannel.open("127.0.0.1", b.port, heartbeat=False, pin_key=dev.id)
    assert ch.auth_result == "match"
    await ch.close()


# ------------------------------------------------------------------------------------------ tv_busy
@pytest.mark.asyncio
async def test_busy_tv_asks_first_and_force_replaces_the_other_app(tmp_path, make_tv, synthetic):
    tv = await make_tv(fail="busy")
    ctl, events = await started(tmp_path, make_device(tv))
    res = await ctl.mirror_start()
    assert res["ok"] is False and res["code"] == "tv_busy"
    err = errors_of(events)[-1]
    assert err["code"] == "tv_busy" and err["app"] == "YouTube" and err["retry"]["params"]["force"] is True
    assert tv.events_of("launch") == [], "the sender must not LAUNCH over someone's YouTube without asking"
    assert tv.app["appId"] == "233637DE"

    res = await ctl.mirror_start(**err["retry"]["params"])
    assert res == {"ok": True, "protocol": "cast"}
    await mirror_live(ctl)
    assert tv.app["appId"] == "0F5096E8" and tv.events_of("launch")[0].data["running"] == "233637DE"
    await ctl.mirror_stop()
    assert tv.app is None
    await ctl.shutdown()


# ------------------------------------------------------------------------------------------ permission_pending
@pytest.mark.asyncio
async def test_consent_pending_is_shown_then_approval_goes_live(tmp_path, make_tv, synthetic):
    tv = await make_tv(fail="pending", pending_s=60)
    ctl, events = await started(tmp_path, make_device(tv))
    task = asyncio.create_task(ctl.mirror_start())
    await asyncio.wait_for(tv.consent_requested.wait(), 5)
    await wait_until(lambda: ctl.mirror.state == "waiting_tv" and ctl.mirror.detail, 5, "waiting_tv with a detail")
    assert ctl.mirror.detail == models.PermissionPendingError().message_pl == "Zaakceptuj połączenie na telewizorze."
    assert not task.done() and tv.app is None
    tv.approve()
    assert (await asyncio.wait_for(task, 10))["ok"] is True
    await mirror_live(ctl)
    await ctl.mirror_stop()
    await ctl.shutdown()


@pytest.mark.asyncio
async def test_consent_denied_is_tv_denied_without_fallback(tmp_path, make_tv, dlna, synthetic):
    tv = await make_tv(fail="pending", pending_s=60)
    ctl, events = await started(tmp_path, device_with_dlna(tv, dlna))
    task = asyncio.create_task(ctl.mirror_start())
    await asyncio.wait_for(tv.consent_requested.wait(), 5)
    tv.deny()
    with pytest.raises(models.AirlinxError) as ei:
        await asyncio.wait_for(task, 10)
    assert ei.value.code == "tv_denied" and dlna.actions == []
    await ctl.shutdown()


@pytest.mark.asyncio
async def test_giving_up_while_pending_stops_a_late_accepted_session(tmp_path, make_tv, synthetic):
    """The user cancels, then somebody presses OK on the TV: the app that appears must be STOPped."""
    tv = await make_tv(fail="pending", pending_s=60)
    ctl, events = await started(tmp_path, make_device(tv))
    task = asyncio.create_task(ctl.mirror_start())
    await asyncio.wait_for(tv.consent_requested.wait(), 5)
    await ctl.mirror_stop()                               # cancels the pending start
    assert (await asyncio.wait_for(task, 5)).get("code") == "cancelled"
    tv.approve()                                          # late OK: the TV launches the app anyway
    await wait_until(lambda: tv.events_of("launched"), 5, "the late launch")
    await wait_until(lambda: tv.app is None and tv.events_of("stop"), 10, "the guard to STOP the late app")
    await ctl.shutdown()


# ------------------------------------------------------------------------------------------ Cast port dead
@pytest.mark.asyncio
async def test_closed_cast_port_on_a_pinned_tv_asks_before_dlna_for_mirror_and_media(
        tmp_path, make_tv, dlna, synthetic):
    good = await make_tv(device_id="bb" * 16)
    dev = device_with_dlna(good, dlna)
    ch = await cv.CastChannel.open("127.0.0.1", good.port, heartbeat=False, pin_key=dev.id)
    await ch.close()                                      # TV identity is now pinned
    down = await make_tv(device_id="bb" * 16, fail="noport")
    dev.cast_port = down.port
    ctl, events = await started(tmp_path, dev)

    res = await ctl.mirror_start()
    assert res == {"ok": False, "code": "cast_unverified",
                   "message": ctl.CAST_UNVERIFIED_PL}
    err = errors_of(events)[-1]
    assert err["code"] == "cast_unverified" and err["retry"]["params"]["protocol"] == "dlna"
    clip = make_clip(tmp_path / "c.mp4", 2)
    (item,) = await ctl.media_add(paths=[str(clip)])
    res = await ctl.media_play(item.id)
    assert res["code"] == "cast_unverified"
    assert dlna.actions == [], "pinned TV: DLNA has no verification, so it must not be used silently"
    assert tv_events(down) == []
    await ctl.shutdown()


def tv_events(tv):
    return [e for e in tv.events if e.kind not in ("start",)]


@pytest.mark.asyncio
async def test_closed_cast_port_on_an_unpinned_tv_falls_back_to_dlna_for_files(tmp_path, make_tv, dlna, synthetic):
    tv = await make_tv(fail="noport")
    dev = device_with_dlna(tv, dlna)
    ctl, events = await started(tmp_path, dev)
    clip = make_clip(tmp_path / "c.mp4", 3)
    (item,) = await ctl.media_add(paths=[str(clip)])
    res = await ctl.media_play(item.id)
    assert res == {"ok": True, "protocol": "dlna", "itemId": item.id}
    await wait_until(lambda: dlna.state == "PLAYING", 10, "the DLNA renderer to play")
    assert any(e[0] == "toast" and "DLNA" in e[1]["message"] for e in events)
    await ctl.media_stop()
    await ctl.shutdown()


@pytest.mark.asyncio
async def test_silent_cast_port_times_out_as_tv_not_found(tmp_path, make_tv, monkeypatch, synthetic):
    orig = cv.CastChannel.open.__func__

    async def quick(cls, host, port=8009, **kw):
        kw["timeout"] = 1.0
        return await orig(cls, host, port, **kw)
    monkeypatch.setattr(cv.CastChannel, "open", classmethod(quick))
    tv = await make_tv(fail="hang")
    ctl, events = await started(tmp_path, make_device(tv))
    with pytest.raises(models.AirlinxError) as ei:
        await ctl.mirror_start()
    assert ei.value.code == "tv_not_found"
    await ctl.shutdown()
