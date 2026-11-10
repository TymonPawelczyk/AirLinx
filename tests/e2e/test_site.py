"""Landing page in site/ served by a local static server. Offline: any non-local request is a failure."""
import re

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e

PLACEHOLDER = "{{REPO_URL}}"
SHIPPED_URL = "https://github.com/TymonPawelczyk/AirLinx"
REAL_URL = "https://example.test/acme/AirLinx"


def open_site(new_tab, site_url, **kw):
    tab = new_tab(site_url, **kw)
    tab.page.wait_for_load_state("load")
    return tab


def test_no_console_errors_and_no_external_requests(new_tab, site_url):
    tab = open_site(new_tab, site_url)
    # lazy images and fonts load on scroll: walk the whole page so every request is made
    h = tab.page.evaluate("document.documentElement.scrollHeight")
    for y in range(0, h, 600):
        tab.page.mouse.wheel(0, 600)
    tab.page.wait_for_load_state("networkidle")
    assert tab.problems == [], tab.problems
    assert tab.external == [], f"landing page must not call third parties: {tab.external}"


@pytest.mark.parametrize("width", [360, 1440])
def test_no_horizontal_scroll(new_tab, site_url, width):
    tab = open_site(new_tab, site_url, viewport={"width": width, "height": 900})
    tab.page.wait_for_load_state("networkidle")
    sw, cw = tab.page.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
    assert sw <= cw, f"page is {sw}px wide in a {cw}px viewport"
    wide = tab.page.evaluate("""() => [...document.querySelectorAll('body *')]
        .filter(e => e.getBoundingClientRect().right > document.documentElement.clientWidth + 1
                     && !e.closest('pre, [aria-hidden=true], .screen-glow') && getComputedStyle(e).position !== 'fixed')
        .slice(0, 5).map(e => e.tagName + '.' + e.className)""")
    assert wide == [], f"elements sticking out of the viewport: {wide}"


def test_anchor_links_all_resolve(new_tab, site_url):
    tab = open_site(new_tab, site_url)
    broken = tab.page.evaluate("""() => [...document.querySelectorAll('a[href^="#"]')]
        .map(a => a.getAttribute('href')).filter(h => h.length > 1 && h !== '#top' && !document.getElementById(h.slice(1)))""")
    assert broken == []


@pytest.mark.parametrize("link,section", [("Jak to działa", "jak-to-dziala"), ("Wymagania", "wymagania"),
                                          ("Instalacja", "instalacja"), ("Pytania", "faq")])
def test_header_nav_scrolls_to_section(new_tab, site_url, link, section):
    tab = open_site(new_tab, site_url, viewport={"width": 1440, "height": 900})
    tab.page.get_by_role("navigation", name="Sekcje strony").get_by_role("link", name=link).click()
    expect(tab.page).to_have_url(re.compile(f"#{section}$"))
    tab.page.wait_for_function(
        "(id) => { const r = document.getElementById(id).getBoundingClientRect(); return r.top < innerHeight && r.bottom > 0 }",
        arg=section)


def test_skip_link_moves_focus_to_main(new_tab, site_url):
    tab = open_site(new_tab, site_url)
    tab.page.keyboard.press("Tab")
    skip = tab.page.get_by_role("link", name="Przejdź do treści")
    expect(skip).to_be_focused()
    tab.page.keyboard.press("Enter")
    expect(tab.page.locator("main#tresc")).to_be_focused()


def test_primary_cta_points_at_install_section(new_tab, site_url):
    tab = open_site(new_tab, site_url)
    cta = tab.page.get_by_role("link", name="Zainstaluj AirLinx")
    expect(cta).to_have_attribute("href", "#instalacja")


# ----------------------------------------------------------------------------- copy buttons

