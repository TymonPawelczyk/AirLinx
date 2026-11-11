"""ui/index.html against the built-in mock bridge (ui/bridge.js). No Python backend, no TV.

Selectors: roles, aria-labels and the data-* state hooks on `.app` (data-screen, data-session, data-mode).
Waiting is done on those states, never with sleeps.
"""
import json
import re

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def app(tab):
    return tab.page.locator(".app")


def mock_state(tab):
    return tab.page.evaluate("airlinx.mock.state()")


def open_ui(new_tab, ui_url, query="", **kw):
    tab = new_tab(ui_url + query, **kw)
    return tab


def to_work(tab):
    expect(app(tab)).to_have_attribute("data-screen", "work")


def share(tab):
    tab.page.get_by_role("button", name="Udostępnij ekran").click()


def assert_clean(tab):
    assert tab.problems == [], tab.problems
    assert tab.external == [], tab.external


# ----------------------------------------------------------------------------- mirror flow

def test_start_pick_device_share_live_stop(new_tab, ui_url):
    tab = open_ui(new_tab, ui_url, "?mock=multi")                         # a second TV to switch to
    p = tab.page
    expect(app(tab)).to_have_attribute("data-screen", "discovery")      # scanning first
    to_work(tab)
    expect(p.locator("#tv-name")).to_have_text("Salon")                  # last used device preselected

    p.get_by_role("button", name="Telewizor", exact=False).first.click()
    menu = p.get_by_role("menu", name="Telewizory")
    expect(menu).to_be_visible()
    menu.get_by_role("menuitemradio", name="Sypialnia").click()
    expect(p.locator("#tv-name")).to_have_text("Sypialnia")
    assert mock_state(tab)["selected_device_id"] == "cast-a03e77c1d2f84e90"

    share(tab)
    expect(app(tab)).to_have_attribute("data-session", "live")
    expect(p.get_by_text("Na żywo", exact=False).first).to_be_visible()
    # stats arrive from the mock every 500 ms: fps turns from the placeholder into a number
    expect(p.locator("#s-fps")).not_to_have_text("—")
    assert mock_state(tab)["mirror"]["state"] == "live"

    p.get_by_role("button", name="Zatrzymaj", exact=True).click()
    expect(app(tab)).to_have_attribute("data-session", "idle")
    assert mock_state(tab)["mirror"]["state"] == "idle"
    assert_clean(tab)


def test_default_mock_has_one_tv_and_neutral_files(new_tab, ui_url):
    tab = open_ui(new_tab, ui_url)
    p = tab.page
    to_work(tab)
    assert [d["name"] for d in mock_state(tab)["devices"]] == ["Salon"]
    p.get_by_role("button", name="Multimedia").click()
    p.locator(".dropzone").get_by_role("button", name="Wybierz pliki").click()
    expect(p.locator("#queue .q-row")).to_have_count(4)
    titles = p.locator("#queue .qn").all_inner_texts()
    assert titles == ["wakacje-2026", "spacer-nad-morzem", "koncert-w-ogrodzie", "zorza"]


def test_live_telemetry_matches_measured_game_profile(new_tab, ui_url):
    tab = open_ui(new_tab, ui_url)
    p = tab.page
    to_work(tab)
    share(tab)
    expect(app(tab)).to_have_attribute("data-session", "live")
    p.wait_for_function("airlinx.mock.state().mirror.stats")
    for _ in range(4):                                     # several samples, not one lucky draw
        st = mock_state(tab)["mirror"]["stats"]
        assert 105 <= st["latency_ms"] <= 117, st
        assert 59 <= st["fps"] <= 60 and 11.5e6 <= st["bitrate"] <= 12.2e6, st
        assert 2.5 <= st["rtt_ms"] <= 3.5 and st["loss"] == 0, st
        p.wait_for_function("(t) => JSON.stringify(airlinx.mock.state().mirror.stats) !== t", arg=json.dumps(st))
    expect(p.locator("#s-fps")).to_have_text(re.compile(r"^(59|60)$"))
    expect(p.locator("#s-proto")).to_contain_text(re.compile(r"RTT [23],\d ms"))


