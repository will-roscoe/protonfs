"""Browser tests for the page's trash line, outdated marks, chart areas and path handling.

``/api/root`` is pinned (see :func:`open_page`) so what the page draws does not depend on a
background job finishing.
"""

from __future__ import annotations

import json

import pytest
from tests.usage.treedrive import TreeDrive
from tests.webui._demo import serve_demo

from protonfs.webui.settings import GuiSettings

pytestmark = pytest.mark.browser

sync_api = pytest.importorskip("playwright.sync_api")
TIMEOUT = 5000
TARGET = "/my-files"
NOW = 1_000_000.0


@pytest.fixture(scope="module")
def demo_server(tmp_path_factory):
    drive = TreeDrive()
    drive.add_file("/my-files/A/f1", 100)
    with serve_demo(tmp_path_factory.mktemp("fixesdemo"), drive, GuiSettings(TARGET)) as demo:
        yield demo


def _row(depth, path, kind, size, **kw):
    row = {"depth": depth, "path": path, "name": path.rsplit("/", 1)[-1], "kind": kind,
           "size": size, "descendants": 2 if kind == "folder" else None, "pct_parent": None,
           "pct_used": None, "pct_plan": None, "in_trash": None, "in_trash_complete": None,
           "unchecked_bytes": None, "checked_at": NOW, "age_seconds": 0.0, "state": "listed",
           "exclusion": "none", "gap_kind": "none", "error": None, "trash_at": None,
           "trash_unresolved": 0}
    row.update(kw)
    return row


@pytest.fixture()
def open_page(pw_browser, demo_server):
    """Return ``open(body, refresh=None)``: a page whose ``/api/root`` answers ``body``."""
    ctxs = []
    errors = []

    def _open(body, *, refresh=None):
        ctx = pw_browser.new_context(viewport={"width": 1200, "height": 700})
        ctx.set_default_timeout(TIMEOUT)
        ctxs.append(ctx)
        pg = ctx.new_page()
        pg.on("pageerror", lambda e: errors.append(str(e)))
        full = {"breakdowns": {}, "pending": [], "trash": None, "now": NOW}
        full.update(body)
        text = json.dumps(full)
        pg.route("**/api/root*", lambda route: route.fulfill(
            status=200, content_type="application/json", body=text))
        if refresh is not None:
            pg.route("**/api/refresh", lambda route: route.fulfill(
                status=200, content_type="application/json", body=json.dumps(refresh)))
        pg.goto(demo_server.url)
        pg.wait_for_selector(f'tr[data-path="{TARGET}"]')
        return pg

    yield _open
    for c in ctxs:
        c.close()
    assert not errors, errors


def test_trash_summary_is_shown_with_reason_unresolved_count_and_age(open_page):
    trash = {"at": NOW - 7200, "groups": [["/my-files", 1, 0, 700]], "unresolved_folders": 2,
             "partial": "trash listing timed out after 300 s", "total_known": 700,
             "complete": False}
    page = open_page({"rows": [_row(0, TARGET, "folder", 5000)], "trash": trash})
    line = page.locator("#trashline")
    line.wait_for(state="visible")
    text = line.inner_text()
    assert "at least 700 B" in text and "timed out after 300 s" in text
    assert "2 trashed folders, size unknown" in text and "2h ago" in text


def test_complete_trash_summary_reads_complete(open_page):
    trash = {"at": NOW - 60, "groups": [], "unresolved_folders": 0, "partial": None,
             "total_known": 2048, "complete": True}
    page = open_page({"rows": [_row(0, TARGET, "folder", 5000)], "trash": trash})
    page.locator("#trashline").wait_for(state="visible")
    assert page.locator("#trashline").inner_text().startswith("Trash: 2.0 KiB (complete)")


def test_root_trash_cell_with_unresolved_folders_is_a_lower_bound_not_zero_unchecked(open_page):
    row = _row(0, TARGET, "folder", 5000, in_trash=700, in_trash_complete=False,
               unchecked_bytes=None, trash_unresolved=2, trash_at=NOW - 7200, age_seconds=10.0)
    page = open_page({"rows": [row]})
    cell = page.locator(f'tr[data-path="{TARGET}"] td:nth-child(4)')
    text = cell.inner_text()
    assert "≥ 700 B" in text and "2 trashed folders unmeasured" in text
    assert "unchecked" not in text and "0%" not in text
    assert "trash figure from 2h ago" in cell.get_attribute("title")


def test_invalidated_breakdown_is_marked_outdated(open_page):
    rows = [_row(0, TARGET, "folder", 5000), _row(1, f"{TARGET}/A", "folder", 1000)]
    page = open_page({"rows": rows, "breakdowns": {TARGET: {"invalid": True}}})
    tr = page.locator(f'tr[data-path="{TARGET}"]')
    assert "outdated" in tr.get_attribute("class")
    assert tr.locator("td.state").inner_text() == "outdated"
    other = page.locator(f'tr[data-path="{TARGET}/A"]')
    assert "outdated" not in other.get_attribute("class")
    # it says what will re-check it, not that a re-check is under way (none may be)
    title = tr.locator("td.state").get_attribute("title")
    assert "Refresh re-checks it" in title and "re-checking" not in title


def test_trash_line_at_the_account_root_says_whether_it_is_being_measured(open_page):
    page = open_page({"rows": [_row(0, TARGET, "folder", 5000)]})
    page.evaluate("PFS.state.settings.target = '/'; PFS.state.path = '/';"
                  "PFS.state.trash = null; PFS.state.pending = ['/']; PFS.render()")
    line = page.locator("#trashline")
    line.wait_for(state="visible")
    assert line.inner_text() == "Trash: being measured"
    page.evaluate("PFS.state.pending = []; PFS.render()")
    assert line.inner_text() == "Trash: not measured yet"