def test_copy_buttons_put_the_command_on_the_clipboard(new_tab, site_url):
    tab = open_site(new_tab, site_url, permissions=["clipboard-read", "clipboard-write"])
    p = tab.page
    blocks = p.locator("figure.code")
    assert blocks.count() >= 3
    for i in range(blocks.count()):
        fig = blocks.nth(i)
        want = fig.locator("pre code").inner_text()
        btn = fig.get_by_role("button", name=re.compile("^Kopiuj polecenie"))
        btn.scroll_into_view_if_needed()
        btn.click()
        expect(btn).to_contain_text("Skopiowano")
        expect(p.locator("[data-copy-status]")).to_have_text("Skopiowano do schowka.")
        assert p.evaluate("navigator.clipboard.readText()") == want
    # the label resets by itself
    expect(btn).to_contain_text("Kopiuj", timeout=4000)
    expect(btn).not_to_contain_text("Skopiowano")


def test_copy_button_falls_back_to_a_message_when_clipboard_fails(new_tab, site_url):
    tab = open_site(new_tab, site_url)
    p = tab.page
    p.evaluate("void (navigator.clipboard.writeText = () => Promise.reject(new Error('denied')))")
    btn = p.locator("figure.code").first.get_by_role("button", name=re.compile("^Kopiuj polecenie"))
    btn.scroll_into_view_if_needed()
    btn.click()
    expect(btn).to_contain_text("Zaznacz i skopiuj")
    expect(p.locator("[data-copy-status]")).to_contain_text("Schowek niedostępny")
    # the command is pre-selected so Ctrl+C works
    assert p.evaluate("getSelection().toString()") == p.locator("figure.code").first.locator("pre code").inner_text()


# ----------------------------------------------------------------------------- theme

def test_theme_toggle_flips_theme_label_and_storage(new_tab, site_url):
    tab = open_site(new_tab, site_url, scheme="dark")
    p = tab.page
    html = p.locator("html")
    toggle = p.get_by_role("button", name=re.compile("^Włącz motyw"))
    expect(toggle).to_have_attribute("aria-label", "Włącz motyw jasny")
    toggle.click()
    expect(html).to_have_attribute("data-theme", "light")
    expect(toggle).to_have_attribute("aria-label", "Włącz motyw ciemny")
    assert p.evaluate("localStorage.getItem('airlinx-theme')") == "light"
    toggle.click()
    expect(html).to_have_attribute("data-theme", "dark")
    assert p.evaluate("localStorage.getItem('airlinx-theme')") == "dark"


def test_stored_theme_wins_over_system_preference_and_survives_reload(new_tab, site_url):
    tab = open_site(new_tab, site_url, scheme="dark")
    tab.page.evaluate("localStorage.setItem('airlinx-theme', 'light')")
    tab.page.reload()
    expect(tab.page.locator("html")).to_have_attribute("data-theme", "light")
    bg_light = tab.page.evaluate("getComputedStyle(document.body).backgroundColor")
    tab.page.evaluate("localStorage.setItem('airlinx-theme', 'dark')")
    tab.page.reload()
    expect(tab.page.locator("html")).to_have_attribute("data-theme", "dark")
    assert tab.page.evaluate("getComputedStyle(document.body).backgroundColor") != bg_light


def test_system_light_preference_is_used_without_stored_choice(new_tab, site_url):
    tab = open_site(new_tab, site_url, scheme="light")
    toggle = tab.page.get_by_role("button", name=re.compile("^Włącz motyw"))
    expect(toggle).to_have_attribute("aria-label", "Włącz motyw ciemny")


def test_theme_still_works_when_storage_is_blocked(new_tab, site_url):
    tab = new_tab(site_url)
    tab.page.add_init_script("Object.defineProperty(window, 'localStorage', {get() { throw new Error('blocked') }})")
    tab.page.reload()
    tab.page.get_by_role("button", name=re.compile("^Włącz motyw")).click()
    expect(tab.page.locator("html")).to_have_attribute("data-theme", "light")


# ----------------------------------------------------------------------------- repo URL

def page_with_repo_url(browser, site_url, value):
    """A page whose <html data-repo-url> is replaced with `value` before the browser sees it."""
    ctx = browser.new_context(viewport={"width": 1100, "height": 800})
    page = ctx.new_page()
    page.set_default_timeout(8000)

    def patch_html(route):
        resp = route.fetch()
        body = re.sub(r'data-repo-url="[^"]*"', lambda _: f'data-repo-url="{value}"', resp.text())
        route.fulfill(response=resp, body=body)
    page.route(re.compile(r"/(index\.html)?$"), patch_html)
    page.goto(site_url)
    return ctx, page


