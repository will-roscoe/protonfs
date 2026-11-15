"""Tests for the GUI server: token auth, routes, SSE, startup and port handling."""

from __future__ import annotations

import http.client
import io
import json
import re
import socket
import threading
from datetime import datetime
from urllib.parse import urlsplit

import click
import pytest
from tests.usage.conftest import FakeClock
from tests.usage.treedrive import TreeDrive

from protonfs.usage.cache import UsageCache
from protonfs.usage.engine import UsageEngine
from protonfs.webui.logtarget import resolve_log_target
from protonfs.webui.server import parse_port, serve
from protonfs.webui.settings import GuiSettings

TARGET = "/my-files"


class Running:
    def __init__(self, drive, engine, stdout, stderr, token, host, port, thread):
        self.drive, self.engine = drive, engine
        self.stdout, self.stderr = stdout, stderr
        self.token, self.host, self.port, self.thread = token, host, port, thread

    def request(self, method, path, body=None, *, auth=True, headers=None, timeout=5):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        hdrs = dict(headers or {})
        if auth is True:
            hdrs["X-Protonfs-Token"] = self.token
        if body is not None:
            hdrs["Content-Type"] = "application/json"
            body = json.dumps(body)
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp, data

    def raw(self, request: bytes, timeout=5) -> bytes:
        with socket.create_connection((self.host, self.port), timeout=timeout) as sock:
            sock.sendall(request)
            chunks = []
            try:
                while True:
                    data = sock.recv(65536)
                    if not data:
                        break
                    chunks.append(data)
            except socket.timeout:
                pass
        return b"".join(chunks)

    @property
    def cookie(self):
        return f"pfs_token_{self.port}={self.token}"

    def json(self, method, path, body=None, **kw):
        resp, data = self.request(method, path, body, **kw)
        return resp.status, json.loads(data)

    def events(self, timeout=5):
        """Open the SSE stream; yield (event, data) pairs, reading with a socket timeout."""
        sock = socket.create_connection((self.host, self.port), timeout=timeout)
        req = (f"GET /api/events HTTP/1.1\r\nHost: {self.host}:{self.port}\r\n"
               f"X-Protonfs-Token: {self.token}\r\n\r\n")
        sock.sendall(req.encode())
        f = sock.makefile("rb")
        while f.readline().strip():  # skip response headers
            pass

        def gen():
            ev = None
            try:
                while True:
                    line = f.readline().decode().rstrip("\n")
                    if line.startswith("event: "):
                        ev = line[7:]
                    elif line.startswith("data: "):
                        yield ev, json.loads(line[6:])
                    elif line.startswith(": "):
                        yield "comment", line[2:]
            finally:
                f.close()
                sock.close()

        return gen()


def _start(tmp_path, *, listen="127.0.0.1", port=None, drive=None, settings=None, out=None):
    drive = drive or TreeDrive()
    cache = UsageCache(tmp_path / "du.sqlite")
    engine = UsageEngine(drive, cache, clock=FakeClock())
    out, err = out or io.StringIO(), io.StringIO()
    ready, stop = threading.Event(), threading.Event()
    log = resolve_log_target(str(tmp_path / "server.log"), now=datetime.now())
    errors = []

    def run():
        try:
            serve(engine, settings or GuiSettings(TARGET), listen=listen, port=port, log=log,
                  open_browser=False, stdout=out, stderr=err, ready=ready, stop=stop)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
            ready.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert ready.wait(15), "server did not start"
    return drive, engine, cache, out, err, stop, t, errors


@pytest.fixture()
def tree_drive():
    d = TreeDrive()
    d.add_file("/my-files/A/f1", 100)
    d.add_file("/my-files/A/sub/x", 1000)
    d.add_file("/my-files/B/big", 5000)
    d.on_slow_size = None  # serve() attaches its throttle notice where the source has one
    return d


@pytest.fixture()
def srv(tmp_path, tree_drive):
    drive, engine, cache, out, err, stop, t, errors = _start(tmp_path, drive=tree_drive)
    assert not errors, errors
    url = urlsplit(out.getvalue().strip().splitlines()[-1])
    token = url.query[2:]
    yield Running(drive, engine, out, err, token, url.hostname, url.port, t)
    stop.set()
    t.join(5)
    assert not t.is_alive()
    cache.close()