def _shape(page):
    return page.evaluate(
        """() => {
          const h = PFS.charts.hierarchy(PFS.state, 3);
          const all = [];
          (function walk(n) { all.push(n); (n.children || []).forEach(walk); })(h);
          return {negative: h.negative, value: h.value, nodes: all.map(n => ({
            path: n.path, name: n.name, kind: n.kind, gap: n.gapKind || null, value: n.value}))};
        }""")


def test_negative_remainder_is_not_drawn_and_areas_stay_in_the_box(open_page):
    rows = [
        _row(0, TARGET, "folder", 100),
        _row(1, f"{TARGET}/a", "folder", 100, state="sized"),
        _row(1, f"{TARGET}/b", "folder", 60, state="sized"),
        _row(1, TARGET, "gap", -50, state="gap", name="(unreconciled)", gap_kind="unreconciled"),
    ]
    page = open_page({"rows": rows})
    page.wait_for_selector('#chart-treemap g.node[data-path="/my-files/b"]')
    shape = _shape(page)
    assert shape["negative"] == 1
    assert all(n["value"] >= 0 for n in shape["nodes"])
    assert not any(n["kind"] == "gap" for n in shape["nodes"])
    box = page.evaluate("document.querySelector('#chart-treemap').getBoundingClientRect().right")
    xs = page.evaluate(
        "[...document.querySelectorAll('#chart-treemap g.node')].map(g => "
        "g.getBoundingClientRect().right)")
    assert max(xs) <= box + 1
    assert "below zero" in page.locator("#chart-treemap .chart-note").inner_text()
    page.click('#tabs [data-tab="sunburst"]')
    page.wait_for_selector("#chart-sunburst svg")
    assert "below zero" in page.locator("#chart-sunburst .chart-note").inner_text()


def test_folders_only_draws_the_hidden_file_bytes_as_a_filler(open_page):
    rows = [
        _row(0, TARGET, "folder", 1000),
        _row(1, f"{TARGET}/sub", "folder", 100, state="sized"),
    ]
    page = open_page({"rows": rows})
    page.wait_for_selector('#chart-treemap g.node[data-path="/my-files/sub"]')
    page.evaluate("PFS.state.settings.dirs_only = true; PFS.charts.render()")
    shape = _shape(page)
    filler = [n for n in shape["nodes"] if n["gap"] == "filler"]
    assert len(filler) == 1 and filler[0]["value"] == 900
    assert "Folders only" in filler[0]["name"]
    assert shape["value"] == 1000  # the folder's area is its size, not just its subfolders


def test_folders_only_filler_names_items_without_a_size_too(open_page):
    rows = [
        _row(0, TARGET, "folder", 1000),
        _row(1, f"{TARGET}/sub", "folder", 100, state="sized"),
        _row(1, f"{TARGET}/nosize", "folder", None, state="unmeasured"),
    ]
    page = open_page({"rows": rows})
    page.wait_for_selector('#chart-treemap g.node[data-path="/my-files/sub"]')
    page.evaluate("PFS.state.settings.dirs_only = true; PFS.charts.render()")
    filler = [n for n in _shape(page)["nodes"] if n["gap"] == "filler"]
    assert len(filler) == 1 and "items without a size" in filler[0]["name"]


def test_escaped_slash_in_a_name_is_one_path_segment(open_page):
    page = open_page({"rows": [_row(0, TARGET, "folder", 5)]})
    assert page.evaluate("PFS.parentOf('/my-files/a\\\\/b')") == "/my-files"
    assert page.evaluate("PFS.parentOf('/my-files/a\\\\/b/c')") == "/my-files/a\\/b"
    assert page.evaluate("PFS.parentOf('/my-files')") == "/"
    page.evaluate(
        "PFS.state.path = '/my-files/a\\\\/b/c'; PFS.render()")
    crumbs = page.locator("#breadcrumb li").all_inner_texts()
    assert crumbs == ["Drive", "my-files", "a/b", "c"]
    page.locator("#breadcrumb li:nth-child(3) button").click()
    page.wait_for_function("PFS.state.path === '/my-files/a\\\\/b'")


def test_compound_refresh_threshold_still_fades_rows(open_page):
    row = _row(0, TARGET, "folder", 5, age_seconds=9900.0)
    page = open_page({"rows": [row]})
    page.evaluate(
        "PFS.state.settings = {target: '/my-files', refresh: '2h45m', maxdepth: 3};"
        " PFS.render()")
    opacity = page.locator(f'tr[data-path="{TARGET}"]').evaluate("e => e.style.opacity")
    assert float(opacity) == pytest.approx(0.6)
    page.evaluate(
        "PFS.state.settings = {target: '/my-files', refresh: '1mo', maxdepth: 3};"
        " PFS.state.rows[0].age_seconds = 2592000; PFS.render()")
    assert float(page.locator(f'tr[data-path="{TARGET}"]').evaluate(
        "e => e.style.opacity")) == pytest.approx(0.6)


def test_refresh_that_did_not_start_is_reported(open_page):
    page = open_page({"rows": [_row(0, TARGET, "folder", 5)]},
                     refresh={"queued": False, "pending": []})
    page.click("#refresh-here")
    page.wait_for_function(
        "document.querySelector('#status').textContent.includes('did not start')")
