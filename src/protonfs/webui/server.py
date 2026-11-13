"""The local web server behind ``protonfs du --gui``.

:func:`serve` listens on a loopback port behind a random access token and maps HTTP routes
onto :class:`~protonfs.webui.api.Api`; :func:`run_gui` builds everything from the parsed
``du`` options. Every route except ``GET /`` needs the ``pfs_token`` cookie or an
``X-Protonfs-Token`` header; the token reaches the browser once, in the printed URL.

.. versionadded:: 3.4.0
"""

from __future__ import annotations

import errno
import html
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import socket
import threading
import webbrowser
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import TYPE_CHECKING, TextIO
from urllib.parse import parse_qs, urlsplit

import click

from .api import Api
from .jobs import JobQueue
from .logtarget import LogTarget, resolve_log_target
from .settings import GuiSettings

if TYPE_CHECKING:
    from protonfs.commands.du import DuOptions
    from protonfs.usage.engine import UsageEngine

COOKIE_PREFIX = "pfs_token_"
HEADER = "X-Protonfs-Token"
#: Marker in ``static/index.html`` replaced by the access token for an authorised request.
PLACEHOLDER = "{{PFS_TOKEN}}"
_MAX_BODY = 1 << 20
_REQUEST_TIMEOUT = 30.0
_SSE_WRITE_TIMEOUT = 60.0
_TOKEN_QUERY = re.compile(r"([?&]t=)[^&\s]*")