def test_last_stdout_line_is_url(srv):
    lines = srv.stdout.getvalue().splitlines()
    assert lines[-1] == f"http://127.0.0.1:{srv.port}/?t={srv.token}"
    assert len(srv.token) >= 32


def test_index_served_without_token_and_static_needs_one(srv):
    resp, data = srv.request("GET", "/", auth=False)
    assert resp.status == 200 and b"<title>" in data
    assert srv.request("GET", "/static/app.js", auth=False)[0].status == 403
    assert srv.request("GET", "/static/../server.py")[0].status == 404
    assert srv.request("GET", "/static/vendor/d3.v7.9.0.min.js")[0].status == 200


def test_api_requires_token(srv):
    for method, path in (("GET", "/api/tree"), ("GET", "/api/root"), ("GET", "/api/settings"),
                         ("GET", "/api/events"), ("POST", "/api/walk/cancel")):
        resp, _ = srv.request(method, path, {} if method == "POST" else None, auth=False)
        assert resp.status == 403, path
    resp, _ = srv.request("GET", "/api/settings", auth=False,
                          headers={"X-Protonfs-Token": "wrong"})
    assert resp.status == 403
    resp, _ = srv.request("GET", "/api/settings", auth=False,
                          headers={"Cookie": f"pfs_token_{srv.port}=wrong"})
    assert resp.status == 403
    # the old unscoped cookie name is not accepted
    resp, _ = srv.request("GET", "/api/settings", auth=False,
                          headers={"Cookie": f"pfs_token={srv.token}"})
    assert resp.status == 403


def test_cookie_set_from_query_token_then_redirect(srv):
    resp, _ = srv.request("GET", f"/?t={srv.token}", auth=False)
    assert resp.status == 303 and resp.getheader("Location") == "/"
    cookie = resp.getheader("Set-Cookie")
    assert cookie.startswith(f"pfs_token_{srv.port}={srv.token}")
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/" in cookie
    assert srv.request("GET", "/?t=nope", auth=False)[0].getheader("Set-Cookie") is None
    # the cookie opens GETs and the page, which then carries the token for its POSTs
    resp, _ = srv.request("GET", "/api/settings", auth=False, headers={"Cookie": srv.cookie})
    assert resp.status == 200
    resp, page = srv.request("GET", "/", auth=False, headers={"Cookie": srv.cookie})
    assert f'name="pfs-token" content="{srv.token}"'.encode() in page
    nocookie = srv.request("GET", "/", auth=False)[1]
    assert srv.token.encode() not in nocookie and b"{{PFS_TOKEN}}" not in nocookie
    assert srv.request("GET", "/", auth=False, headers={"Cookie": srv.cookie})[0].getheader(
        "Cache-Control") == "no-store"
    import re
    meta = re.search(rb'name="pfs-token" content="([^"]*)"', page).group(1).decode()
    assert srv.request("POST", "/api/walk/cancel", {}, auth=False,
                       headers={"X-Protonfs-Token": meta})[0].status == 200


def test_post_needs_the_header_not_just_the_cookie(srv):
    resp, _ = srv.request("POST", "/api/walk/cancel", {}, auth=False,
                          headers={"Cookie": srv.cookie})
    assert resp.status == 403
    assert srv.request("POST", "/api/walk/cancel", {})[0].status == 200


def test_foreign_origin_is_rejected(srv):
    own = f"http://{srv.host}:{srv.port}"
    bad = srv.request("POST", "/api/walk/cancel", {}, headers={"Origin": "http://127.0.0.1:1"})
    assert bad[0].status == 403
    assert srv.request("POST", "/api/walk/cancel", {}, headers={"Origin": own})[0].status == 200
    resp, _ = srv.request("GET", "/api/settings", headers={"Origin": "http://evil.example"})
    assert resp.status == 403


def test_responses_carry_referrer_policy(srv):
    for path in ("/", "/api/settings"):
        assert srv.request("GET", path)[0].getheader("Referrer-Policy") == "no-referrer"
    redirect, _ = srv.request("GET", f"/?t={srv.token}", auth=False)
    assert redirect.getheader("Referrer-Policy") == "no-referrer"