def test_share_goes_through_connecting_and_permission_states(new_tab, ui_url):
    tab = open_ui(new_tab, ui_url, "?mock=slow")        # slow mock: the transient states last long enough to observe
    to_work(tab)
    share(tab)
    expect(app(tab)).to_have_attribute("data-session", "connecting")
    expect(app(tab)).to_have_attribute("data-session", "permission")
    expect(tab.page.get_by_role("button", name="Anuluj")).to_be_visible()
    tab.page.locator(".f-permission").get_by_role("button", name="Anuluj").click()
    expect(app(tab)).to_have_attribute("data-session", "idle")


def test_connection_drop_shows_lost_then_recovers(new_tab, ui_url):
    tab = open_ui(new_tab, ui_url)
    to_work(tab)
    share(tab)
    expect(app(tab)).to_have_attribute("data-session", "live")
    tab.page.evaluate("airlinx.mock.dropConnection()")
    expect(app(tab)).to_have_attribute("data-session", "lost")
    expect(app(tab)).to_have_attribute("data-session", "live")


def test_session_failure_can_be_retried(new_tab, ui_url):
    tab = open_ui(new_tab, ui_url)
    to_work(tab)
    share(tab)
    expect(app(tab)).to_have_attribute("data-session", "live")
    tab.page.evaluate("airlinx.mock.mirrorError('Telewizor zamknął sesję.')")
    expect(app(tab)).to_have_attribute("data-session", "failed")
    expect(tab.page.locator("#failed-desc")).to_have_text("Telewizor zamknął sesję.")
    tab.page.get_by_role("button", name="Spróbuj ponownie").click()
    expect(app(tab)).to_have_attribute("data-session", "live")


def test_quality_profile_change_is_sent_to_backend(new_tab, ui_url):
    tab = open_ui(new_tab, ui_url)
    to_work(tab)
    group = tab.page.locator(".r-idle .seg[data-group=profile]")
    group.get_by_role("radio", name="Kino").click()
    expect(group.get_by_role("radio", name="Kino")).to_have_attribute("aria-checked", "true")
    tab.page.wait_for_function("airlinx.mock.state().settings.mirror.preset === 'cinema' "
                               "|| airlinx.mock.state().mirror.options.preset === 'cinema'")


WAYLAND_PL = "Sesja Wayland nie jest obsługiwana: kopia ekranu może być czarna."


def test_wayland_session_shows_a_persistent_note_in_screen_mode(new_tab, ui_url):
    tab = open_ui(new_tab, ui_url, "?mock=wayland")
    p = tab.page
    to_work(tab)
    note = p.locator("#wayland-note")
    expect(note).to_be_visible()
    expect(note).to_contain_text(WAYLAND_PL)
    expect(note).to_contain_text("Ubuntu on Xorg")
    share(tab)                                            # still allowed: the user may know better
    expect(app(tab)).to_have_attribute("data-session", "live")
    expect(note).to_be_visible()                          # stays while live, it is not a toast
    p.get_by_role("button", name="Multimedia").click()
    expect(note).to_be_hidden()                           # playing files works on Wayland
    assert_clean(tab)


def test_x11_session_has_no_wayland_note(new_tab, ui_url):
    tab = open_ui(new_tab, ui_url)
    to_work(tab)
    expect(tab.page.locator("#wayland-note")).to_be_hidden()


UNSTABLE_PL = "Wyższa rozdzielczość, na części telewizorów obraz może się zrywać."


def test_default_profile_is_game_with_its_measured_latency(new_tab, ui_url):
    tab = open_ui(new_tab, ui_url)
    to_work(tab)
    group = tab.page.locator(".r-idle .seg[data-group=profile]")
    expect(group.get_by_role("radio", name="Gra / praca")).to_have_attribute("aria-checked", "true")
    desc = tab.page.locator(".r-idle .profile-desc")
    expect(desc).to_contain_text("około 110 ms")
    expect(desc).not_to_contain_text("eksperymentalny")