def test_placeholder_stays_visible_and_nothing_links_to_it(site_url, browser):
    ctx, p = page_with_repo_url(browser, site_url, PLACEHOLDER)
    try:
        expect(p.locator("[data-repo-text]").first).to_have_text(PLACEHOLDER)
        expect(p.locator("figure.code", has_text="git clone")).to_contain_text(f"git clone {PLACEHOLDER}")
        hrefs = p.evaluate("[...document.querySelectorAll('a[href]')].map(a => a.getAttribute('href'))")
        assert not any("{{" in h or "REPO_URL" in h for h in hrefs)
        # without a real URL the secondary CTA keeps pointing inside the page and the footer link stays hidden
        cta = p.locator("[data-repo-cta]")
        expect(cta).to_have_attribute("href", "#jak-to-dziala")
        expect(cta).to_have_text("Jak to działa")
        expect(p.locator("[data-repo-item]")).to_be_hidden()
    finally:
        ctx.close()


def test_shipped_page_points_at_the_public_repository(new_tab, site_url):
    """The page as committed, with nothing substituted."""
    tab = open_site(new_tab, site_url)
    p = tab.page
    expect(p.locator("html")).to_have_attribute("data-repo-url", SHIPPED_URL)
    expect(p.locator("[data-repo-text]").first).to_have_text(SHIPPED_URL)
    expect(p.locator("figure.code", has_text="git clone")).to_contain_text(f"git clone {SHIPPED_URL}")
    cta = p.locator("[data-repo-cta]")
    expect(cta).to_have_attribute("href", SHIPPED_URL)
    expect(cta).to_have_text("Kod źródłowy")
    expect(p.locator("[data-repo-item]")).to_be_visible()
    assert PLACEHOLDER not in p.content()


def test_real_repo_url_is_substituted_everywhere(site_url, browser):
    ctx, page = page_with_repo_url(browser, site_url, REAL_URL)
    try:
        expect(page.locator("[data-repo-text]").first).to_have_text(REAL_URL)
        expect(page.locator("figure.code", has_text="git clone")).to_contain_text(f"git clone {REAL_URL}")
        cta = page.locator("[data-repo-cta]")
        expect(cta).to_have_attribute("href", REAL_URL)
        expect(cta).to_have_text("Kod źródłowy")
        expect(page.locator("[data-repo-item]")).to_be_visible()
        expect(page.locator("[data-repo-link]")).to_have_attribute("href", REAL_URL)
    finally:
        ctx.close()


def test_non_http_repo_url_is_not_turned_into_a_link(site_url, browser):
    """A malformed data-repo-url (e.g. javascript:) must never become a link target."""
    ctx, page = page_with_repo_url(browser, site_url, "javascript:alert(1)")
    try:
        expect(page.locator("[data-repo-cta]")).to_have_attribute("href", "#jak-to-dziala")
        expect(page.locator("[data-repo-item]")).to_be_hidden()
    finally:
        ctx.close()


# ----------------------------------------------------------------------------- images and accessibility

def test_every_image_has_an_alt_attribute_and_informative_ones_describe(new_tab, site_url):
    tab = open_site(new_tab, site_url)
    imgs = tab.page.evaluate("""() => [...document.images].map(i => ({src: i.getAttribute('src'),
        alt: i.getAttribute('alt'), decorative: !!i.closest('[aria-hidden=true]')}))""")
    assert imgs
    assert [i["src"] for i in imgs if i["alt"] is None] == []
    informative = [i for i in imgs if not i["decorative"] and "screen-glow" not in (i["src"] or "")]
    # an informative image with an empty alt is only fine when its twin (dark/light variant) is described
    empty = [i["src"] for i in informative if i["alt"] == ""]
    described = {i["src"] for i in informative if i["alt"]}
    assert len(described) >= 5, "screenshots must carry real descriptions"
    assert all(len(i["alt"]) > 15 for i in informative if i["alt"]), "alt text too short to be useful"
    assert len(empty) <= 2, empty