def test_token_never_reaches_the_log_and_log_is_private(srv, tmp_path):
    srv.request("GET", f"/?t={srv.token}", auth=False)
    srv.request("GET", f"/api/nope?x=1&t={srv.token}&y=2")
    srv.request("GET", f"/?%74={srv.token}", auth=False)  # percent-encoded key
    srv.raw(f"GET /?t={srv.token} HTTP/1.1\r\nHost: {srv.host}:{srv.port}\r\nBogus\r\n\r\n"
            .encode())
    text = (tmp_path / "server.log").read_text()
    assert "GET /" in text and "t=<redacted>" in text
    assert srv.token not in text
    assert (tmp_path / "server.log").stat().st_mode & 0o777 == 0o600


def test_bad_content_length(srv):
    head = (f"POST /api/walk/cancel HTTP/1.1\r\nHost: {srv.host}:{srv.port}\r\n"
            f"X-Protonfs-Token: {srv.token}\r\n")
    assert srv.raw((head + "Content-Length: -5\r\n\r\n").encode()).startswith(b"HTTP/1.0 400")
    assert srv.raw((head + "Content-Length: abc\r\n\r\n").encode()).startswith(b"HTTP/1.0 400")
    assert srv.raw((head + "\r\n").encode()).startswith(b"HTTP/1.0 411")
    big = head + f"Content-Length: {(1 << 20) + 1}\r\n\r\n"
    assert srv.raw(big.encode()).startswith(b"HTTP/1.0 400")


def test_tree_request_makes_no_drive_calls_and_queues_stale(srv):
    # hold every queued job at its first Drive call, so what the requests did (and what
    # they left pending) can be checked before any job runs
    gate = threading.Event()
    real_enter = srv.drive._enter

    def gated(op, path):
        gate.wait(10)
        real_enter(op, path)

    srv.drive._enter = gated
    try:
        events = srv.events()
        assert next(events) == ("comment", "open")
        before = list(srv.drive.calls)
        status, body = srv.json("GET", f"/api/tree?path={TARGET}&depth=2&queue=0")
        assert status == 200 and list(srv.drive.calls) == before and body["pending"] == []
        status, body = srv.json("GET", f"/api/tree?path={TARGET}&depth=2")
        assert status == 200 and list(srv.drive.calls) == before
        assert body["pending"] == [TARGET] and body["rows"][0]["path"] == TARGET
        gate.set()
        name, data = next(events)
        assert name == "breakdown" and data["path"] == TARGET
        assert data["breakdown"]["size"] == 6100
        status, body = srv.json("GET", f"/api/tree?path={TARGET}&depth=2&queue=0")
        assert {"A", "B"} <= {r["name"] for r in body["rows"]}
        # the page's event refetch: the subfolders the job revealed are queued once
        gate.clear()
        status, body = srv.json("GET", f"/api/tree?path={TARGET}&depth=2&queue=new")
        assert status == 200 and sorted(body["pending"]) == [f"{TARGET}/A", f"{TARGET}/B"]
    finally:
        gate.set()


def test_unknown_queue_mode_is_a_bad_request(srv):
    assert srv.json("GET", "/api/root?queue=sometimes")[0] == 400


def test_bad_requests(srv):
    assert srv.json("GET", "/api/tree?path=/devices&depth=1")[0] == 400
    assert srv.json("GET", f"/api/tree?path={TARGET}&depth=x")[0] == 400
    assert srv.json("GET", "/api/nope")[0] == 404
    resp, _ = srv.request("POST", "/api/refresh", None)
    assert resp.status == 400  # path missing
    conn = http.client.HTTPConnection(srv.host, srv.port, timeout=5)
    conn.request("POST", "/api/settings", body="{not json",
                 headers={"X-Protonfs-Token": srv.token})
    assert conn.getresponse().status == 400
    conn.close()


def test_refresh_endpoint_forces_fresh(srv):
    events = srv.events()
    next(events)
    srv.json("GET", f"/api/tree?path={TARGET}&depth=1")
    assert next(events)[0] == "breakdown"
    n = len(srv.drive.calls)
    status, body = srv.json("POST", "/api/refresh", {"path": TARGET})
    assert status == 200 and body["queued"] is True
    assert next(events)[0] == "breakdown"
    assert len(srv.drive.calls) > n


