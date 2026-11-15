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
SHOTS = os.environ.get("PFS_SHOTS")  # directory for screenshots, when set


def _chromium_available() -> bool:
    try:
        with sync_api.sync_playwright() as p:
            return os.path.exists(p.chromium.executable_path)
    except Exception:  # noqa: BLE001
        return False


if not _chromium_available():
    pytest.skip("playwright chromium is not installed", allow_module_level=True)


class Demo:
    def __init__(self, url, drive, engine):
        self.url, self.drive, self.engine = url, drive, engine


@pytest.fixture(scope="module")
def demo_server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("demo")
    drive = TreeDrive()
    drive.add_file("/my-files/A/f1", 100)
    drive.add_file("/my-files/A/sub/x", 1000)
    drive.add_file("/my-files/B/big", 5000)
    drive.add_file("/my-files/frozen/z", 40)
    drive.add_file("/my-files/gone/g", 700)
    drive.add_file("/my-files/gone/h", 300)
    drive.add_file("/my-files/sizedonly/deep/q", 2222)
    drive.on_slow_size = None  # lets serve() wire the throttle notice
    cache = UsageCache(tmp / "du.sqlite")
    engine = UsageEngine(drive, cache, clock=FakeClock())
    out, err = io.StringIO(), io.StringIO()
    ready, stop = threading.Event(), threading.Event()
    log = resolve_log_target(str(tmp / "server.log"), now=datetime.now())
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


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture()
def page(browser, demo_server):
    ctx = browser.new_context(viewport={"width": 1200, "height": 700})
    ctx.set_default_timeout(TIMEOUT)
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(demo_server.url)
    pg.wait_for_selector('tr[data-path="/my-files/A"]')
    yield pg
    ctx.close()
    assert not errors, errors


def _shot(page, name):
    if SHOTS:
        page.screenshot(path=os.path.join(SHOTS, name + ".png"))


def _width_ratio(page):
    return page.evaluate(
        "document.querySelector('#explorer').getBoundingClientRect().width /"
        " document.querySelector('#main').getBoundingClientRect().width")


def test_splitter_drag_persists(page):
    before = _width_ratio(page)
    box = page.locator("#splitter").bounding_box()
    page.mouse.move(box["x"] + 3, box["y"] + 50)
    page.mouse.down()
    page.mouse.move(box["x"] + 3 + 250, box["y"] + 50, steps=5)
    page.mouse.up()
    after = _width_ratio(page)
    assert after > before + 0.1
    saved = float(page.evaluate("localStorage.getItem('pfs.split')"))
    assert 15 <= saved <= 85
    _shot(page, "splitter-dragged")
    page.reload()
    page.wait_for_selector('tr[data-path="/my-files/A"]')
    assert abs(_width_ratio(page) - after) < 0.01


def test_double_click_navigates_and_goup_returns(page):
    assert page.locator("#goup").is_disabled()
    page.locator('tr[data-path="/my-files/A"]').dblclick()
    page.wait_for_function("!document.querySelector('#goup').disabled")
    page.wait_for_selector('tr[data-path="/my-files/B"]', state="detached")
    _shot(page, "navigated-into-A")
    page.locator("#goup").click()
    page.wait_for_selector('tr[data-path="/my-files/B"]')
    assert page.locator("#goup").is_disabled()


def test_keyboard_enter_and_backspace(page):
    page.locator('tr[data-path="/my-files/A"]').click()
    page.locator("#tablewrap").focus()
    page.keyboard.press("Enter")
    page.wait_for_selector('tr[data-path="/my-files/B"]', state="detached")
    page.keyboard.press("Backspace")
    page.wait_for_selector('tr[data-path="/my-files/B"]')


def test_tabs_switch(page):
    assert page.locator('[data-pane="treemap"]').is_visible()
    page.locator('[data-tab="sunburst"]').click()
    assert page.locator('[data-pane="sunburst"]').is_visible()
    assert page.locator('[data-pane="treemap"]').is_hidden()
    assert page.locator("#icicle-toggle").is_visible()
    _shot(page, "sunburst-tab")
    page.locator('[data-tab="treemap"]').click()
    assert page.locator('[data-pane="treemap"]').is_visible()