@pytest.mark.parametrize("name", ["Balans", "Kino"])
def test_high_resolution_profiles_are_marked_experimental(new_tab, ui_url, name):
    tab = open_ui(new_tab, ui_url)
    p = tab.page
    to_work(tab)
    p.locator(".r-idle .seg[data-group=profile]").get_by_role("radio", name=name).click()
    desc = p.locator(".r-idle .profile-desc")
    expect(desc).to_contain_text("eksperymentalny")
    expect(desc).to_contain_text(UNSTABLE_PL)
    expect(desc).not_to_contain_text(" ms")                 # no latency promise nobody measured
    expect(p.locator("#idle-desc")).not_to_contain_text(" ms")
    expect(p.locator("#idle-badges")).to_contain_text("eksperymentalny")


def test_settings_profile_cards_promise_latency_only_for_game(new_tab, ui_url):
    tab = open_ui(new_tab, ui_url)
    to_work(tab)
    open_settings(tab, "Obraz")
    cards = tab.page.locator("[data-group=profile-card]")
    expect(cards.locator("[data-val=gra]")).to_contain_text("~110")
    for val in ("zrownowazony", "kino"):
        card = cards.locator(f"[data-val={val}]")
        expect(card).to_contain_text("eksperymentalny")
        expect(card).not_to_contain_text("ms")
    expect(tab.page.locator('[data-panel="zaawansowane"]')).not_to_contain_text("100–150 ms")


# ----------------------------------------------------------------------------- TV busy / no devices

def test_tv_busy_asks_before_interrupting(new_tab, ui_url):
    tab = open_ui(new_tab, ui_url, "?mock=busy")
    to_work(tab)
    share(tab)
    dlg = tab.page.get_by_role("alertdialog")
    expect(dlg).to_be_visible()
    expect(dlg.get_by_role("heading")).to_contain_text("Przerwać")
    expect(dlg).to_contain_text("YouTube")
    # declining leaves the TV alone
    dlg.get_by_role("button", name="Anuluj").click()
    expect(dlg).to_be_hidden()
    assert mock_state(tab)["mirror"]["state"] == "idle"
    # accepting retries with force and goes live
    share(tab)
    expect(dlg).to_be_visible()
    dlg.get_by_role("button", name="Przerwij i pokaż").click()
    expect(app(tab)).to_have_attribute("data-session", "live")
    assert_clean(tab)


def test_tv_busy_dialog_closes_on_escape_without_retry(new_tab, ui_url):
    tab = open_ui(new_tab, ui_url, "?mock=busy")
    to_work(tab)
    share(tab)
    expect(tab.page.get_by_role("alertdialog")).to_be_visible()
    tab.page.keyboard.press("Escape")
    expect(tab.page.get_by_role("alertdialog")).to_be_hidden()
    assert mock_state(tab)["mirror"]["state"] == "idle"


def test_no_devices_screen_and_rescan(new_tab, ui_url):
    tab = open_ui(new_tab, ui_url, "?mock=nodevices")
    expect(app(tab)).to_have_attribute("data-screen", "notfound")
    expect(tab.page.get_by_role("heading", name="Nie widzę telewizora")).to_be_visible()
    expect(tab.page.get_by_role("button", name="Szukaj ponownie")).to_be_visible()
    tab.page.get_by_role("button", name="Szukaj ponownie").click()
    expect(app(tab)).to_have_attribute("data-screen", "discovery")      # scanning again
    expect(app(tab)).to_have_attribute("data-screen", "notfound")
    assert_clean(tab)


