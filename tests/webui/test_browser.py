"""Drive the page shell in headless Chromium against a real ``serve()`` on a TreeDrive."""

from __future__ import annotations

import io
import os
import threading
from datetime import datetime

import pytest
from tests.usage.conftest import FakeClock
from tests.usage.treedrive import TreeDrive

from protonfs.usage.cache import UsageCache
from protonfs.usage.engine import UsageEngine
from protonfs.webui.logtarget import resolve_log_target
from protonfs.webui.server import THROTTLE_NOTICE, serve
from protonfs.webui.settings import GuiSettings

pytestmark = pytest.mark.browser

sync_api = pytest.importorskip("playwright.sync_api")
TIMEOUT = 5000
TARGET = "/my-files"
LONG_NAME = "L" * 120
SHOTS = os.environ.get("PFS_SHOTS")  # directory for screenshots, when set


class Demo:
    def __init__(self, url, drive, engine):
        self.url, self.drive, self.engine = url, drive, engine


@pytest.fixture()
def demo_server(tmp_path):
    drive = TreeDrive()
    drive.add_file("/my-files/A/f1", 100)
    drive.add_file("/my-files/A/sub/x", 1000)
    drive.add_file("/my-files/B/big", 5000)
    drive.add_file("/my-files/frozen/z", 40)
    drive.add_file("/my-files/gone/g", 700)
    drive.add_file("/my-files/gone/h", 300)
    drive.add_file("/my-files/sizedonly/deep/q", 2222)
    drive.add_file("/my-files/" + LONG_NAME + "/w", 10)
    drive.on_slow_size = None  # lets serve() wire the throttle notice
    cache = UsageCache(tmp_path / "du.sqlite")
    engine = UsageEngine(drive, cache, clock=FakeClock())
    out, err = io.StringIO(), io.StringIO()
    ready, stop = threading.Event(), threading.Event()
    log = resolve_log_target(str(tmp_path / "server.log"), now=datetime.now())
    errors = []

    def run():
        try:
            serve(engine, GuiSettings(TARGET, freeze=["frozen"]), port=None, log=log,
                  open_browser=False, stdout=out, stderr=err, ready=ready, stop=stop)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
            ready.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert ready.wait(15), "server did not start"
    assert not errors, errors
    url = out.getvalue().strip().splitlines()[-1]
    yield Demo(url, drive, engine)
    stop.set()
    t.join(5)
    cache.close()


# module scope: two sync_playwright() instances cannot be alive in one thread at once
@pytest.fixture(scope="module")
def pw_browser():
    try:
        pw = sync_api.sync_playwright().start()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"playwright cannot start: {exc!r}")
    try:
        if not os.path.exists(pw.chromium.executable_path):
            pytest.skip("playwright chromium is not installed")
        b = pw.chromium.launch()
    except Exception as exc:  # noqa: BLE001
        pw.stop()
        pytest.skip(f"chromium cannot launch: {exc!r}")
    yield b
    b.close()
    pw.stop()


@pytest.fixture()
def pw_page(pw_browser, demo_server):
    ctx = pw_browser.new_context(viewport={"width": 1200, "height": 700})
    ctx.set_default_timeout(TIMEOUT)
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(demo_server.url)
    pg.wait_for_selector('tr[data-path="/my-files/A"]')
    yield pg
    ctx.close()
    assert not errors, errors


def _shot(pw_page, name):
    if SHOTS:
        pw_page.screenshot(path=os.path.join(SHOTS, name + ".png"))


def _width_ratio(pw_page):
    return pw_page.evaluate(
        "document.querySelector('#explorer').getBoundingClientRect().width /"
        " document.querySelector('#main').getBoundingClientRect().width")


def test_splitter_drag_persists(pw_page):
    before = _width_ratio(pw_page)
    box = pw_page.locator("#splitter").bounding_box()
    pw_page.mouse.move(box["x"] + 3, box["y"] + 50)
    pw_page.mouse.down()
    pw_page.mouse.move(box["x"] + 3 + 250, box["y"] + 50, steps=5)
    pw_page.mouse.up()
    after = _width_ratio(pw_page)
    assert after > before + 0.1
    saved = float(pw_page.evaluate("localStorage.getItem('pfs.split')"))
    assert 15 <= saved <= 85
    _shot(pw_page, "splitter-dragged")
    pw_page.reload()
    pw_page.wait_for_selector('tr[data-path="/my-files/A"]')
    assert abs(_width_ratio(pw_page) - after) < 0.01