def test_walk_progress_events_and_cancel(srv):
    events = srv.events()
    next(events)
    assert srv.json("POST", "/api/walk", {"paths": [TARGET]})[1]["started"] is True
    seen = []
    for name, data in events:
        if name == "progress":
            seen.append(data)
            if data["state"] != "running":
                break
    assert seen[-1]["state"] == "done" and seen[-1]["done"] >= 3
    # cancel before the next walk starts: the walk reports cancelled
    srv.json("POST", "/api/settings", {"refresh": "0"})  # force a real list call to block on
    gate = threading.Event()
    srv.drive.before("list", TARGET, lambda: gate.wait(5))
    assert srv.json("POST", "/api/walk", {"paths": [TARGET]})[1]["started"] is True
    assert srv.json("POST", "/api/walk/cancel", {})[1]["cancelled"] is True
    gate.set()
    for name, data in events:
        if name == "progress" and data["state"] != "running":
            break
    assert data["state"] == "cancelled"


def test_settings_roundtrip(srv):
    status, body = srv.json("GET", "/api/settings")
    assert status == 200 and body["target"] == TARGET and body["maxdepth"] == 3
    status, body = srv.json("POST", "/api/settings", {"maxdepth": 4, "dirs_only": True})
    assert status == 200 and body["maxdepth"] == 4
    assert srv.json("GET", "/api/settings")[1]["dirs_only"] is True
    status, body = srv.json("POST", "/api/settings", {"maxdepth": 50})
    assert status == 400 and "maxdepth" in body["error"]
    for bad in ({"refresh": 5}, {"refresh": None}, {"hide": ["a/**/[z-a]"]}, {"hide": "A"}):
        status, body = srv.json("POST", "/api/settings", bad)
        assert status == 400 and body["error"], bad
    assert srv.json("GET", f"/api/tree?path={TARGET}&depth=1&queue=0")[0] == 200


def test_invalid_exclusion_pattern_is_rejected_and_settings_kept(srv):
    status, _ = srv.json("POST", "/api/settings", {"freeze": ["keep"]})
    assert status == 200
    for bad in (["!"], ["\\"], ["a", "!"]):
        status, body = srv.json("POST", "/api/settings", {"hide": bad})
        assert status == 400 and body["error"], bad
    status, body = srv.json("GET", "/api/settings")
    assert (body["freeze"], body["hide"]) == (["keep"], [])
    assert srv.json("GET", "/api/root?queue=0")[0] == 200  # nothing 500s afterwards
    assert srv.json("GET", f"/api/tree?path={TARGET}&depth=1&queue=0")[0] == 200


def test_serve_refuses_invalid_initial_patterns(tmp_path):
    bad = GuiSettings(TARGET, freeze=["!"])
    *_, errors = _start(tmp_path, settings=bad)
    assert len(errors) == 1 and isinstance(errors[0], click.ClickException)
    assert "pattern" in errors[0].message


def test_root_includes_trash(srv):
    status, body = srv.json("GET", "/api/root")
    assert status == 200 and "trash" in body and body["rows"][0]["path"] == TARGET


def test_requests_are_logged_not_printed(srv, tmp_path):
    srv.request("GET", "/api/settings")
    assert "GET /api/settings" in (tmp_path / "server.log").read_text()
    assert srv.stdout.getvalue().count("\n") == 1


def test_port_in_use_error(tmp_path):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    port = blocker.getsockname()[1]
    try:
        *_, errors = _start(tmp_path, port=port)
    finally:
        blocker.close()
    assert isinstance(errors[0], click.ClickException)
    assert errors[0].message == f"port {port} is in use"