def test_manual_ip_validation_and_add(new_tab, ui_url):
    tab = open_ui(new_tab, ui_url, "?mock=nodevices")
    p = tab.page
    expect(app(tab)).to_have_attribute("data-screen", "notfound")
    p.get_by_role("button", name="Dodaj ręcznie (IP)").click()
    p.get_by_label("Adres IP telewizora").fill("nie-ip")
    p.get_by_role("button", name="Połącz").click()
    expect(p.locator("#manual-hint")).to_contain_text("192.168.0.20")
    p.get_by_label("Adres IP telewizora").fill("192.168.0.55")
    p.get_by_role("button", name="Połącz").click()
    expect(app(tab)).to_have_attribute("data-screen", "work")
    expect(p.locator("#tv-name")).to_have_text("TV 192.168.0.55")


# ----------------------------------------------------------------------------- media

def test_media_add_play_pause_resume_stop(new_tab, ui_url):
    tab = open_ui(new_tab, ui_url)
    p = tab.page
    to_work(tab)
    p.get_by_role("button", name="Multimedia").click()
    expect(app(tab)).to_have_attribute("data-mode", "media")
    expect(p.get_by_text("Kolejka jest pusta")).to_be_visible()

    p.locator(".dropzone").get_by_role("button", name="Wybierz pliki").click()   # mock: 4 fake files
    queue = p.locator("#queue .q-row")
    expect(queue).to_have_count(4)
    expect(queue.first).to_contain_text("wakacje-2026")

    queue.first.get_by_role("button").first.click()
    play = p.get_by_role("button", name="Pauza")                  # label flips when playing
    expect(play).to_be_visible()
    play.click()
    expect(p.get_by_role("button", name="Odtwórz", exact=True)).to_be_visible()
    assert mock_state(tab)["playback"]["state"] == "paused"
    p.get_by_role("button", name="Odtwórz", exact=True).click()
    expect(p.get_by_role("button", name="Pauza")).to_be_visible()

    p.get_by_role("button", name="Następny").click()
    p.wait_for_function("airlinx.mock.state().queue.findIndex(x => x.id === airlinx.mock.state().playback.item_id) === 1")
    p.get_by_role("button", name="Zatrzymaj odtwarzanie").click()
    p.wait_for_function("airlinx.mock.state().playback.state === 'idle'")
    assert_clean(tab)


def test_media_remove_item(new_tab, ui_url):
    tab = open_ui(new_tab, ui_url)
    to_work(tab)
    tab.page.get_by_role("button", name="Multimedia").click()
    tab.page.locator(".dropzone").get_by_role("button", name="Wybierz pliki").click()
    expect(tab.page.locator("#queue .q-row")).to_have_count(4)
    tab.page.get_by_role("button", name="Usuń z kolejki: Zorza", exact=False).click()
    expect(tab.page.locator("#queue .q-row")).to_have_count(3)


def test_media_url_form_adds_item(new_tab, ui_url):
    tab = open_ui(new_tab, ui_url)
    to_work(tab)
    tab.page.get_by_role("button", name="Multimedia").click()
    tab.page.get_by_label("Albo odtwórz z adresu").fill("http://serwer/film.mkv")
    tab.page.get_by_role("button", name="Odtwórz", exact=True).click()
    tab.page.wait_for_function("airlinx.mock.state().queue.some(x => x.path_or_url === 'http://serwer/film.mkv')")


# ----------------------------------------------------------------------------- settings

def open_settings(tab, section=None):
    tab.page.get_by_role("button", name="Ustawienia", exact=True).first.click()
    expect(app(tab)).to_have_attribute("data-screen", "settings")
    if section:
        tab.page.get_by_role("tab", name=section).click()


def test_settings_tabs_and_back(new_tab, ui_url):
    tab = open_ui(new_tab, ui_url)
    to_work(tab)
    open_settings(tab)
    for name, panel in [("Obraz", "obraz"), ("Dźwięk", "dzwiek"), ("Zaawansowane", "zaawansowane"), ("Ogólne", "ogolne")]:
        tab.page.get_by_role("tab", name=name).click()
        expect(tab.page.locator(f'[data-panel="{panel}"]')).to_be_visible()
    tab.page.locator("[data-act=back]").click()
    expect(app(tab)).to_have_attribute("data-screen", "work")