def parse_port(text: str | None) -> int | None:
    """Parse the ``--port`` value.

    :param text: ``None`` or ``"auto"`` (let the OS pick), or a port number as text.
    :returns: the port, or ``None`` for auto.
    :raises click.BadParameter: if ``text`` is not ``auto`` or an integer from 1 to 65535.

    .. versionadded:: 3.4.0
    """
    if text is None or text.strip().lower() == "auto":
        return None
    try:
        port = int(text)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        raise click.BadParameter(f"{text!r}: expected a port from 1 to 65535, or 'auto'")
    return port


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _LengthRequired(ValueError):
    pass


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, handler, *, api: Api, token: str, log: TextIO) -> None:
        if ":" in addr[0]:
            self.address_family = socket.AF_INET6
        self.api = api
        self.token = token
        self.log = log
        self.log_lock = threading.Lock()
        super().__init__(addr, handler)


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    timeout = _REQUEST_TIMEOUT

    # ----------------------------------------------------------------- plumbing

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - base signature
        self._log(format % args)

    def _log(self, text: str) -> None:
        text = _TOKEN_QUERY.sub(r"\1<redacted>", text)
        stamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        with self.server.log_lock:
            try:
                self.server.log.write(f"{stamp} {self.address_string()} {text}\n")
                self.server.log.flush()
            except (OSError, ValueError):
                pass

    @property
    def _cookie_name(self) -> str:
        return f"{COOKIE_PREFIX}{self.server.server_address[1]}"

    def _authorised(self, *, header_only: bool = False) -> bool:
        given = self.headers.get(HEADER)
        if given is None and not header_only:
            for part in (self.headers.get("Cookie") or "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == self._cookie_name:
                    given = value
        return given is not None and _token_ok(given, self.server.token)

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        return origin is None or origin == f"http://{self.headers.get('Host')}"

    def _send(self, status: int, body: bytes, ctype: str, headers: tuple = ()) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data) -> None:
        self._send(status, json.dumps(data).encode("utf-8"), "application/json")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    # ----------------------------------------------------------------- dispatch

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        url = urlsplit(self.path)
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        path = url.path
        try:
            if method == "GET" and path == "/":
                return self._index(query)
            if not self._authorised(header_only=method == "POST") or not self._origin_ok():
                return self._error(HTTPStatus.FORBIDDEN, "missing or wrong access token")
            if method == "GET" and path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if method == "GET" and path == "/api/events":
                return self._events()
            body = self._read_json() if method == "POST" else {}
            api = self.server.api
            routes = {
                ("GET", "/api/tree"): lambda: api.tree(
                    query.get("path", api.settings.target), _int(query.get("depth"), api),
                    queue=query.get("queue", "1") != "0",
                ),
                ("GET", "/api/root"): lambda: api.root(queue=query.get("queue", "1") != "0"),
                ("GET", "/api/settings"): api.get_settings,
                ("POST", "/api/settings"): lambda: api.set_settings(body),
                ("POST", "/api/refresh"): lambda: api.refresh(body.get("path")),
                ("POST", "/api/walk"): lambda: api.walk(body.get("paths")),
                ("POST", "/api/walk/cancel"): api.cancel_walk,
            }
            handler = routes.get((method, path))
            if handler is None:
                return self._error(HTTPStatus.NOT_FOUND, f"no route {method} {path}")
            self._json(HTTPStatus.OK, handler())
        except (BrokenPipeError, ConnectionResetError):
            return
        except _LengthRequired as exc:
            self._error(HTTPStatus.LENGTH_REQUIRED, str(exc))
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:  # noqa: BLE001 - report to the client, keep serving
            self._log(f"500 {method} {path}: {exc!r}")
            try:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}: {exc}")
            except OSError:
                pass

    def _read_json(self) -> dict:
        header = self.headers.get("Content-Length")
        if header is None:
            raise _LengthRequired("POST needs a Content-Length")
        try:
            length = int(header)
        except ValueError:
            length = -1
        if length < 0:
            raise ValueError("bad Content-Length")
        if length > _MAX_BODY:
            raise ValueError("request body too large")
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid JSON body: {exc}") from None
        if not isinstance(data, dict):
            raise ValueError("body must be a JSON object")
        return data

    # ------------------------------------------------------------------- routes

    def _index(self, query: dict) -> None:
        supplied = query.get("t")
        if supplied and _token_ok(supplied, self.server.token):
            # move the token out of the address bar and history: cookie, then plain "/"
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/")
            self.send_header(
                "Set-Cookie",
                f"{self._cookie_name}={self.server.token}; HttpOnly; SameSite=Strict; Path=/",
            )
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        page = (resources.files("protonfs.webui") / "static" / "index.html").read_text("utf-8")
        # the page needs the token for its POST requests (the cookie is HttpOnly); only a
        # request that already holds the cookie gets it, and the response is not cached
        token = html.escape(self.server.token, quote=True) if self._authorised() else ""
        page = page.replace(PLACEHOLDER, token)
        self._send(HTTPStatus.OK, page.encode("utf-8"), "text/html; charset=utf-8")

    def _static(self, name: str) -> None:
        parts = name.split("/")
        if not name or any(p in ("", ".", "..") or "\\" in p for p in parts):
            return self._error(HTTPStatus.NOT_FOUND, "no such file")
        node = resources.files("protonfs.webui") / "static"
        for p in parts:
            node = node / p
        if not node.is_file():
            return self._error(HTTPStatus.NOT_FOUND, "no such file")
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if name.endswith(".js"):
            ctype = "text/javascript"
        self._send(HTTPStatus.OK, node.read_bytes(), ctype)

    def _events(self) -> None:
        events = self.server.api.events()  # subscribe before anything is written
        try:
            self.connection.settimeout(_SSE_WRITE_TIMEOUT)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b": open\n\n")
            self.wfile.flush()
            for ev in events:
                if ev["event"] == "keepalive":
                    out = ": keepalive\n\n"
                else:
                    out = f"event: {ev['event']}\ndata: {json.dumps(ev['data'])}\n\n"
                self.wfile.write(out.encode("utf-8"))
                self.wfile.flush()
        except (OSError, ValueError, TypeError) as exc:
            if not isinstance(exc, (BrokenPipeError, ConnectionResetError)):
                self._log(f"event stream ended: {exc!r}")
        finally:
            events.close()
            self.close_connection = True


def _can_open_browser(stdout: TextIO) -> bool:
    return stdout.isatty() and bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _token_ok(given: str, token: str) -> bool:
    return secrets.compare_digest(given.encode("utf-8"), token.encode("utf-8"))


def _int(text: str | None, api: Api) -> int:
    if text is None:
        return api.settings.maxdepth
    try:
        return int(text)
    except ValueError:
        raise ValueError(f"depth {text!r} is not an integer") from None