def test_non_loopback_warning(tmp_path):
    drive, engine, cache, out, err, stop, t, errors = _start(tmp_path, listen="0.0.0.0")
    try:
        assert not errors, errors
        assert "serving on 0.0.0.0:" in err.getvalue()
        assert "file names, sizes and the access token travel unencrypted over plain HTTP" in (
            err.getvalue()
        )
        # 0.0.0.0 is not an address to browse to: the URL uses loopback, and stderr says
        # to use this machine's address from elsewhere.
        assert out.getvalue().strip().splitlines()[-1].startswith("http://127.0.0.1:")
        assert "this machine's address" in err.getvalue()
    finally:
        stop.set()
        t.join(5)
        cache.close()


def test_loopback_has_no_warning(srv):
    assert srv.stderr.getvalue() == ""


def test_account_check_runs_before_listening(tmp_path, tree_drive):
    out = io.StringIO()
    seen = []
    # the URL is printed only once listening, so it must still be absent at the first call
    tree_drive.before("info", "/my-files", lambda: seen.append(out.getvalue()))
    _, _, cache, _, _, stop, t, errors = _start(tmp_path, drive=tree_drive, out=out)
    stop.set()
    t.join(5)
    cache.close()
    assert not errors
    assert seen == [""]  # check_account asked Drive before anything was printed


@pytest.mark.parametrize("text, want", [(None, None), ("auto", None), ("AUTO", None),
                                        ("8080", 8080), ("65535", 65535)])
def test_parse_port(text, want):
    assert parse_port(text) == want


@pytest.mark.parametrize("text", ["0", "65536", "-1", "http", ""])
def test_parse_port_rejects(text):
    with pytest.raises(click.BadParameter):
        parse_port(text)


def test_ipv6_loopback_url_is_bracketed(tmp_path):
    try:
        probe = socket.socket(socket.AF_INET6)
        probe.bind(("::1", 0))
        probe.close()
    except OSError:
        pytest.skip("no IPv6 loopback")
    for listen in ("::1", "[::1]"):
        _, _, cache, out, err, stop, t, errors = _start(tmp_path, listen=listen)
        try:
            assert not errors, errors
            assert out.getvalue().strip().splitlines()[-1].startswith("http://[::1]:")
            assert err.getvalue() == ""
        finally:
            stop.set()
            t.join(5)
            cache.close()