def test_settings_switch_and_protocol_persist_to_backend(new_tab, ui_url):
    tab = open_ui(new_tab, ui_url)
    to_work(tab)
    open_settings(tab)
    sw = tab.page.get_by_role("switch", name="Powiadomienia")
    expect(sw).to_have_attribute("aria-checked", "true")
    sw.click()
    expect(sw).to_have_attribute("aria-checked", "false")
    tab.page.wait_for_function("airlinx.mock.state().settings.notifications === false")
    tab.page.get_by_role("tab", name="Zaawansowane").click()
    tab.page.locator("[data-group=proto] [data-val=dlna]").click()
    tab.page.wait_for_function("airlinx.mock.state().settings.protocol === 'dlna'")


def test_settings_show_version_from_backend(new_tab, ui_url):
    tab = open_ui(new_tab, ui_url)
    to_work(tab)
    open_settings(tab)
    expect(tab.page.locator("#app-version")).to_have_text("AirLinx 0.1.0")


@pytest.mark.parametrize("query, label", [("", "NVENC, wybierany automatycznie"),
                                          ("?mock=cpu", "libx264, wybierany automatycznie")])
def test_settings_encoder_label_follows_detected_encoder(new_tab, ui_url, query, label):
    tab = open_ui(new_tab, ui_url, query)
    to_work(tab)
    open_settings(tab, "Obraz")
    expect(tab.page.locator("#encoder-info")).to_have_text(label)
    if query:
        expect(tab.page.locator("#encoder-desc")).to_contain_text("procesor")
    assert_clean(tab)


AUDIO_MODES = [("system", "Dźwięk systemu"), ("mic", "Mikrofon"), ("system_mic", "System + mikrofon"),
               ("app", "Wybrana aplikacja")]


def audio_group(tab):
    return tab.page.locator('[data-group="asrc"]')


def test_audio_source_has_four_modes_and_defaults_to_system(new_tab, ui_url):
    tab = open_ui(new_tab, ui_url)
    to_work(tab)
    open_settings(tab, "Dźwięk")
    radios = audio_group(tab).get_by_role("radio")
    expect(radios).to_have_count(4)
    for val, label in AUDIO_MODES:
        expect(audio_group(tab).locator(f'[data-val="{val}"]')).to_contain_text(label)
    expect(audio_group(tab).locator('[data-val="system"]')).to_have_attribute("aria-checked", "true")
    expect(tab.page.locator("#asrc-mic")).to_be_hidden()
    expect(tab.page.locator("#asrc-app")).to_be_hidden()


def test_audio_microphone_list_comes_from_backend(new_tab, ui_url):
    tab = open_ui(new_tab, ui_url)
    to_work(tab)
    open_settings(tab, "Dźwięk")
    audio_group(tab).locator('[data-val="mic"]').click()
    sel = tab.page.get_by_role("combobox", name="Mikrofon")
    expect(sel).to_be_visible()
    expect(sel.locator("option")).to_have_text(["Domyślny mikrofon (Blue Yeti)", "Blue Yeti",
                                                "Wbudowany mikrofon (karta dźwiękowa)"])
    sel.select_option(label="Wbudowany mikrofon (karta dźwiękowa)")
    tab.page.wait_for_function(
        "airlinx.mock.state().settings.audio.source === 'mic' && airlinx.mock.state().settings.audio.mic.startsWith('alsa_input.pci')")