def test_double_click_navigates_and_goup_returns(pw_page):
    assert pw_page.locator("#goup").is_disabled()
    pw_page.locator('tr[data-path="/my-files/A"]').dblclick()
    pw_page.wait_for_function("!document.querySelector('#goup').disabled")
    pw_page.wait_for_selector('tr[data-path="/my-files/B"]', state="detached")
    _shot(pw_page, "navigated-into-A")
    pw_page.locator("#goup").click()
    pw_page.wait_for_selector('tr[data-path="/my-files/B"]')
    assert pw_page.locator("#goup").is_disabled()


def test_keyboard_enter_and_backspace(pw_page):
    pw_page.locator('tr[data-path="/my-files/A"]').click()
    pw_page.locator("#tablewrap").focus()
    pw_page.keyboard.press("Enter")
    pw_page.wait_for_selector('tr[data-path="/my-files/B"]', state="detached")
    pw_page.keyboard.press("Backspace")
    pw_page.wait_for_selector('tr[data-path="/my-files/B"]')


def test_tabs_switch(pw_page):
    assert pw_page.locator('[data-pane="treemap"]').is_visible()
    pw_page.locator('[data-tab="sunburst"]').click()
    assert pw_page.locator('[data-pane="sunburst"]').is_visible()
    assert pw_page.locator('[data-pane="treemap"]').is_hidden()
    assert pw_page.locator("#icicle-toggle").is_visible()
    _shot(pw_page, "sunburst-tab")
    pw_page.locator('[data-tab="treemap"]').click()
    assert pw_page.locator('[data-pane="treemap"]').is_visible()


def test_sizemode_toggle(pw_page):
    row = pw_page.locator('tr[data-path="/my-files/B"] td').nth(1)
    assert "KiB" in row.inner_text()
    assert row.get_attribute("title") == "5000 bytes"
    pw_page.locator('#sizemode [data-mode="plan"]').click()
    assert row.inner_text() == "—"
    assert row.get_attribute("title") == "set du.plan_size"
    pw_page.locator('#sizemode [data-mode="parent"]').click()
    assert row.inner_text().endswith("%")


def test_states_are_styled(pw_page):
    assert "excluded" in pw_page.locator('tr[data-path="/my-files/frozen"]').get_attribute("class")
    assert pw_page.locator('tr[data-path="/my-files/frozen"] .badge-frozen').count() == 1
    _shot(pw_page, "table-states")


def test_depth_slider_posts_settings(pw_page, demo_server):
    pw_page.locator("#settings-toggle").click()
    pw_page.locator("#depth").evaluate(
        "e => { e.value = 2; e.dispatchEvent(new Event('input', {bubbles: true}));"
        " e.dispatchEvent(new Event('change', {bubbles: true})); }")
    pw_page.wait_for_function("document.querySelector('#depth-out').textContent === '2'")
    r = pw_page.evaluate("fetch('/api/settings').then(r => r.json())")
    assert r["maxdepth"] == 2
    pw_page.locator("#depth").evaluate(
        "e => { e.value = 3; e.dispatchEvent(new Event('change', {bubbles: true})); }")
    pw_page.wait_for_function(
        "fetch('/api/settings').then(r => r.json()).then(s => s.maxdepth === 3)")


def test_invalid_refresh_shows_message_and_reverts(pw_page):
    pw_page.locator("#settings-toggle").click()
    pw_page.locator("#refresh").fill("banana")
    pw_page.locator("#refresh").press("Enter")
    pw_page.wait_for_function("document.querySelector('#refresh-msg').textContent.length > 0")
    assert pw_page.locator("#refresh").input_value() == "5d"


def test_exclusion_editor_add_remove(pw_page):
    pw_page.locator("#settings-toggle").click()
    assert pw_page.locator("#exclude-rows .exclude-row").count() == 1  # "frozen"
    pw_page.locator("#exclude-add").click()
    new = pw_page.locator("#exclude-rows .exclude-row").nth(1)
    new.locator("input").fill("B")
    new.locator("select").select_option("hide")
    pw_page.wait_for_selector('tr[data-path="/my-files/B"]', state="detached")
    r = pw_page.evaluate("fetch('/api/settings').then(r => r.json())")
    assert r["hide"] == ["B"] and r["freeze"] == ["frozen"]
    _shot(pw_page, "settings-open")
    new.locator("button").click()
    pw_page.wait_for_selector('tr[data-path="/my-files/B"]')
    assert pw_page.locator("#exclude-rows .exclude-row").count() == 1
    r = pw_page.evaluate("fetch('/api/settings').then(r => r.json())")
    assert r["hide"] == []


