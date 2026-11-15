"""Drive the treemap / sunburst / icicle charts in headless Chromium against a real server."""

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
from protonfs.webui.server import serve
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
    tmp = tmp_path_factory.mktemp("chartsdemo")
    drive = TreeDrive()
    drive.add_file("/my-files/A/f1", 100)
    drive.add_file("/my-files/A/sub/x", 1000)
    drive.add_file("/my-files/A/old", 250)
    drive.trash("/my-files/A/old")
    drive.add_file("/my-files/B/big", 5000)
    drive.add_file("/my-files/frozen/z", 40)
    drive.add_file("/my-files/hidden/h", 900)
    drive.add_file("/my-files/sizedonly/deep/q", 2222)
    drive.on_slow_size = None
    cache = UsageCache(tmp / "du.sqlite")
    engine = UsageEngine(drive, cache, clock=FakeClock())
    out, err = io.StringIO(), io.StringIO()
    ready, stop = threading.Event(), threading.Event()
    log = resolve_log_target(str(tmp / "server.log"), now=datetime.now())
    errors = []

    def run():
        try:
            serve(engine, GuiSettings(TARGET, freeze=["frozen"], hide=["hidden"]), port=None,
                  log=log, open_browser=False, stdout=out, stderr=err, ready=ready, stop=stop)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
            ready.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert ready.wait(15), "server did not start"
    assert not errors, errors
    yield Demo(out.getvalue().strip().splitlines()[-1], drive, engine)
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
    pg.wait_for_selector('#chart-treemap g.node[data-path="/my-files/A"]')
    yield pg
    ctx.close()
    assert not errors, errors


def _shot(page, name):
    if SHOTS:
        page.screenshot(path=os.path.join(SHOTS, "charts-" + name + ".png"))


def _corner(page, selector):
    """Click/hover target in the top-left of a node's rect (clear of nested children)."""
    return page.locator(selector + " > rect.fill")


def _into_A(page):
    _corner(page, '#chart-treemap g.node.folder[data-path="/my-files/A"]').click(
        position={"x": 4, "y": 4})
    page.wait_for_selector('#chart-treemap g.node[data-path="/my-files/A/f1"]')


def test_hierarchy_shapes(page):
    shape = page.evaluate(
        """() => {
          const h = PFS.charts.hierarchy(PFS.state, 3);
          const all = [];
          (function walk(n) { all.push(n); (n.children || []).forEach(walk); })(h);
          return all.map(n => ({path: n.path, kind: n.kind, leaf: !n.children,
                                value: n.value, gap: n.gapKind || null, sized: !!n.sized}));
        }""")
    paths = [n["path"] for n in shape]
    assert not any("hidden" in (p or "") for p in paths), paths  # -X rows are absent
    by = {n["path"]: n for n in shape}
    assert by["/my-files/sizedonly"]["leaf"] and by["/my-files/sizedonly"]["sized"]
    assert by["/my-files/sizedonly"]["value"] == 2222
    gaps = [n for n in shape if n["kind"] == "gap"]
    assert gaps and all(n["leaf"] for n in gaps)
    assert any(n["gap"] == "unattributed" and n["value"] == 940 for n in gaps)
    assert "/my-files/frozen" not in by or by["/my-files/frozen"]["value"] == 0
    _into_A(page)
    inner = page.evaluate(
        "PFS.charts.hierarchy(PFS.state, 3).children.map(c => [c.kind, c.gapKind, c.value])")
    assert ["gap", "in_trash", 250] in inner


def test_gap_and_sized_styles_present(page):
    tm = "#chart-treemap "
    assert page.locator(tm + ".gap-unattributed").count() >= 1
    assert page.locator(tm + ".sized").count() >= 1
    assert page.locator(tm + ".sized > rect.hatch").count() >= 1
    # the frozen folder has no cached size: no area in the treemap
    assert page.locator(tm + 'g.node[data-path="/my-files/frozen"]').count() == 0
    page.locator('[data-tab="sunburst"]').click()
    page.wait_for_selector("#chart-sunburst path.arc")
    sb = "#chart-sunburst "
    assert page.locator(sb + ".sized").count() >= 1
    assert page.locator(sb + ".gap-unattributed").count() >= 1
    frozen = 'g.node.placeholder.excluded[data-path="/my-files/frozen"]'
    assert page.locator(sb + frozen).count() == 1
    _shot(page, "sunburst")
    page.locator('[data-tab="treemap"]').click()
    _into_A(page)
    assert page.locator(tm + ".gap-trash").count() == 1


def test_treemap_click_drills_in(page):
    _shot(page, "treemap")
    _into_A(page)
    page.wait_for_selector('tr[data-path="/my-files/B"]', state="detached")
    assert page.locator("#breadcrumb [aria-current]").inner_text() == "A"
    _shot(page, "treemap-drilled")
    page.locator("#goup").click()
    page.wait_for_selector('#chart-treemap g.node[data-path="/my-files/B"]')


def test_file_click_selects_row(page):
    _into_A(page)
    page.locator('#chart-treemap g.node.file[data-path="/my-files/A/f1"] > rect.fill').click()
    page.wait_for_selector('tr.selected[data-path="/my-files/A/f1"]')
    assert page.locator('#chart-treemap g.node.selected[data-path="/my-files/A/f1"]').count() == 1


def test_tooltip_fields(page):
    _corner(page, '#chart-treemap g.node.folder[data-path="/my-files/sizedonly"]').hover(
        position={"x": 4, "y": 4})
    tip = page.locator("#chart-tooltip")
    tip.wait_for(state="visible")
    text = tip.inner_text()
    for needle in ("sizedonly", "KiB", "of plan", "of used", "of parent", "items",
                   "in trash", "ago", "state"):
        assert needle in text, (needle, text)
    page.wait_for_selector('tr.selected[data-path="/my-files/sizedonly"]')  # hover selects the row
    page.mouse.move(2, 2)
    page.wait_for_selector("#chart-tooltip", state="hidden")


def test_icicle_toggle(page):
    page.locator('[data-tab="sunburst"]').click()
    page.wait_for_selector("#chart-sunburst path.arc")
    assert page.locator("#chart-sunburst rect.cell").count() == 0
    page.locator("#icicle-toggle").check()
    page.wait_for_selector("#chart-sunburst rect.cell")
    assert page.locator("#chart-sunburst path.arc").count() == 0
    _shot(page, "icicle")
    page.locator("#icicle-toggle").uncheck()
    page.wait_for_selector("#chart-sunburst path.arc")


def test_walk_progress_and_cancel(page, demo_server):
    _corner(page, '#chart-treemap g.node.folder[data-path="/my-files/B"]').hover(
        position={"x": 4, "y": 4})
    page.wait_for_selector('tr.selected[data-path="/my-files/B"]')
    page.locator("#chart-walk").click()
    # the walk over the tiny fake drive finishes at once; the bar reports the end state
    page.wait_for_function(
        "document.querySelector('#walkbar').textContent.indexOf('Walk') >= 0")
    page.locator("#chart-refresh").click()
    page.wait_for_function("PFS.state.pending.length >= 0")
    # Cancel is only offered while a walk runs: simulate a running progress state
    page.evaluate("PFS.state.progress = {state: 'running', done: 1, total: 4};"
                  "PFS.charts.render()")
    assert page.locator("#chart-walk-cancel").is_visible()
    with page.expect_request(lambda r: r.url.endswith("/api/walk/cancel")):
        page.locator("#chart-walk-cancel").click()