def test_sizemode_toggle(page):
    row = page.locator('tr[data-path="/my-files/B"] td').nth(1)
    assert "KiB" in row.inner_text()
    assert row.get_attribute("title") == "5000 bytes"
    page.locator('#sizemode [data-mode="plan"]').click()
    assert row.inner_text() == "—"
    assert row.get_attribute("title") == "set du.plan_size"
    page.locator('#sizemode [data-mode="parent"]').click()
    assert row.inner_text().endswith("%")


def test_states_are_styled(page):
    assert "excluded" in page.locator('tr[data-path="/my-files/frozen"]').get_attribute("class")
    assert page.locator('tr[data-path="/my-files/frozen"] .badge-frozen').count() == 1
    _shot(page, "table-states")


def test_depth_slider_posts_settings(page, demo_server):
    page.locator("#settings-toggle").click()
    page.locator("#depth").evaluate(
        "e => { e.value = 2; e.dispatchEvent(new Event('input', {bubbles: true}));"
        " e.dispatchEvent(new Event('change', {bubbles: true})); }")
    page.wait_for_function("document.querySelector('#depth-out').textContent === '2'")
    r = page.evaluate("fetch('/api/settings').then(r => r.json())")
    assert r["maxdepth"] == 2
    page.locator("#depth").evaluate(
        "e => { e.value = 3; e.dispatchEvent(new Event('change', {bubbles: true})); }")
    page.wait_for_function(
        "fetch('/api/settings').then(r => r.json()).then(s => s.maxdepth === 3)")


def test_invalid_refresh_shows_message_and_reverts(page):
    page.locator("#settings-toggle").click()
    page.locator("#refresh").fill("banana")
    page.locator("#refresh").press("Enter")
    page.wait_for_function("document.querySelector('#refresh-msg').textContent.length > 0")
    assert page.locator("#refresh").input_value() == "5d"


def test_exclusion_editor_add_remove(page):
    page.locator("#settings-toggle").click()
    assert page.locator("#exclude-rows .exclude-row").count() == 1  # "frozen"
    page.locator("#exclude-add").click()
    new = page.locator("#exclude-rows .exclude-row").nth(1)
    new.locator("input").fill("B")
    new.locator("select").select_option("hide")
    page.wait_for_selector('tr[data-path="/my-files/B"]', state="detached")
    r = page.evaluate("fetch('/api/settings').then(r => r.json())")
    assert r["hide"] == ["B"] and r["freeze"] == ["frozen"]
    _shot(page, "settings-open")
    new.locator("button").click()
    page.wait_for_selector('tr[data-path="/my-files/B"]')
    assert page.locator("#exclude-rows .exclude-row").count() == 1
    r = page.evaluate("fetch('/api/settings').then(r => r.json())")
    assert r["hide"] == []


def test_sse_update_rerenders_row(page, demo_server):
    old = page.locator('tr[data-path="/my-files/B"] td').nth(1).get_attribute("title")
    assert old == "5000 bytes"
    demo_server.drive.add_file("/my-files/B/more", 3000)
    page.evaluate(
        "fetch('/api/refresh', {method: 'POST', headers: {'Content-Type': 'application/json',"
        " 'X-Protonfs-Token': document.querySelector('meta[name=pfs-token]').content},"
        " body: JSON.stringify({path: '/my-files/B'})})")
    page.wait_for_function(
        "[...document.querySelectorAll('tr[data-path=\"/my-files/B\"] td')][1]"
        ".title === '8000 bytes'")


def test_notice_is_shown_and_dismissible(page, demo_server):
    assert page.locator("#notice").is_hidden()
    demo_server.drive.on_slow_size("/my-files/A")
    page.wait_for_selector("#notice:not([hidden])")
    assert page.locator("#notice-text").inner_text() == THROTTLE_NOTICE
    _shot(page, "notice")
    page.locator("#notice-dismiss").click()
    assert page.locator("#notice").is_hidden()
