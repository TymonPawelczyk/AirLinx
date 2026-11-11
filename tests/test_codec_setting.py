"""mirror.codec: settings validation/migration, controller plumbing and the HEVC encoder in AppState."""
import json

import pytest

from airlinx.settings import DEFAULTS, SCHEMA_VERSION, Settings

from test_controller import DP4, Env, FakeSession, run, settle  # noqa: F401


# ---------------------------------------------------------------------------------- settings
def test_default_codec_is_h264_until_hevc_is_confirmed_on_a_real_tv(tmp_path):
    assert DEFAULTS["mirror"]["codec"] == "h264"
    assert Settings(tmp_path / "c.json").get("mirror.codec") == "h264"


@pytest.mark.parametrize("value", ["auto", "h264", "hevc"])
def test_valid_codecs_are_kept_and_persisted(tmp_path, value):
    p = tmp_path / "c.json"
    Settings(p).update({"mirror": {"codec": value}})
    assert json.loads(p.read_text())["mirror"]["codec"] == value
    assert Settings(p).get("mirror.codec") == value


@pytest.mark.parametrize("value", ["HEVC", "h265", "", None, 5, ["hevc"], {"a": 1}, True])
def test_invalid_codecs_fall_back_to_the_default(tmp_path, value):
    s = Settings(tmp_path / "c.json")
    s.update({"mirror": {"codec": "hevc"}})
    s.update({"mirror": {"codec": value}})
    assert s.get("mirror.codec") == "h264"
    p = tmp_path / "d.json"
    p.write_text(json.dumps({"version": SCHEMA_VERSION, "mirror": {"codec": value}}))
    assert Settings(p).get("mirror.codec") == "h264"


def test_v2_config_is_migrated_and_pinned_to_h264(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"version": 2, "mirror": {"preset": "cinema", "fps": 30}, "audio": {"source": "mic"}}))
    s = Settings(p)
    assert SCHEMA_VERSION == 3
    assert s.get("version") == 3 and s.get("mirror.codec") == "h264"
    assert s.get("mirror.preset") == "cinema" and s.get("mirror.fps") == 30 and s.get("audio.source") == "mic"


def test_v2_config_with_a_hand_written_codec_keeps_it(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"version": 2, "mirror": {"codec": "hevc"}}))
    assert Settings(p).get("mirror.codec") == "hevc"


def test_v2_config_without_a_mirror_section_gets_one_with_the_codec(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"version": 2}))
    assert Settings(p).get("mirror.codec") == "h264"


# ---------------------------------------------------------------------------------- controller
def test_state_reports_the_hevc_encoder(tmp_path):
    env = Env(tmp_path)
    env.caps = {"nvenc": True, "cuda_scale": True, "x264": True, "hevc_nvenc": True, "x265": True}

    async def go():
        assert env.ctl.state().hevc_encoder is None
        await env.ctl.start()
        await settle()
        return env.ctl.state().hevc_encoder, env.last("state").hevc_encoder

    assert run(go()) == ("hevc_nvenc", "hevc_nvenc")


@pytest.mark.parametrize("caps, expected", [
    ({"nvenc": False, "x264": True, "x265": True}, "libx265"),
    ({"nvenc": True, "x264": True}, ""),                          # ffmpeg without any HEVC encoder
])
def test_hevc_encoder_falls_back_to_software_or_is_reported_missing(tmp_path, caps, expected):
    env = Env(tmp_path)
    env.caps = caps

    async def go():
        await env.ctl.start()
        await settle()
        return env.ctl.state().hevc_encoder

    assert run(go()) == expected


def test_codec_option_reaches_the_session_and_is_saved(tmp_path):
    env = Env(tmp_path)

    async def go():
        await env.ctl.start()
        await env.ctl.devices_select("cast-salon")
        await env.ctl.dispatch("mirror.start", {"sourceId": "monitor:DP-4", "options": {"codec": "auto"}})
        await settle()
        assert FakeSession.instances[-1].opts.codec == "auto"
        await env.ctl.dispatch("mirror.update", {"options": {"codec": "hevc"}})
        assert FakeSession.instances[-1].opts.codec == "hevc"
        await env.ctl.dispatch("mirror.stop")

    run(go())
    assert env.settings.get("mirror.codec") == "hevc"


def test_default_session_uses_h264(tmp_path):
    env = Env(tmp_path)

    async def go():
        await env.ctl.start()
        await env.ctl.devices_select("cast-salon")
        await env.ctl.dispatch("mirror.start", {"sourceId": "monitor:DP-4"})
        await settle()
        return FakeSession.instances[-1].opts.codec

    assert run(go()) == "h264"


def test_unknown_codec_in_an_rpc_is_ignored(tmp_path):
    env = Env(tmp_path)

    async def go():
        await env.ctl.start()
        await env.ctl.dispatch("mirror.update", {"options": {"codec": "hevc"}})
        await env.ctl.dispatch("mirror.update", {"options": {"codec": "av1"}})

    run(go())
    assert env.settings.get("mirror.codec") == "hevc"
    assert env.ctl.mirror.options.codec == "hevc"


def test_settings_set_codec_applies_to_the_next_session(tmp_path):
    env = Env(tmp_path)

    async def go():
        await env.ctl.start()
        await env.ctl.dispatch("settings.set", {"patch": {"mirror": {"codec": "auto"}}})
        return env.ctl.mirror.options.codec

    assert run(go()) == "auto"