def test_audio_app_list_select_and_refresh(new_tab, ui_url):
    tab = open_ui(new_tab, ui_url)
    to_work(tab)
    open_settings(tab, "Dźwięk")
    audio_group(tab).locator('[data-val="app"]').click()
    apps = tab.page.locator("#app-list")
    expect(apps.get_by_role("radio")).to_have_count(3)
    expect(apps.get_by_role("radio", name="Firefox")).to_contain_text("2 strumienie")
    # no app is chosen until the user picks one, and the summary in the rail says so
    expect(apps.get_by_role("radio", name="Firefox")).to_have_attribute("aria-checked", "false")
    apps.get_by_role("radio", name="Spotify").click()
    tab.page.wait_for_function("airlinx.mock.state().settings.audio.app === 'spotify'")
    expect(apps.get_by_role("radio", name="Spotify")).to_have_attribute("aria-checked", "true")
    # a new app starts playing: Refresh picks it up
    tab.page.evaluate("airlinx.mock.setAudioApps([{key:'vlc',name:'VLC',icon:null,icon_url:null,streams:1}])")
    tab.page.get_by_role("button", name="Odśwież").click()
    expect(apps.get_by_role("radio", name="VLC")).to_be_visible()
    # Spotify, still selected, is now listed as gone
    expect(apps.get_by_role("radio", name="Spotify")).to_contain_text("nie gra teraz")


def test_audio_empty_app_and_mic_lists(new_tab, ui_url):
    tab = open_ui(new_tab, ui_url, "?mock=noapps,nomic")
    to_work(tab)
    open_settings(tab, "Dźwięk")
    audio_group(tab).locator('[data-val="app"]').click()
    expect(tab.page.locator("#app-list")).to_contain_text("Żadna aplikacja nic teraz nie odtwarza")
    audio_group(tab).locator('[data-val="mic"]').click()
    expect(tab.page.get_by_role("combobox", name="Mikrofon").locator("option")).to_have_text(["Domyślny mikrofon"])


def test_audio_fallback_to_system_is_announced_when_app_is_gone(new_tab, ui_url):
    tab = open_ui(new_tab, ui_url)
    to_work(tab)
    open_settings(tab, "Dźwięk")
    audio_group(tab).locator('[data-val="app"]').click()
    tab.page.locator("#app-list").get_by_role("radio", name="Spotify").click()
    tab.page.evaluate("airlinx.mock.setAudioApps([])")           # Spotify stops playing before the share starts
    tab.page.locator("[data-act=back]").click()
    share(tab)
    expect(app(tab)).to_have_attribute("data-session", "live")
    expect(tab.page.locator("#toasts")).to_contain_text("nic teraz nie odtwarza")
    summary = tab.page.locator(".r-live .a-sum")
    expect(summary).to_have_attribute("data-warn", "")
    expect(summary).to_contain_text("System")


def test_audio_source_disabled_when_sound_is_off(new_tab, ui_url):
    tab = open_ui(new_tab, ui_url)
    to_work(tab)
    tab.page.locator(".r-idle").get_by_role("switch", name="Dźwięk na TV").click()
    open_settings(tab, "Dźwięk")
    expect(tab.page.locator("#asrc-off")).to_be_visible()
    for val, _ in AUDIO_MODES:
        expect(audio_group(tab).locator(f'[data-val="{val}"]')).to_have_attribute("aria-disabled", "true")
    # clicks on disabled cards do nothing
    audio_group(tab).locator('[data-val="mic"]').click(force=True)
    assert mock_state(tab)["settings"]["audio"]["source"] == "system"
    # the summary in the main rail says so too
    tab.page.locator("[data-act=back]").click()
    expect(tab.page.locator(".r-idle .a-sum")).to_contain_text("Wyłączony")
    # turning it back on re-enables the cards
    tab.page.locator(".r-idle").get_by_role("switch", name="Dźwięk na TV").click()
    open_settings(tab, "Dźwięk")
    expect(audio_group(tab).locator('[data-val="mic"]')).not_to_have_attribute("aria-disabled", "true")


