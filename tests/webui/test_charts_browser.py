"""Drive the treemap / sunburst / icicle charts in headless Chromium against a real server."""

from __future__ import annotations

import json
import os

import pytest
from tests.usage.treedrive import TreeDrive
from tests.webui._demo import serve_demo

from protonfs.webui.settings import GuiSettings

pytestmark = pytest.mark.browser

sync_api = pytest.importorskip("playwright.sync_api")
TIMEOUT = 5000
TARGET = "/my-files"
SHOTS = os.environ.get("PFS_SHOTS")  # directory for screenshots, when set


@pytest.fixture(scope="module")
def demo_server(tmp_path_factory):
    drive = TreeDrive()
    drive.add_file("/my-files/A/f1", 100)
    drive.add_file("/my-files/A/sub/x", 1000)
    drive.add_file("/my-files/A/old", 250)
    drive.trash("/my-files/A/old")
    drive.add_file("/my-files/B/big", 5000)
    drive.add_file("/my-files/frozen/z", 40)
    drive.add_file("/my-files/hidden/h", 900)
    drive.add_file("/my-files/sizedonly/deep/q", 2222)
    settings = GuiSettings(TARGET, freeze=["frozen"], hide=["hidden"])
    with serve_demo(tmp_path_factory.mktemp("chartsdemo"), drive, settings) as demo:
        yield demo


@pytest.fixture()
def page(pw_browser, demo_server):
    ctx = pw_browser.new_context(viewport={"width": 1200, "height": 700})
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


def _row(depth, path, kind, size, **kw):
    row = {"depth": depth, "path": path, "name": path.rsplit("/", 1)[-1], "kind": kind,
           "size": size, "descendants": 2 if kind == "folder" else None, "pct_parent": None,
           "pct_used": None, "pct_plan": None, "in_trash": None, "in_trash_complete": None,
           "unchecked_bytes": None, "checked_at": None, "age_seconds": 0.0, "state": "listed",
           "exclusion": "none", "gap_kind": "none", "error": None}
    row.update(kw)
    return row


PINNED_ROWS = [
    _row(0, "/my-files", "folder", 9512),
    _row(1, "/my-files/B", "folder", 5000),
    _row(2, "/my-files/B/big", "file", 5000, state="sized"),
    _row(1, "/my-files/sizedonly", "folder", 2222, state="sized"),
    _row(1, "/my-files/frozen", "folder", None, state="excluded", exclusion="freeze"),
    _row(1, "/my-files/hidden", "folder", 900, exclusion="hide"),
    _row(1, "/my-files", "gap", 940, state="gap", name="(unattributed, incl. excluded)",
         gap_kind="unattributed"),
]


@pytest.fixture()
def pinned_page(pw_browser, demo_server):
    """The page with ``/api/root`` pinned, so the background listing cannot change the shape."""
    ctx = pw_browser.new_context(viewport={"width": 1200, "height": 700})
    ctx.set_default_timeout(TIMEOUT)
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    body = json.dumps({"rows": PINNED_ROWS, "breakdowns": {}, "pending": [], "trash": None})
    pg.route("**/api/root*", lambda route: route.fulfill(
        status=200, content_type="application/json", body=body))
    pg.goto(demo_server.url)
    pg.wait_for_selector('#chart-treemap g.node[data-path="/my-files/B"]')
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
    _corner(page, '#chart-treemap g.node.folder[data-path="/my-files/A"]').dblclick(
        position={"x": 4, "y": 4})
    page.wait_for_function(
        "PFS.state.path === '/my-files/A' && PFS.state.rows[0].path === '/my-files/A'")
    page.wait_for_selector('#chart-treemap g.node[data-path="/my-files/A/f1"]')


def test_hierarchy_shapes(pinned_page):
    page = pinned_page
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
    assert not by["/my-files/B/big"]["sized"]  # a file is never "not walked"
    gaps = [n for n in shape if n["kind"] == "gap"]
    assert gaps and all(n["leaf"] for n in gaps)
    assert any(n["gap"] == "unattributed" and n["value"] == 940 for n in gaps)
    assert by["/my-files/frozen"]["value"] == 0


def test_hierarchy_has_gap_child_in_trash(page):
    _into_A(page)
    inner = page.evaluate(
        "PFS.charts.hierarchy(PFS.state, 3).children.map(c => [c.kind, c.gapKind, c.value])")
    assert ["gap", "in_trash", 250] in inner


def test_gap_and_sized_styles_present(pinned_page):
    page = pinned_page
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
    frozen = 'g.node.placeholder.excluded[data-path="/my-files/frozen"]'
    assert page.locator(sb + frozen).count() == 1
    _shot(page, "sunburst")


def test_trash_gap_style_present(page):
    _into_A(page)
    assert page.locator("#chart-treemap .gap-trash").count() == 1


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


def test_tooltip_fields(pinned_page):
    page = pinned_page
    _corner(page, '#chart-treemap g.node.folder[data-path="/my-files/sizedonly"]').hover(
        position={"x": 4, "y": 4})
    tip = page.locator("#chart-tooltip")
    tip.wait_for(state="visible")
    text = tip.inner_text()
    for needle in ("sizedonly", "KiB", "of plan", "of used", "of parent", "items",
                   "in trash", "ago", "state"):
        assert needle in text, (needle, text)
    page.mouse.move(2, 2)
    page.wait_for_selector("#chart-tooltip", state="hidden")