def test_sse_update_rerenders_row(pw_page, demo_server):
    old = pw_page.locator('tr[data-path="/my-files/B"] td').nth(1).get_attribute("title")
    assert old == "5000 bytes"
    demo_server.drive.add_file("/my-files/B/more", 3000)
    pw_page.evaluate(
        "fetch('/api/refresh', {method: 'POST', headers: {'Content-Type': 'application/json',"
        " 'X-Protonfs-Token': document.querySelector('meta[name=pfs-token]').content},"
        " body: JSON.stringify({path: '/my-files/B'})})")
    pw_page.wait_for_function(
        "[...document.querySelectorAll('tr[data-path=\"/my-files/B\"] td')][1]"
        ".title === '8000 bytes'")


def test_notice_is_shown_and_dismissible(pw_page, demo_server):
    assert pw_page.locator("#notice").is_hidden()
    demo_server.drive.on_slow_size("/my-files/A")
    pw_page.wait_for_selector("#notice:not([hidden])")
    assert pw_page.locator("#notice-text").inner_text() == THROTTLE_NOTICE
    _shot(pw_page, "notice")
    pw_page.locator("#notice-dismiss").click()
    assert pw_page.locator("#notice").is_hidden()


def test_wide_table_stays_reachable_at_default_split(pw_page):
    row = pw_page.locator(f'tr[data-path="/my-files/{LONG_NAME}"]')
    row.wait_for()
    cell = row.locator("td.name")
    assert cell.get_attribute("title") == LONG_NAME
    assert cell.locator(".nm").evaluate("e => e.scrollWidth > e.clientWidth")  # ellipsised
    geom = pw_page.evaluate(
        "(() => { const w = document.querySelector('#tablewrap');"
        " const h = document.querySelector('th[data-sort=name]').parentElement.lastElementChild"
        ".getBoundingClientRect();"
        " const wr = w.getBoundingClientRect();"
        " return {right: h.right - wr.left + w.scrollLeft, scrollWidth: w.scrollWidth,"
        " clientWidth: w.clientWidth, overflowX: getComputedStyle(w).overflowX}; })()")
    assert geom["right"] <= geom["scrollWidth"] + 1
    # reachable: either the whole table fits, or the pane scrolls horizontally
    assert geom["scrollWidth"] <= geom["clientWidth"] or geom["overflowX"] in ("auto", "scroll")
    frozen = pw_page.locator('tr[data-path="/my-files/frozen"] td.state')
    assert frozen.inner_text() == "frozen"
    assert frozen.get_attribute("title") == "excluded (freeze)"


def test_depth_error_shows_next_to_depth(pw_page):
    pw_page.locator("#settings-toggle").click()
    pw_page.route("**/api/settings", lambda route: route.fulfill(
        status=400, content_type="application/json", body='{"error": "no deeper"}')
        if route.request.method == "POST" else route.continue_())
    pw_page.locator("#depth").evaluate(
        "e => { e.value = 5; e.dispatchEvent(new Event('change', {bubbles: true})); }")
    pw_page.wait_for_function("document.querySelector('#depth-msg').textContent === 'no deeper'")
    assert pw_page.locator("#refresh-msg").inner_text() == ""
    assert pw_page.locator("#depth").input_value() == "3"


def test_select_scrolls_row_into_view(pw_page):
    pw_page.set_viewport_size({"width": 1200, "height": 220})
    path = pw_page.evaluate(
        "(() => { const f = [...document.querySelectorAll('#rows tr[data-kind=folder]')];"
        " return f[f.length - 1].dataset.path; })()")
    assert pw_page.evaluate("document.querySelector('#tablewrap').scrollTop") == 0
    pw_page.evaluate("p => PFS.select(p)", path)
    pw_page.wait_for_function(
        "(() => { const r = document.querySelector('#rows tr.selected');"
        " if (!r) return false;"
        " const a = r.getBoundingClientRect(), w = document.querySelector('#tablewrap')"
        ".getBoundingClientRect();"
        " return a.top >= w.top - 1 && a.bottom <= w.bottom + 1; })()")
    assert pw_page.evaluate("document.querySelector('#tablewrap').scrollTop") > 0