def serve(
    engine: UsageEngine,
    settings: GuiSettings,
    *,
    listen: str = "127.0.0.1",
    port: int | None = None,
    log: LogTarget,
    open_browser: bool = False,
    stdout: TextIO,
    stderr: TextIO,
    ready: threading.Event | None = None,
    stop: threading.Event | None = None,
    plan_size: int | None = None,
) -> None:
    """Serve the GUI until ``KeyboardInterrupt`` or ``stop`` is set.

    The account check (:meth:`~protonfs.usage.engine.UsageEngine.check_account`) runs once
    before listening, so a switched account's cache is cleared before anything is answered.
    Once listening, the last line written to ``stdout`` is ``http://<host>:<port>/?t=<token>``
    (IPv6 hosts in brackets). Request logging goes to ``log``, never to ``stdout``.

    :param engine: the usage engine.
    :param settings: the initial settings.
    :param listen: address to bind; a non-loopback address prints a warning on ``stderr``.
    :param port: port to bind, or ``None`` for one chosen by the OS.
    :param log: where the request log goes.
    :param open_browser: open the URL in a browser (on a background thread), only when
        ``listen`` is loopback, ``stdout`` is a terminal and ``DISPLAY`` or
        ``WAYLAND_DISPLAY`` is set.
    :param stdout: receives the URL.
    :param stderr: receives warnings.
    :param ready: set once the server is listening and the URL is printed.
    :param stop: when set, the server shuts down and ``serve`` returns.
    :param plan_size: plan size in bytes for percentage columns, or ``None``.
    :raises click.ClickException: if the log cannot be opened, an explicit ``port`` is in
        use or the address cannot be bound.

    .. versionadded:: 3.4.0
    """
    try:
        stream = log.open()
    except OSError as exc:
        raise click.ClickException(f"cannot open log {log.path}: {exc}") from exc
    listen = listen.strip("[]")  # accept a bracketed IPv6 address
    token = secrets.token_urlsafe(24)
    holder: dict = {}
    httpd = None
    try:
        engine.check_account(settings.policy())
    except BaseException:
        stream.close()
        raise
    jobs = JobQueue(engine, lambda: holder["api"].settings)
    holder["api"] = api = Api(engine, jobs, settings, plan_size=plan_size)
    try:
        try:
            httpd = _Server(
                (listen, port or 0), _Handler, api=api, token=token, log=stream
            )
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE and port:
                raise click.ClickException(f"port {port} is in use") from exc
            raise click.ClickException(f"cannot listen on {listen}:{port or 0}: {exc}") from exc
        bound = httpd.server_address[1]
        host = f"[{listen.replace('%', '%25')}]" if ":" in listen else listen
        url = f"http://{host}:{bound}/?t={token}"
        loopback = _is_loopback(listen)
        if not loopback:
            stderr.write(
                f"warning: serving on {host}:{bound}: file names, sizes and the access token "
                "travel unencrypted over plain HTTP\n"
            )
            stderr.flush()
        stream.write(f"serving on {host}:{bound}\n")
        stdout.write(url + "\n")
        stdout.flush()
        if open_browser and loopback and _can_open_browser(stdout):
            threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
        if stop is not None:
            threading.Thread(
                target=lambda: (stop.wait(), httpd.shutdown()), daemon=True
            ).start()
        if ready is not None:
            ready.set()
        httpd.serve_forever(poll_interval=0.1)
    finally:
        jobs.close()
        if httpd is not None:
            httpd.server_close()
        stream.close()


def run_gui(
    target_remote: str,
    options: DuOptions,
    *,
    stdout: TextIO,
    stderr: TextIO,
    stop: threading.Event | None = None,
) -> int:
    """Run ``du --gui`` for ``target_remote`` with the parsed ``options``.

    Builds the production engine, seeds :class:`~protonfs.webui.settings.GuiSettings` from
    the options (nesting depth ``options.maxdepth`` when the user raised it above the CLI
    default of 1, else 3), and serves until interrupted.

    :param target_remote: the escaped Drive path to open the GUI on.
    :param options: the parsed ``du`` options (``listen``, ``port`` and ``log`` are used).
    :param stdout: receives the URL.
    :param stderr: receives warnings.
    :param stop: when set, the server shuts down and this returns ``0``.
    :returns: the exit code: ``0`` after ``KeyboardInterrupt`` or a clean stop.
    :raises click.BadParameter: for an invalid ``--port``.
    :raises click.ClickException: if the port is in use or the address cannot be bound.

    .. versionadded:: 3.4.0
    """
    from protonfs.commands.du import default_engine

    from .settings import DEPTH_RANGE

    port = parse_port(options.port)
    depth = options.maxdepth if options.maxdepth and options.maxdepth > 1 else 3
    settings = GuiSettings(
        target=target_remote,
        refresh=options.refresh,
        maxdepth=min(depth, DEPTH_RANGE[1]),
        dirs_only=options.dirs_only,
        freeze=list(options.exclude_updates),
        hide=list(options.exclude),
    )
    log = resolve_log_target(options.log, now=datetime.now())
    try:
        serve(
            default_engine(), settings, listen=options.listen or "127.0.0.1", port=port,
            log=log, open_browser=True, stdout=stdout, stderr=stderr,
            plan_size=options.plan_size, stop=stop,
        )
    except KeyboardInterrupt:
        return 0
    return 0


__all__ = ["parse_port", "run_gui", "serve"]