def test_audio_summary_button_opens_audio_settings(new_tab, ui_url):
    tab = open_ui(new_tab, ui_url)
    to_work(tab)
    tab.page.locator(".r-idle .a-sum").click()
    expect(app(tab)).to_have_attribute("data-screen", "settings")
    expect(tab.page.locator('[data-panel="dzwiek"]')).to_be_visible()
    expect(tab.page.locator('[data-group="asrc"] [aria-checked="true"]')).to_be_focused()


def test_audio_change_while_live_updates_now_playing_line(new_tab, ui_url):
    tab = open_ui(new_tab, ui_url)
    to_work(tab)
    share(tab)
    expect(app(tab)).to_have_attribute("data-session", "live")
    open_settings(tab, "Dźwięk")
    expect(tab.page.locator("#asrc-now-t")).to_have_text("Teraz: Dźwięk systemu")
    audio_group(tab).locator('[data-val="system_mic"]').click()
    expect(tab.page.locator("#asrc-now-t")).to_have_text("Teraz: System + mikrofon")


def test_audio_dlna_change_while_live_is_deferred(new_tab, ui_url):
    tab = open_ui(new_tab, ui_url)
    to_work(tab)
    open_settings(tab, "Zaawansowane")
    tab.page.locator("[data-group=proto] [data-val=dlna]").click()
    tab.page.locator("[data-act=back]").click()
    share(tab)
    expect(app(tab)).to_have_attribute("data-session", "live")
    open_settings(tab, "Dźwięk")
    audio_group(tab).locator('[data-val="system_mic"]').click()
    expect(tab.page.locator("#asrc-now-t")).to_contain_text("następnym udostępnianiu")


# ----------------------------------------------------------------------------- keyboard / focus / theme

def test_keyboard_arrows_move_radio_selection(new_tab, ui_url):
    tab = open_ui(new_tab, ui_url)
    to_work(tab)
    group = tab.page.locator(".r-idle .seg[data-group=profile]")
    group.get_by_role("radio", name="Gra / praca").focus()
    tab.page.keyboard.press("ArrowRight")
    expect(group.get_by_role("radio", name="Balans")).to_be_focused()
    expect(group.get_by_role("radio", name="Balans")).to_have_attribute("aria-checked", "true")
    tab.page.keyboard.press("ArrowLeft")
    tab.page.keyboard.press("ArrowLeft")                         # wraps around
    expect(group.get_by_role("radio", name="Kino")).to_be_focused()


def test_device_menu_keyboard_and_focus_return(new_tab, ui_url):
    tab = open_ui(new_tab, ui_url, "?mock=multi")
    to_work(tab)
    pill = tab.page.get_by_role("button", name="Telewizor", exact=False).first
    pill.focus()
    tab.page.keyboard.press("Enter")
    expect(pill).to_have_attribute("aria-expanded", "true")
    expect(tab.page.get_by_role("menu", name="Telewizory")).to_be_visible()
    tab.page.get_by_role("menuitemradio", name="Salon").focus()
    tab.page.keyboard.press("ArrowDown")
    expect(tab.page.get_by_role("menuitemradio", name="Sypialnia")).to_be_focused()
    tab.page.keyboard.press("Escape")
    expect(pill).to_have_attribute("aria-expanded", "false")
    expect(pill).to_be_focused()


def test_device_menu_moves_focus_into_menu_on_open(new_tab, ui_url):
    tab = open_ui(new_tab, ui_url)
    to_work(tab)
    pill = tab.page.get_by_role("button", name="Telewizor", exact=False).first
    pill.focus()
    tab.page.keyboard.press("Enter")
    expect(tab.page.get_by_role("menuitemradio", name="Salon")).to_be_focused()


def open_picker(tab):
    opener = tab.page.locator(".r-idle [data-act=picker]")
    opener.click()
    dlg = tab.page.get_by_role("dialog", name=re.compile("Co pokazać"))
    expect(dlg).to_be_visible()
    return opener, dlg


