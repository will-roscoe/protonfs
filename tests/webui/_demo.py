"""Shared helper for the browser tests: a real ``serve()`` on a TreeDrive, run in a thread."""

from __future__ import annotations

import io
import threading
from contextlib import contextmanager
from datetime import datetime

from tests.usage.conftest import FakeClock

from protonfs.usage.cache import UsageCache
from protonfs.usage.engine import UsageEngine
from protonfs.webui.logtarget import resolve_log_target
from protonfs.webui.server import serve


class Demo:
    def __init__(self, url, drive, engine):
        self.url, self.drive, self.engine = url, drive, engine


@contextmanager
def serve_demo(tmp_path, drive, settings):
    """Serve ``drive`` with ``settings`` on a free port; yield a :class:`Demo`; stop on exit."""
    drive.on_slow_size = None  # lets serve() wire the throttle notice
    cache = UsageCache(tmp_path / "du.sqlite")
    engine = UsageEngine(drive, cache, clock=FakeClock())
    out, err = io.StringIO(), io.StringIO()
    ready, stop = threading.Event(), threading.Event()
    log = resolve_log_target(str(tmp_path / "server.log"), now=datetime.now())
    errors = []

    def run():
        try:
            serve(engine, settings, port=None, log=log, open_browser=False, stdout=out,
                  stderr=err, ready=ready, stop=stop)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
            ready.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    try:
        assert ready.wait(15), "server did not start"
        assert not errors, errors
        yield Demo(out.getvalue().strip().splitlines()[-1], drive, engine)
    finally:
        stop.set()
        t.join(5)
        cache.close()