def test_hover_does_not_select_but_click_does(pinned_page):
    page = pinned_page
    node = _corner(page, '#chart-treemap g.node.folder[data-path="/my-files/sizedonly"]')
    node.hover(position={"x": 4, "y": 4})
    page.locator("#chart-tooltip").wait_for(state="visible")
    assert page.evaluate("PFS.state.selected") is None
    assert page.locator("tr.selected").count() == 0
    node.click(position={"x": 4, "y": 4})
    page.wait_for_selector('tr.selected[data-path="/my-files/sizedonly"]')
    assert page.evaluate("PFS.state.selected") == "/my-files/sizedonly"
    assert page.evaluate("PFS.state.path") == "/my-files"  # a click selects, it does not open


def test_icicle_toggle(pinned_page):
    page = pinned_page
    page.locator('[data-tab="sunburst"]').click()
    page.wait_for_selector("#chart-sunburst path.arc")
    assert page.locator("#chart-sunburst rect.cell").count() == 0
    page.locator("#icicle-toggle").check()
    page.wait_for_selector("#chart-sunburst rect.cell")
    assert page.locator("#chart-sunburst path.arc").count() == 0
    _shot(page, "icicle")
    page.locator("#icicle-toggle").uncheck()
    page.wait_for_selector("#chart-sunburst path.arc")


def test_hatch_pattern_resolves_on_every_tab(pinned_page):
    page = pinned_page
    page.locator('[data-tab="sunburst"]').click()
    page.wait_for_selector("#chart-sunburst path.hatch")
    info = page.evaluate(
        """() => {
          const pats = document.querySelectorAll('pattern#pfs-hatch');
          let hidden = false;
          for (let e = pats[0]; e; e = e.parentElement) {
            if (getComputedStyle(e).display === 'none') hidden = true;
          }
          const h = document.querySelector('#chart-sunburst path.hatch');
          return {count: pats.length, hidden: hidden, fill: getComputedStyle(h).fill,
                  w: pats[0].parentElement.parentElement.getBoundingClientRect().width};
        }""")
    assert info["count"] == 1 and not info["hidden"], info
    assert "pfs-hatch" in info["fill"], info
    page.locator("#icicle-toggle").check()
    page.wait_for_selector("#chart-sunburst rect.hatch")
    fill = page.evaluate(
        "getComputedStyle(document.querySelector('#chart-sunburst rect.hatch')).fill")
    assert "pfs-hatch" in fill
    # painted, not just referenced: the pattern's line must have a visible stroke
    stroke = page.evaluate(
        "getComputedStyle(document.querySelector('pattern#pfs-hatch line')).stroke")
    assert stroke not in ("none", ""), stroke


def test_placeholder_arc_is_hoverable(pinned_page):
    page = pinned_page
    page.locator('[data-tab="sunburst"]').click()
    page.wait_for_selector("#chart-sunburst g.node.placeholder path.fill")
    box = page.locator("#chart-sunburst g.node.placeholder path.fill").bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.locator("#chart-tooltip").wait_for(state="visible")
    assert "frozen" in page.locator("#chart-tooltip").inner_text()


def test_identical_refetch_does_not_redraw(pinned_page):
    page = pinned_page
    page.evaluate("document.querySelector('#chart-treemap svg').__mark = 1")
    page.evaluate("PFS.state.rows = JSON.parse(JSON.stringify(PFS.state.rows));"
                  "PFS.charts.render()")
    assert page.evaluate("document.querySelector('#chart-treemap svg').__mark") == 1


def test_unlisted_root_says_so(page):
    page.evaluate("PFS.navigate('/my-files/frozen')")
    page.wait_for_selector("#chart-treemap .chart-empty")


def test_walk_selected_posts_with_token_and_reports_failure(page):
    page.wait_for_function("document.querySelector('#chart-walk').disabled")
    _corner(page, '#chart-treemap g.node.folder[data-path="/my-files/B"]').click(
        position={"x": 4, "y": 4})
    page.wait_for_selector('tr.selected[data-path="/my-files/B"]')
    with page.expect_request(lambda r: r.url.endswith("/api/walk")) as info:
        page.locator("#chart-walk").click()
    req = info.value
    assert req.post_data_json == {"paths": ["/my-files/B"]}
    assert req.headers["x-protonfs-token"]
    page.wait_for_function(
        "document.querySelector('#walkbar').textContent.indexOf('Walk') >= 0")
    with page.expect_request(lambda r: r.url.endswith("/api/refresh")) as info:
        page.locator("#chart-refresh").click()
    assert info.value.post_data_json == {"path": "/my-files"}
    assert info.value.headers["x-protonfs-token"]
    page.route("**/api/refresh", lambda route: route.fulfill(
        status=403, content_type="application/json", body='{"error": "bad token"}'))
    page.locator("#chart-refresh").click()
    page.wait_for_function(
        "document.querySelector('#chart-msg').textContent.indexOf('bad token') >= 0")


def test_walk_bar_cancel_and_ended_line_clears(page):
    page.evaluate("PFS.state.progress = {state: 'running', done: 1, total: 4};"
                  "PFS.charts.render()")
    assert page.locator("#chart-walk-cancel").is_visible()
    with page.expect_request(lambda r: r.url.endswith("/api/walk/cancel")) as info:
        page.locator("#chart-walk-cancel").click()
    assert info.value.headers["x-protonfs-token"]
    page.evaluate("PFS.charts.endLingerMs = 200;"
                  "PFS.state.progress = {state: 'cancelled', done: 2, total: 4};"
                  "PFS.charts.render()")
    assert "cancelled" in page.locator("#walkbar").inner_text()
    page.wait_for_selector("#walkbar", state="hidden")