def test_source_picker_traps_focus_and_restores_it(new_tab, ui_url):
    tab = open_ui(new_tab, ui_url, reduced_motion=False)
    to_work(tab)
    opener, dlg = open_picker(tab)
    expect(dlg.get_by_role("radio")).to_have_count(2)             # two monitors in the mock
    expect(dlg.get_by_role("tab", selected=True)).to_be_focused()
    for _ in range(12):                                           # Tab never leaves the dialog
        tab.page.keyboard.press("Tab")
        assert tab.page.evaluate("!!document.activeElement.closest('#picker')")
    tab.page.keyboard.press("Escape")
    expect(dlg).to_be_hidden()
    expect(opener).to_be_focused()


def test_source_picker_takes_focus_with_reduced_motion(new_tab, ui_url):
    tab = open_ui(new_tab, ui_url, reduced_motion=True)
    to_work(tab)
    _, dlg = open_picker(tab)
    expect(dlg.get_by_role("tab", selected=True)).to_be_focused()


def test_escape_leaves_settings(new_tab, ui_url):
    tab = open_ui(new_tab, ui_url)
    to_work(tab)
    open_settings(tab)
    tab.page.keyboard.press("Escape")
    expect(app(tab)).to_have_attribute("data-screen", "work")


def test_every_interactive_control_has_an_accessible_name(new_tab, ui_url):
    tab = open_ui(new_tab, ui_url)
    to_work(tab)
    nameless = tab.page.evaluate("""() => [...document.querySelectorAll('button, input, select, [role=switch], [role=radio]')]
        .filter(e => e.offsetParent !== null && e.type !== 'hidden')
        .filter(e => !(e.getAttribute('aria-label') || e.getAttribute('aria-labelledby') || e.textContent.trim()
                       || (e.labels && e.labels.length) || e.title))
        .map(e => e.outerHTML.slice(0, 120))""")
    assert nameless == []


def test_light_theme_from_settings(new_tab, ui_url):
    tab = open_ui(new_tab, ui_url)
    to_work(tab)
    expect(tab.page.locator("html")).to_have_attribute("data-theme", "dark")
    dark_fg = tab.page.evaluate("getComputedStyle(document.querySelector('.app')).color")
    open_settings(tab)
    tab.page.locator('[data-group="theme"]').get_by_role("radio", name="Jasny").click()
    expect(tab.page.locator("html")).to_have_attribute("data-theme", "light")
    # colors transition, so wait for the computed value to change
    tab.page.wait_for_function("(d) => getComputedStyle(document.querySelector('.app')).color !== d", arg=dark_fg)
    tab.page.wait_for_function("airlinx.mock.state().settings.ui.theme === 'light'")
    assert_clean(tab)


def test_light_theme_url_override_renders_without_errors(new_tab, ui_url):
    tab = open_ui(new_tab, ui_url, "?theme=light")
    to_work(tab)
    expect(tab.page.locator("html")).to_have_attribute("data-theme", "light")
    share(tab)
    expect(app(tab)).to_have_attribute("data-session", "live")
    assert_clean(tab)


def test_no_console_errors_across_all_screens(new_tab, ui_url):
    tab = open_ui(new_tab, ui_url)
    p = tab.page
    to_work(tab)
    open_settings(tab)
    for name in ("Obraz", "Dźwięk", "Zaawansowane"):
        p.get_by_role("tab", name=name).click()
    p.locator("[data-act=back]").click()
    p.get_by_role("button", name="Multimedia").click()
    p.get_by_role("button", name="Ekran", exact=True).click()
    share(tab)
    expect(app(tab)).to_have_attribute("data-session", "live")
    p.get_by_role("button", name="Szczegóły").click()
    expect(p.locator("#details-panel")).to_be_visible()
    assert_clean(tab)


def test_small_window_has_no_horizontal_overflow(new_tab, ui_url):
    tab = open_ui(new_tab, ui_url, viewport={"width": 900, "height": 600})
    to_work(tab)
    assert tab.page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