def test_images_actually_load(new_tab, site_url):
    tab = open_site(new_tab, site_url)
    tab.page.evaluate("document.querySelectorAll('img[loading=lazy]').forEach(i => i.loading = 'eager')")
    tab.page.wait_for_load_state("networkidle")
    broken = tab.page.evaluate("[...document.images].filter(i => i.complete && i.naturalWidth === 0).map(i => i.getAttribute('src'))")
    assert broken == []


def test_landmarks_headings_and_names(new_tab, site_url):
    tab = open_site(new_tab, site_url)
    p = tab.page
    assert p.get_by_role("banner").count() == 1
    assert p.get_by_role("main").count() == 1
    assert p.get_by_role("contentinfo").count() == 1
    assert p.get_by_role("navigation").count() >= 2
    # every nav has a distinct label
    labels = p.evaluate("[...document.querySelectorAll('nav')].map(n => n.getAttribute('aria-label'))")
    assert all(labels) and len(set(labels)) == len(labels)
    assert p.evaluate("document.documentElement.lang") == "pl"
    assert p.title().strip() != ""
    assert p.locator('meta[name="description"]').get_attribute("content")

    h1 = p.get_by_role("heading", level=1)
    assert h1.count() == 1
    levels = p.evaluate("[...document.querySelectorAll('h1,h2,h3,h4,h5,h6')].map(h => +h.tagName[1])")
    assert levels[0] == 1
    assert all(b - a <= 1 for a, b in zip(levels, levels[1:])), f"heading levels skip: {levels}"


def test_interactive_elements_have_accessible_names_and_ids_are_unique(new_tab, site_url):
    tab = open_site(new_tab, site_url)
    bad = tab.page.evaluate("""() => {
        const name = e => (e.getAttribute('aria-label') || e.textContent || '').trim()
                          || (e.getAttribute('aria-labelledby') ? [...e.getAttribute('aria-labelledby').split(' ')]
                              .map(i => (document.getElementById(i) || {}).textContent || '').join('').trim() : '')
                          || (e.querySelector('img[alt]:not([alt=""])') ? 'img' : '');
        return [...document.querySelectorAll('a[href], button, summary, input, select, textarea')]
            .filter(e => !name(e)).map(e => e.outerHTML.slice(0, 100));
    }""")
    assert bad == []
    dup = tab.page.evaluate("""() => { const seen = {}, d = [];
        document.querySelectorAll('[id]').forEach(e => { if (seen[e.id]) d.push(e.id); seen[e.id] = 1; }); return d; }""")
    assert dup == []
    dangling = tab.page.evaluate("""() => [...document.querySelectorAll('[aria-labelledby],[aria-controls],[aria-describedby]')]
        .flatMap(e => ['aria-labelledby', 'aria-controls', 'aria-describedby'].flatMap(a => (e.getAttribute(a) || '').split(' ').filter(Boolean)))
        .filter(id => !document.getElementById(id))""")
    assert dangling == []


def test_buttons_and_links_are_reachable_and_show_focus(new_tab, site_url):
    tab = open_site(new_tab, site_url)
    p = tab.page
    seen = set()
    for _ in range(14):
        p.keyboard.press("Tab")
        info = p.evaluate("""() => { const e = document.activeElement, cs = getComputedStyle(e);
            return {tag: e.tagName, label: (e.getAttribute('aria-label') || e.textContent || '').trim().slice(0, 30),
                    outline: cs.outlineStyle !== 'none' && parseFloat(cs.outlineWidth) > 0, shadow: cs.boxShadow !== 'none'} }""")
        seen.add(info["label"])
        assert info["outline"] or info["shadow"], f"no visible focus indicator on {info}"
    assert {"Zainstaluj AirLinx"} <= seen


def test_faq_items_toggle(new_tab, site_url):
    tab = open_site(new_tab, site_url)
    items = tab.page.locator("#faq details")
    if items.count() == 0:
        pytest.skip("FAQ is not built from <details>")
    first = items.first
    first.locator("summary").click()
    expect(first).to_have_attribute("open", "")
    first.locator("summary").click()
    expect(first).not_to_have_attribute("open", "")