def test_unopenable_log_is_a_click_exception(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    log = resolve_log_target(str(blocker / "sub" / "x.log"), now=datetime.now())
    with pytest.raises(click.ClickException, match="cannot open log"):
        serve(None, GuiSettings(TARGET), log=log, stdout=io.StringIO(), stderr=io.StringIO())


def test_browser_only_with_tty_and_display(monkeypatch):
    from protonfs.webui.server import _can_open_browser

    class Tty(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert _can_open_browser(Tty()) is False
    monkeypatch.setenv("DISPLAY", ":0")
    assert _can_open_browser(Tty()) is True
    assert _can_open_browser(io.StringIO()) is False


def test_run_gui_end_to_end(tmp_path, tree_drive, monkeypatch):
    from protonfs.commands.du import DuOptions
    from protonfs.webui.server import run_gui

    cache = UsageCache(tmp_path / "du.sqlite")
    engine = UsageEngine(tree_drive, cache, clock=FakeClock())
    monkeypatch.setattr("protonfs.commands.du.default_engine", lambda: engine)
    out, err, stop = io.StringIO(), io.StringIO(), threading.Event()
    options = DuOptions(maxdepth=9, maxdepth_given=True, refresh="1h", dirs_only=True,
                        exclude=("tmp",),
                        exclude_updates=("frozen",), plan_size=1000,
                        log=str(tmp_path / "logs") + "/")
    result = []
    t = threading.Thread(
        target=lambda: result.append(
            run_gui(f"{TARGET}/A", options, stdout=out, stderr=err, stop=stop)),
        daemon=True)
    t.start()
    try:
        for _ in range(300):  # up to 15 s, like the other server starts
            if out.getvalue().strip():
                break
            threading.Event().wait(0.05)
        url = urlsplit(out.getvalue().strip().splitlines()[-1])
        srv = Running(tree_drive, engine, out, err, url.query[2:], url.hostname, url.port, t)
        assert url.hostname == "127.0.0.1"
        status, body = srv.json("GET", "/api/settings")
        assert status == 200
        assert body == {"target": f"{TARGET}/A", "refresh": "1h", "maxdepth": 6,
                        "dirs_only": True, "freeze": ["frozen"], "hide": ["tmp"],
                        "refresh_seconds": 3600.0}
        rows = srv.json("GET", f"/api/tree?path={TARGET}/A&depth=1&queue=0")[1]["rows"]
        assert rows[0]["pct_plan"] is None or rows[0]["pct_plan"] >= 0  # plan_size accepted
    finally:
        stop.set()
        t.join(5)
        cache.close()
    assert result == [0]
    (log,) = list((tmp_path / "logs").glob("du-gui-*.log"))
    assert "GET /api/settings" in log.read_text()


def test_slow_size_publishes_a_notice(srv):
    events = srv.events()
    next(events)
    srv.drive.on_slow_size("/my-files/A")
    for name, data in events:
        if name == "notice":
            break
    assert "rate-limiting folder sizes" in data["message"]


@pytest.mark.parametrize(
    "kwargs, want",
    [
        ({}, 3),  # no depth flag: the GUI default
        ({"maxdepth": 1, "maxdepth_given": True}, 1),  # an explicit -d 1 is honoured
        ({"maxdepth": 0, "maxdepth_given": True}, 1),  # clamped to the slider's range
        ({"maxdepth": 9, "maxdepth_given": True}, 6),
        ({"maxdepth": None, "maxdepth_given": True}, 6),  # -d 0- (no maximum)
    ],
)
def test_run_gui_depth_seed(kwargs, want, monkeypatch, tmp_path):
    from protonfs.commands.du import DuOptions
    from protonfs.webui import server as server_mod

    seen = {}
    monkeypatch.setattr("protonfs.commands.du.default_engine", lambda: object())
    monkeypatch.setattr(server_mod, "serve",
                        lambda engine, settings, **kw: seen.setdefault("s", settings))
    options = DuOptions(log=str(tmp_path / "logs") + "/", **kwargs)
    assert server_mod.run_gui("/my-files", options, stdout=io.StringIO(),
                              stderr=io.StringIO()) == 0
    assert seen["s"].maxdepth == want


def test_unexpected_host_header_is_rejected_on_loopback(srv):
    """A DNS-rebinding page reaches 127.0.0.1 under its own name: refuse it."""
    for host, ok in ((f"127.0.0.1:{srv.port}", True), (f"localhost:{srv.port}", True),
                     (f"evil.example:{srv.port}", False), ("127.0.0.1:1", False)):
        reply = srv.raw(f"GET / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n"
                        .encode())
        status = int(reply.split(b" ", 2)[1])
        assert (status == 200) is ok, (host, status)


def _log_text(tmp_path):
    return (tmp_path / "server.log").read_text()


def test_log_records_jobs_drive_calls_and_durations(srv, tmp_path):
    events = srv.events()
    next(events)
    srv.json("GET", f"/api/tree?path={TARGET}&depth=1")
    assert next(events)[0] == "breakdown"
    text = _log_text(tmp_path)
    assert f"job queued {TARGET}" in text
    assert f"job started {TARGET}" in text
    assert re.search(rf"job finished {TARGET} in \d+\.\d+s", text)
    assert re.search(rf"drive size {TARGET} \d+\.\d+s ok", text)
    assert re.search(rf"drive list {TARGET} \d+\.\d+s ok", text)


def test_log_records_errors_and_never_the_token(srv, tmp_path):
    events = srv.events()
    next(events)
    boom = RuntimeError(f"boom t={srv.token}")
    srv.drive.fail_next("list", f"{TARGET}/A", boom)
    srv.json("POST", "/api/refresh", {"path": f"{TARGET}/A"})
    assert next(events)[0] == "breakdown"
    text = _log_text(tmp_path)
    assert re.search(rf"drive list {TARGET}/A \d+\.\d+s error: ", text)
    assert f"job failed {TARGET}/A" in text
    assert srv.token not in text


def test_log_records_walks(srv, tmp_path):
    events = srv.events()
    next(events)
    srv.json("POST", "/api/walk", {"paths": [TARGET]})
    for name, data in events:
        if name == "progress" and data["state"] != "running":
            break
    text = _log_text(tmp_path)
    assert f"walk started {TARGET}" in text and "walk done" in text
