# Verbosity + Event Log Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add layered `-v`/`-vv`/`-vvv`/`-vvvv` console verbosity, a config-toggled rotating event-log file, and a progress render-style flag, with curated non-spammy narration wired through the whole package.

**Architecture:** Two layers feeding one event log (spec Approach A). A `Reporter` (`protonfs/reporting.py`) renders curated phase/progress/item/warn/done narration to stderr, gated and throttled by level. The existing stdlib `logging` calls are the diagnostic tier. `protonfs/logs.py` wires console + rotating-file handlers and builds the Reporter. Command cores take `reporter: Reporter | None = None` and resolve `get_reporter()`; the CLI group callback configures everything.

**Tech Stack:** Python 3.9+, Click, stdlib `logging` (`RotatingFileHandler`), pytest, ruff.

## Global Constraints

- Python floor 3.9 (no `match`, no `X | Y` runtime unions in non-annotation positions; `from __future__ import annotations` is already used everywhere).
- Line length 100 (ruff `E501`); ruff selects `E,F,I,UP`.
- All human narration → **stderr**; command result-summary lines stay on **stdout** unchanged.
- Docstrings required (interrogate gate 80%, repo runs ~100%); every public function/class/module gets one.
- Verbosity `-v` count is CLI-only; `event_log` + `progress_style` are config keys (`defaults.*`) with env overrides `PROTONFS_EVENT_LOG` / `PROTONFS_PROGRESS_STYLE`.
- Event log: `.protonfs/events.log` (+`.log.1`), `RotatingFileHandler(maxBytes=5*1024*1024, backupCount=1)`, DEBUG, aligned-text format.
- Level→level map: console logging WARNING for levels 0–2, INFO at 3, DEBUG at 4; reporter renders at ≥1 (phases/progress/done/warn), ≥2 adds `item()`; throttle interval `{1:30.0, 2:5.0, 3:1.0, 4:0.0}` seconds.
- Conventional-commit messages; one trailer `Helped-By: claude-opus-4-8 <anthropic@willroscoe.uk>`.
- Do NOT commit anything under `.claude/`.

---

## Task 1: Config keys `event_log` + `progress_style`

**Files:**
- Modify: `src/protonfs/config.py` (`Defaults` dataclass ~L42; `_ENV_DEFAULTS_OVERRIDES` ~L36; `Config.from_dict` ~L100; `_env_layer` ~L300)
- Modify: `src/protonfs/commands/config.py` (`KNOWN_KEYS`, `_BOOL_KEYS` ~L37; add choice validation in `_coerce_value`)
- Test: `tests/test_config.py`, `tests/test_cli_config.py`

**Interfaces:**
- Produces: `Defaults.event_log: bool = False`, `Defaults.progress_style: str = "inline"`; env `PROTONFS_EVENT_LOG`, `PROTONFS_PROGRESS_STYLE`; config keys `defaults.event_log`, `defaults.progress_style`.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_config.py  (append)
def test_defaults_have_event_log_and_progress_style() -> None:
    from protonfs.config import Defaults
    d = Defaults()
    assert d.event_log is False
    assert d.progress_style == "inline"


def test_from_dict_reads_new_defaults(tmp_path) -> None:
    from protonfs.config import Config
    cfg = Config.from_dict(
        {"remote_root": "/x", "defaults": {"event_log": True, "progress_style": "lines"}}
    )
    assert cfg.defaults.event_log is True
    assert cfg.defaults.progress_style == "lines"


def test_env_overrides_event_log_and_progress_style(monkeypatch, tmp_path) -> None:
    from protonfs.config import init_config, load_layered_config
    init_config(tmp_path, "/my-files/test")
    monkeypatch.setenv("PROTONFS_EVENT_LOG", "true")
    monkeypatch.setenv("PROTONFS_PROGRESS_STYLE", "lines")
    cfg = load_layered_config(tmp_path)
    assert cfg.defaults.event_log is True
    assert cfg.defaults.progress_style == "lines"
```

```python
# tests/test_cli_config.py  (append)
def test_config_set_progress_style_rejects_bad_value(tmp_path, monkeypatch) -> None:
    import click, pytest
    from protonfs.commands.config import config_set
    from protonfs.config import init_config
    init_config(tmp_path, "/my-files/test")
    with pytest.raises(click.ClickException):
        config_set(tmp_path, "defaults.progress_style", "sideways")


def test_config_set_event_log_bool(tmp_path) -> None:
    from protonfs.commands.config import config_get, config_set
    from protonfs.config import init_config
    init_config(tmp_path, "/my-files/test")
    config_set(tmp_path, "defaults.event_log", "on")
    assert config_get(tmp_path, "defaults.event_log") == "True"
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/test_config.py::test_defaults_have_event_log_and_progress_style tests/test_cli_config.py::test_config_set_progress_style_rejects_bad_value -q`
Expected: FAIL (`AttributeError` / no validation).

- [ ] **Step 3: Implement in `config.py`**

In `Defaults` dataclass add fields (keep docstring updated):
```python
    on_conflict: str = "skip"
    low_io: bool = False
    event_log: bool = False
    progress_style: str = "inline"
```
Add to `_ENV_DEFAULTS_OVERRIDES`:
```python
_ENV_DEFAULTS_OVERRIDES = {
    "on_conflict": "PROTONFS_ON_CONFLICT",
    "low_io": "PROTONFS_LOW_IO",
    "event_log": "PROTONFS_EVENT_LOG",
    "progress_style": "PROTONFS_PROGRESS_STYLE",
}
```
In `Config.from_dict`, extend the `Defaults(...)` build:
```python
            defaults=Defaults(
                on_conflict=defaults_data.get("on_conflict", "skip"),
                low_io=defaults_data.get("low_io", False),
                event_log=defaults_data.get("event_log", False),
                progress_style=defaults_data.get("progress_style", "inline"),
            ),
```
In `_env_layer`, the defaults loop must bool-parse `event_log` too:
```python
        defaults_layer[key] = (
            _parse_bool_env(value) if key in ("low_io", "event_log") else value
        )
```

- [ ] **Step 4: Implement in `commands/config.py`**

```python
KNOWN_KEYS = (
    "remote_root",
    "device_id",
    "defaults.on_conflict",
    "defaults.low_io",
    "defaults.event_log",
    "defaults.progress_style",
)
_BOOL_KEYS = {"defaults.low_io", "defaults.event_log"}
_CHOICE_KEYS = {"defaults.progress_style": ("inline", "lines")}
```
Extend `_coerce_value` to validate choices:
```python
def _coerce_value(key: str, raw: str):
    """Coerce a raw string CLI value to the type the config key expects."""
    if key in _BOOL_KEYS:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if key in _CHOICE_KEYS:
        choices = _CHOICE_KEYS[key]
        if raw not in choices:
            raise click.ClickException(
                f"invalid value {raw!r} for {key}; choose one of {', '.join(choices)}."
            )
    return raw
```

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_config.py tests/test_cli_config.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/protonfs/config.py src/protonfs/commands/config.py tests/test_config.py tests/test_cli_config.py
git commit -m "feat(config): add defaults.event_log + defaults.progress_style keys

Helped-By: claude-opus-4-8 <anthropic@willroscoe.uk>"
```

---

## Task 2: The Reporter (`protonfs/reporting.py`)

**Files:**
- Create: `src/protonfs/reporting.py`
- Test: `tests/test_reporting.py`

**Interfaces:**
- Produces:
  - `class Reporter(level: int, *, progress_style: str = "inline", stream=sys.stderr, isatty: bool | None = None)`
  - methods `phase(name: str, **fields)`, `progress(done: int, total: int, **fields)`, `item(action: str, path: str)`, `warn(msg: str)`, `done(msg: str, **fields)`, `timed(name: str)` (contextmanager yielding None), property `level`.
  - `THROTTLE = {0: 0.0, 1: 30.0, 2: 5.0, 3: 1.0, 4: 0.0}`
  - `get_reporter() -> Reporter`, `set_reporter(r: Reporter) -> None`, `null_reporter() -> Reporter` (level 0).
  - Consumed by Tasks 3, 4, 5.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_reporting.py
from __future__ import annotations
import io
import logging
import time
import pytest
from protonfs.reporting import Reporter, get_reporter, null_reporter, set_reporter


def _reporter(level, style="lines", isatty=False):
    return Reporter(level, progress_style=style, stream=io.StringIO(), isatty=isatty)


def test_level_zero_is_silent_on_stream() -> None:
    r = _reporter(0)
    r.phase("indexing")
    r.progress(1, 10)
    r.item("download", "a/b.txt")
    r.done("done")
    assert r._stream.getvalue() == ""


def test_level_one_shows_phase_and_done_but_not_items() -> None:
    r = _reporter(1)
    r.phase("downloading", files=200)
    r.item("download", "a/b.txt")   # items only at >=2
    r.done("downloaded 200 files")
    out = r._stream.getvalue()
    assert "downloading" in out
    assert "downloaded 200 files" in out
    assert "a/b.txt" not in out


def test_level_two_shows_items() -> None:
    r = _reporter(2)
    r.item("download", "a/b.txt")
    assert "a/b.txt" in r._stream.getvalue()


def test_progress_throttled_within_interval_but_forced_on_done(monkeypatch) -> None:
    r = _reporter(1)  # interval 30s
    t = [1000.0]
    monkeypatch.setattr("protonfs.reporting.time.monotonic", lambda: t[0])
    r.progress(1, 10)      # first always renders
    r.progress(2, 10)      # within 30s -> suppressed
    first = r._stream.getvalue()
    assert first.count("\n") == 1
    t[0] += 31
    r.progress(3, 10)      # now past interval -> renders
    assert r._stream.getvalue().count("\n") == 2


def test_lines_style_appends_newlines() -> None:
    r = _reporter(1, style="lines")
    r.progress(1, 10)
    r.progress(2, 10, force=True)
    out = r._stream.getvalue()
    assert "\r" not in out
    assert out.count("\n") == 2


def test_inline_style_uses_carriage_return_on_tty() -> None:
    r = _reporter(1, style="inline", isatty=True)
    r.progress(1, 10)
    r.progress(2, 10, force=True)
    out = r._stream.getvalue()
    assert "\r" in out  # rewrites the same line


def test_non_tty_forces_lines_even_when_inline_requested() -> None:
    r = _reporter(1, style="inline", isatty=False)
    r.progress(1, 10)
    r.progress(2, 10, force=True)
    assert "\r" not in r._stream.getvalue()


def test_open_progress_line_closed_before_other_output() -> None:
    r = _reporter(2, style="inline", isatty=True)
    r.progress(1, 10)          # opens an inline progress line
    r.item("download", "a.txt")  # must close it with a newline first
    out = r._stream.getvalue()
    # the progress line is terminated before the item line
    idx_nl = out.index("\n")
    assert out.index("a.txt") > idx_nl


def test_emits_to_events_logger(caplog) -> None:
    r = _reporter(1)
    with caplog.at_level(logging.DEBUG, logger="protonfs.events"):
        r.phase("indexing", files=5)
    assert any("indexing" in rec.getMessage() for rec in caplog.records)


def test_get_set_reporter_roundtrip() -> None:
    assert get_reporter().level == 0  # default null
    r = _reporter(3)
    set_reporter(r)
    assert get_reporter() is r
    set_reporter(null_reporter())  # reset for other tests


def test_timed_reports_duration() -> None:
    r = _reporter(1)
    with r.timed("scanning"):
        pass
    assert "scanning" in r._stream.getvalue()
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/test_reporting.py -q`
Expected: FAIL (`ModuleNotFoundError: protonfs.reporting`).

- [ ] **Step 3: Implement `src/protonfs/reporting.py`**

```python
# src/protonfs/reporting.py
"""Curated, level-gated progress narration for the CLI (the Reporter layer).

Commands call a :class:`Reporter` at phase boundaries (``phase``/``progress``/``item``/
``warn``/``done``); it renders human lines to stderr, gated by verbosity level and
throttled so ``-v`` stays readable, and mirrors every call to the ``protonfs.events``
logger so the event-log file captures the narration even when the console is quiet.
See ``.claude`` spec 2026-07-17-verbosity-and-event-log.
"""
from __future__ import annotations

import contextlib
import logging
import sys
import time

# Seconds between rendered progress updates, by verbosity level (0 = every update).
THROTTLE = {0: 0.0, 1: 30.0, 2: 5.0, 3: 1.0, 4: 0.0}
_MAX_LEVEL = 4

_events_logger = logging.getLogger("protonfs.events")


def _fields(fields: dict) -> str:
    """Render ``key=value`` trailing fields (sorted, space-joined); empty when none."""
    return " ".join(f"{k}={v}" for k, v in fields.items())


class Reporter:
    """Renders verbosity-gated narration to a stream and mirrors it to the event log.

    :param level: verbosity 0–4 (clamped); 0 is silent on the stream but still logs.
    :param progress_style: ``"inline"`` (rewrite one line) or ``"lines"`` (new line each).
    :param stream: where human lines go (defaults to real stderr).
    :param isatty: force TTY-ness for the inline/lines decision (defaults to the
        stream's own ``isatty()``); a non-TTY always uses ``"lines"``.
    """

    def __init__(self, level, *, progress_style="inline", stream=None, isatty=None):
        self.level = max(0, min(int(level), _MAX_LEVEL))
        self._stream = stream if stream is not None else sys.stderr
        tty = isatty if isatty is not None else getattr(self._stream, "isatty", lambda: False)()
        self._inline = progress_style == "inline" and tty
        self._interval = THROTTLE[self.level]
        self._last_progress = 0.0
        self._progress_open = False  # an inline progress line awaits a newline

    def _close_progress(self) -> None:
        """End an open inline progress line so the next output starts cleanly."""
        if self._progress_open:
            self._stream.write("\n")
            self._progress_open = False

    def _emit(self, text: str) -> None:
        """Write a normal (non-progress) line, closing any open progress line first."""
        self._close_progress()
        self._stream.write(text + "\n")
        self._stream.flush()

    def phase(self, name: str, **fields) -> None:
        """Announce a new phase (e.g. ``"downloading"``). Rendered at level >= 1."""
        _events_logger.info("%s %s", name, _fields(fields))
        if self.level >= 1:
            suffix = f" {_fields(fields)}" if fields else ""
            self._emit(f"{name}{suffix}")

    def progress(self, done: int, total: int, *, force: bool = False, **fields) -> None:
        """Report throttled progress. Rendered at level >= 1; ``force`` bypasses the
        throttle (used at phase completion)."""
        pct = int(done / total * 100) if total else 100
        _events_logger.debug("progress %s/%s (%s%%) %s", done, total, pct, _fields(fields))
        if self.level < 1:
            return
        now = time.monotonic()
        if not force and self._interval and (now - self._last_progress) < self._interval:
            return
        self._last_progress = now
        line = f"{done}/{total} ({pct}%)"
        if fields:
            line += f" {_fields(fields)}"
        if self._inline:
            self._stream.write("\r" + line)
            self._stream.flush()
            self._progress_open = True
        else:
            self._stream.write(line + "\n")
            self._stream.flush()

    def item(self, action: str, path: str) -> None:
        """Report a single transferred/affected item. Rendered at level >= 2."""
        _events_logger.debug("%s %s", action, path)
        if self.level >= 2:
            self._emit(f"  {action} {path}")

    def warn(self, msg: str) -> None:
        """Surface a warning at every level (always shown)."""
        _events_logger.warning("%s", msg)
        self._emit(f"! {msg}")

    def done(self, msg: str, **fields) -> None:
        """Announce phase/command completion. Rendered at level >= 1."""
        _events_logger.info("done %s %s", msg, _fields(fields))
        if self.level >= 1:
            suffix = f" {_fields(fields)}" if fields else ""
            self._emit(f"{msg}{suffix}")

    @contextlib.contextmanager
    def timed(self, name: str):
        """Context manager: ``phase(name)`` on enter, ``done(... took Ns)`` on exit."""
        self.phase(name)
        start = time.monotonic()
        try:
            yield
        finally:
            self.done(f"{name} done", took=f"{time.monotonic() - start:.1f}s")


def null_reporter() -> Reporter:
    """A silent level-0 Reporter (still forwards warnings + to the event log)."""
    return Reporter(0)


_current: Reporter = null_reporter()


def get_reporter() -> Reporter:
    """Return the process-wide Reporter (a null one until :func:`set_reporter`)."""
    return _current


def set_reporter(reporter: Reporter) -> None:
    """Install the process-wide Reporter (called by the CLI group callback)."""
    global _current
    _current = reporter
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_reporting.py -q`
Expected: PASS (11 tests).

- [ ] **Step 5: Lint + commit**

```bash
python -m ruff check src/protonfs/reporting.py tests/test_reporting.py
git add src/protonfs/reporting.py tests/test_reporting.py
git commit -m "feat(reporting): add level-gated Reporter with throttled progress + event mirror

Helped-By: claude-opus-4-8 <anthropic@willroscoe.uk>"
```

---

## Task 3: Logging setup (`protonfs/logs.py`)

**Files:**
- Create: `src/protonfs/logs.py`
- Test: `tests/test_logs.py`

**Interfaces:**
- Consumes: `Reporter`, `set_reporter` (Task 2).
- Produces:
  - `configure_logging(verbosity: int, *, progress_style: str, event_log: bool, root: Path, stream=None) -> Reporter`
  - `EVENT_LOG_NAME = "events.log"`, `EVENT_LOG_MAX_BYTES = 5*1024*1024`, `EVENT_LOG_BACKUPS = 1`
  - `backend_passthrough_enabled() -> bool` (True at level 4; read by `drive.py` in Task 5).
  - `_console_level(verbosity) -> int` mapping {0,1,2→WARNING, 3→INFO, 4→DEBUG}.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_logs.py
from __future__ import annotations
import io
import logging
from pathlib import Path
import pytest
from protonfs.logs import (
    EVENT_LOG_NAME, backend_passthrough_enabled, configure_logging, _console_level,
)


@pytest.fixture(autouse=True)
def _reset_logging():
    yield
    logging.getLogger("protonfs").handlers.clear()
    from protonfs.reporting import set_reporter, null_reporter
    set_reporter(null_reporter())


def test_console_level_mapping() -> None:
    assert _console_level(0) == logging.WARNING
    assert _console_level(2) == logging.WARNING
    assert _console_level(3) == logging.INFO
    assert _console_level(4) == logging.DEBUG


def test_configure_returns_reporter_at_level(tmp_path) -> None:
    r = configure_logging(2, progress_style="lines", event_log=False, root=tmp_path)
    assert r.level == 2
    from protonfs.reporting import get_reporter
    assert get_reporter() is r


def test_event_log_file_created_only_when_enabled(tmp_path) -> None:
    (tmp_path / ".protonfs").mkdir()
    configure_logging(1, progress_style="lines", event_log=False, root=tmp_path)
    logging.getLogger("protonfs.events").info("hello")
    assert not (tmp_path / ".protonfs" / EVENT_LOG_NAME).exists()

    logging.getLogger("protonfs").handlers.clear()
    configure_logging(1, progress_style="lines", event_log=True, root=tmp_path)
    logging.getLogger("protonfs.events").info("world")
    log = tmp_path / ".protonfs" / EVENT_LOG_NAME
    assert log.exists()
    assert "world" in log.read_text()


def test_event_log_uses_aligned_format(tmp_path) -> None:
    (tmp_path / ".protonfs").mkdir()
    configure_logging(1, progress_style="lines", event_log=True, root=tmp_path)
    logging.getLogger("protonfs.events").info("start subpath=a")
    text = (tmp_path / ".protonfs" / EVENT_LOG_NAME).read_text()
    assert "INFO" in text and "start subpath=a" in text


def test_backend_passthrough_only_at_level_4(tmp_path) -> None:
    configure_logging(3, progress_style="lines", event_log=False, root=tmp_path)
    assert backend_passthrough_enabled() is False
    logging.getLogger("protonfs").handlers.clear()
    configure_logging(4, progress_style="lines", event_log=False, root=tmp_path)
    assert backend_passthrough_enabled() is True
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/test_logs.py -q`
Expected: FAIL (`ModuleNotFoundError: protonfs.logs`).

- [ ] **Step 3: Implement `src/protonfs/logs.py`**

```python
# src/protonfs/logs.py
"""Wire the console + event-log handlers and build the process Reporter.

Console verbosity and the event-log file are independent sinks (spec): ``-v`` sets the
console threshold, while the event log -- when enabled -- always records the full
``protonfs`` logger tree at DEBUG. Level 4 additionally ungags third-party loggers and
turns on proton-drive subprocess passthrough (read via :func:`backend_passthrough_enabled`).
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from protonfs.reporting import Reporter, set_reporter

EVENT_LOG_NAME = "events.log"
EVENT_LOG_MAX_BYTES = 5 * 1024 * 1024
EVENT_LOG_BACKUPS = 1
_ALIGNED_FMT = "%(asctime)s %(levelname)-5s %(name)-24s %(message)s"
_DATE_FMT = "%Y-%m-%dT%H:%M:%SZ"

_ROOT = "protonfs"
_backend_passthrough = False


def _console_level(verbosity: int) -> int:
    """Map a ``-v`` count to the console logging threshold (WARNING/INFO/DEBUG)."""
    if verbosity >= 4:
        return logging.DEBUG
    if verbosity == 3:
        return logging.INFO
    return logging.WARNING


def backend_passthrough_enabled() -> bool:
    """Whether ``drive.py`` should stream the proton-drive subprocess stderr (level 4)."""
    return _backend_passthrough


def _make_formatter() -> logging.Formatter:
    fmt = logging.Formatter(_ALIGNED_FMT, datefmt=_DATE_FMT)
    fmt.converter = __import__("time").gmtime  # UTC timestamps
    return fmt


def configure_logging(
    verbosity: int, *, progress_style: str, event_log: bool, root: Path, stream=None
) -> Reporter:
    """Configure handlers + build/install the Reporter for this invocation.

    :param verbosity: ``-v`` count (0–4).
    :param progress_style: ``"inline"`` | ``"lines"`` for the Reporter.
    :param event_log: when true, attach a rotating DEBUG file handler under ``.protonfs/``.
    :param root: the repo root whose ``.protonfs/`` holds the event log.
    :param stream: console/reporter stream override (tests); defaults to real stderr.
    :returns: the installed :class:`Reporter`.
    """
    global _backend_passthrough
    _backend_passthrough = verbosity >= 4

    root_logger = logging.getLogger(_ROOT)
    root_logger.setLevel(logging.DEBUG)  # handlers filter; logger passes everything
    root_logger.handlers.clear()
    root_logger.propagate = False

    console = logging.StreamHandler(stream)
    console.setLevel(_console_level(verbosity))
    console.setFormatter(_make_formatter())
    root_logger.addHandler(console)

    if event_log:
        (root / ".protonfs").mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            root / ".protonfs" / EVENT_LOG_NAME,
            maxBytes=EVENT_LOG_MAX_BYTES,
            backupCount=EVENT_LOG_BACKUPS,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(_make_formatter())
        root_logger.addHandler(file_handler)

    if verbosity >= 4:
        logging.getLogger().setLevel(logging.DEBUG)  # ungag third-party root

    reporter = Reporter(verbosity, progress_style=progress_style, stream=stream)
    set_reporter(reporter)
    return reporter
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_logs.py -q`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
python -m ruff check src/protonfs/logs.py tests/test_logs.py
git add src/protonfs/logs.py tests/test_logs.py
git commit -m "feat(logs): configure console + rotating event-log handlers, build Reporter

Helped-By: claude-opus-4-8 <anthropic@willroscoe.uk>"
```

---

## Task 4: CLI wiring — global options + group callback

**Files:**
- Modify: `src/protonfs/cli.py` (add group params + callback; remove `_progress_printer` ~L69-88 and its use in `push`/`pull`)
- Test: `tests/test_cli.py`, `tests/test_cli_surface.py`

**Interfaces:**
- Consumes: `configure_logging` (Task 3), `get_reporter` (Task 2).
- Produces: `protonfs [-v...] [--progress-inline/--progress-lines] [--event-log/--no-event-log] <cmd>`. Group callback resolves flags→config and calls `configure_logging`. `_progress_printer` removed.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_cli.py  (append)
def test_cli_verbose_count_configures_reporter(tmp_path, monkeypatch, make_fake_drive) -> None:
    from click.testing import CliRunner
    from protonfs.cli import main
    from protonfs.config import init_config
    from protonfs.context import load_context
    from protonfs.reporting import get_reporter
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path); ctx.drive = make_fake_drive()
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)
    captured = {}
    monkeypatch.setattr(
        "protonfs.commands.status.compute_status",
        lambda c, p: captured.setdefault("lvl", get_reporter().level) or __import__("collections").Counter(),
    )
    CliRunner().invoke(main, ["-vv", "status"])
    assert captured["lvl"] == 2


def test_cli_event_log_flag_writes_file(tmp_path, monkeypatch, make_fake_drive) -> None:
    from click.testing import CliRunner
    from protonfs.cli import main
    from protonfs.config import init_config
    from protonfs.context import load_context
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path); ctx.drive = make_fake_drive()
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)
    monkeypatch.chdir(tmp_path)
    CliRunner().invoke(main, ["-v", "--event-log", "status"])
    assert (tmp_path / ".protonfs" / "events.log").exists()


def test_cli_no_verbose_stdout_unchanged(tmp_path, monkeypatch, make_fake_drive) -> None:
    # Regression: default invocation still prints exactly the state counts on stdout.
    from click.testing import CliRunner
    from protonfs.cli import main
    from protonfs.config import init_config
    from protonfs.context import load_context
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path); ctx.drive = make_fake_drive()
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)
    result = CliRunner().invoke(main, ["status"])
    assert "synced: 0" in result.output
```

```python
# tests/test_cli_surface.py  (append)
def test_group_level_global_options_are_frozen() -> None:
    import click
    from protonfs.cli import main
    opts = set()
    for p in main.params:
        if isinstance(p, click.Option):
            opts.update(p.opts); opts.update(p.secondary_opts)
    assert {"-v", "--verbose", "--progress-inline", "--progress-lines",
            "--event-log", "--no-event-log"} <= opts
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/test_cli.py::test_cli_verbose_count_configures_reporter tests/test_cli_surface.py::test_group_level_global_options_are_frozen -q`
Expected: FAIL (no such options).

- [ ] **Step 3: Implement group options + callback in `cli.py`**

Replace the `main` group definition:
```python
@click.group()
@click.version_option(__version__, prog_name="protonfs")
@click.option("-v", "--verbose", count=True, help="Increase console detail (-v..-vvvv).")
@click.option(
    "--progress-inline/--progress-lines",
    "progress_inline",
    default=None,
    help="Update progress in place (inline) vs. print each poll on a new line. "
    "Default: config (defaults.progress_style), else inline on a TTY.",
)
@click.option(
    "--event-log/--no-event-log",
    "event_log",
    default=None,
    help="Write a full debug event log to .protonfs/events.log. "
    "Default: config (defaults.event_log), else off.",
)
def main(verbose: int, progress_inline: bool | None, event_log: bool | None) -> None:
    """Sync a local directory tree with Proton Drive."""
    from pathlib import Path

    from protonfs.config import load_layered_config
    from protonfs.logs import configure_logging

    # Resolve flag -> config -> built-in default for the two persisted knobs.
    cfg = load_layered_config(Path.cwd())
    cfg_style = cfg.defaults.progress_style if cfg else "inline"
    cfg_event = cfg.defaults.event_log if cfg else False
    if progress_inline is None:
        style = cfg_style
    else:
        style = "inline" if progress_inline else "lines"
    use_event_log = cfg_event if event_log is None else event_log
    configure_logging(
        verbose, progress_style=style, event_log=use_event_log, root=Path.cwd()
    )
```

- [ ] **Step 4: Remove `_progress_printer` and rewire push/pull**

Delete the `_progress_printer` function (~L69-88). In `push` and `pull`, replace `progress = _progress_printer("push")` / `_progress_printer("pull")` and the `on_progress=progress` args with `on_progress=None` for now (Task 5 rewires them to the reporter). Keep the commands compiling:
```python
    # push:
    with repo_lock(ctx.root):
        for subpath in _normalize_paths(path):
            _accumulate_transfer(result, push_files(ctx, subpath, resolve, dry_run))
    # pull: likewise drop the progress kwarg
```

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_cli.py tests/test_cli_surface.py -q`
Expected: PASS.

- [ ] **Step 6: Update the #93 progress tests**

In `tests/commands/test_pull.py`, delete `test_pull_cli_progress_silent_when_stderr_not_a_tty`, `test_progress_printer_renders_on_tty`, `test_progress_printer_disabled_off_tty` (the `_progress_printer` helper is gone; the Reporter's inline/lines behaviour is covered in `tests/test_reporting.py`). Keep `test_pull_reports_progress_per_batch` but it will be rewired in Task 5.

Run: `python -m pytest tests/ -q`
Expected: PASS (progress-printer tests removed).

- [ ] **Step 7: Commit**

```bash
git add src/protonfs/cli.py tests/
git commit -m "feat(cli): add -v/--progress/--event-log globals + logging group callback

Removes the #93 _progress_printer; progress moves to the Reporter (Task 5).

Helped-By: claude-opus-4-8 <anthropic@willroscoe.uk>"
```

---

## Task 5: Instrument commands + drive backend

Wire `reporter` through the command cores and add narration. Each command core gains a
`reporter: Reporter | None = None` parameter resolved as `reporter or get_reporter()`.
Do this as sub-commits; each sub-task is independently testable.

**Files:**
- Modify: `src/protonfs/commands/{pull,push,refresh,offload,status,ls,rm,restore,trash,setup,upgrade}.py`, `src/protonfs/commands/install_drive` path (in `install.py`), `src/protonfs/drive.py`
- Modify: `src/protonfs/cli.py` (pass `reporter=get_reporter()` is implicit via default; nothing needed if cores default to `get_reporter()`)
- Test: `tests/commands/test_*.py`

**Interfaces:**
- Consumes: `Reporter`, `get_reporter` (Task 2), `backend_passthrough_enabled` (Task 3).
- Produces: each core calls `reporter.phase/progress/item/warn/done`; behaviour and return values unchanged.

### 5a — pull

- [ ] **Step 1: Test (fake reporter records calls)**

```python
# tests/commands/test_pull.py (append)
class _RecordingReporter:
    def __init__(self): self.calls = []
    def phase(self, name, **f): self.calls.append(("phase", name))
    def progress(self, d, t, **f): self.calls.append(("progress", d, t))
    def item(self, a, p): self.calls.append(("item", p))
    def warn(self, m): self.calls.append(("warn", m))
    def done(self, m, **f): self.calls.append(("done", m))
    import contextlib
    @contextlib.contextmanager
    def timed(self, name):
        self.calls.append(("phase", name)); yield; self.calls.append(("done", name))


def test_pull_narrates_phases(tmp_path, make_fake_drive) -> None:
    from protonfs.config import init_config
    from protonfs.context import load_context
    from protonfs.commands.pull import pull
    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    ctx.index.set("a/f", _metadata_only_entry("/my-files/test/a/f"))
    ctx.drive = make_fake_drive()
    rep = _RecordingReporter()
    pull(ctx, None, resolve=None, dry_run=False, reporter=rep)
    kinds = [c[0] for c in rep.calls]
    assert "phase" in kinds and "done" in kinds
```

- [ ] **Step 2: Verify fail** — `python -m pytest tests/commands/test_pull.py::test_pull_narrates_phases -q` → FAIL (`unexpected keyword 'reporter'`).

- [ ] **Step 3: Implement in `pull.py`**

Add param + resolution and narration. Signature:
```python
def pull(ctx, subpath, resolve, dry_run, refresh=False, on_progress=None, reporter=None):
```
At the top of the body:
```python
    from protonfs.reporting import get_reporter
    reporter = reporter or get_reporter()
```
Wrap the scan/classify in `reporter.phase("scanning local", subpath=subpath or ".")`,
call `reporter.phase("downloading", files=len(to_pull))` before download, pass a progress
bridge into `_download_and_index` that calls `reporter.progress(done, total)` and
`reporter.item("↓", rel)` per file, and end with
`reporter.done("downloaded", transferred=total.transferred_items, failed=total.failed_items)`.
Replace the old `on_progress` plumbing: `_download_and_index` calls
`reporter.progress(...)`/`reporter.item(...)` directly instead of the `on_progress` callback.

- [ ] **Step 4: Pass** — `python -m pytest tests/commands/test_pull.py -q` → PASS.
- [ ] **Step 5: Commit** `feat(pull): narrate scan/download phases through the Reporter`.

### 5b — push (same pattern)

- [ ] Test `test_push_narrates_phases` (mirror 5a with `_RecordingReporter`, upload path).
- [ ] Implement: `push(..., reporter=None)`, phases `scanning`→`uploading`(progress+`item("↑", rel)`)→`verifying`→`done`. Remove `on_progress` param (Task 4 already stopped passing it) and drive progress via `reporter`.
- [ ] Pass + commit `feat(push): narrate scan/upload/verify phases through the Reporter`.

### 5c — refresh

- [ ] Test `test_refresh_narrates` (phase "walking remote", done with seeded/changed/deleted).
- [ ] Implement `refresh(..., reporter=None)`; call `reporter.phase("walking remote")`, `reporter.progress` per seeded directory (hook the existing `on_directory`/`_seed_directory` callback), `reporter.done("refreshed", seeded=..., changed=..., deleted=...)`.
- [ ] Pass + commit `feat(refresh): narrate remote walk through the Reporter`.

### 5d — offload

- [ ] Test `test_offload_narrates` (phases scan/verify/delete, `item("✗", rel)` per deleted, warn on skip).
- [ ] Implement `offload(..., reporter=None)`; replace the existing `logger.warning(...)` skip calls with `reporter.warn(...)` (keep the logger call too via reporter's mirror). `reporter.done("offloaded", reclaimed=..., skipped=...)`.
- [ ] Pass + commit `feat(offload): narrate verify/delete phases through the Reporter`.

### 5e — status / ls

- [ ] Test `test_status_narrates_scan` and `test_ls_remote_narrates_walk`.
- [ ] Implement `compute_status(..., reporter=None)` with `reporter.phase("scanning", subpath=...)`; `ls.collect_entries(..., reporter=None)` with `reporter.phase("walking remote")` when `remote=True`.
- [ ] Pass + commit `feat(status,ls): narrate scan/remote-walk through the Reporter`.

### 5f — rm / restore / trash

- [ ] Test each narrates per-path items.
- [ ] Implement: `rm(..., reporter=None)` → `reporter.item("trash"/"delete", path)`; `restore` → `item("restore", path)`; `trash empty` → `phase("emptying trash")`.
- [ ] Pass + commit `feat(rm,restore,trash): narrate per-path operations through the Reporter`.

### 5g — setup / upgrade / install-drive

- [ ] Test setup narrates each ensure_* step; upgrade narrates migration steps.
- [ ] Implement: route the existing `click.echo(...)` step messages in `run_setup`, `run_upgrade`, and `install_drive` through `reporter.phase(...)` (keep the stdout summary lines that scripts rely on; only the step narration moves to the reporter at level >= 1). For each, add `reporter: Reporter | None = None`.
- [ ] Pass + commit `feat(setup,upgrade,install): narrate steps through the Reporter`.

### 5h — drive backend passthrough (level 4)

- [ ] **Test:**
```python
# tests/test_drive.py (append)
def test_drive_logs_argv_at_debug(caplog) -> None:
    import logging
    from protonfs.drive import DriveClient
    d = DriveClient(binary="/nonexistent-proton-drive")
    with caplog.at_level(logging.DEBUG, logger="protonfs.drive"):
        try:
            d.version()
        except Exception:
            pass
    assert any("proton-drive" in r.getMessage() for r in caplog.records)
```
- [ ] **Implement:** in `drive.py` `_invoke` (or the subprocess wrapper), add `logger.debug("invoke %s", " ".join(args))` before running, and when `backend_passthrough_enabled()` and the process produced stderr, `logger.debug("proton-drive stderr: %s", result.stderr)`.
- [ ] Pass + commit `feat(drive): log proton-drive argv (DEBUG) + stderr passthrough at -vvvv`.

### 5i — rewire the retained #93 progress test

- [ ] Update `test_pull_reports_progress_per_batch` to assert via a `_RecordingReporter` that `progress` is called per batch (instead of the removed `on_progress` callback). Run full suite. Commit if changed.

Run after 5a–5i: `python -m pytest tests/ -q` → PASS.

---

## Task 6: Event-log ignore, deinit cleanup, migration

**Files:**
- Modify: `src/protonfs/commands/setup.py` (`_PROTONFS_GITIGNORE` ~L125)
- Modify: `src/protonfs/commands/deinit.py` (`LOCAL_ONLY_FILES` ~L35)
- Modify: `src/protonfs/migrations.py` (add a migration to `MIGRATIONS`)
- Test: `tests/commands/test_setup.py`, `tests/commands/test_deinit.py`, `tests/test_migrations.py`

**Interfaces:**
- Consumes: `EVENT_LOG_NAME` (Task 3) is `"events.log"`; backup is `"events.log.1"`.

- [ ] **Step 1: Tests**

```python
# tests/commands/test_setup.py (append)
def test_git_control_files_ignore_event_log(tmp_path) -> None:
    from protonfs.commands.setup import write_git_control_files
    write_git_control_files(tmp_path)
    ignore = (tmp_path / ".protonfs" / ".gitignore").read_text()
    assert "events.log" in ignore
    assert "events.log.1" in ignore
```
```python
# tests/commands/test_deinit.py (append)
def test_deinit_removes_event_log(tmp_path) -> None:
    from protonfs.config import init_config
    from protonfs.commands.deinit import run_deinit
    init_config(tmp_path, "/my-files/test")
    (tmp_path / ".protonfs" / "events.log").write_text("x")
    run_deinit(tmp_path, dry_run=False, yes=True)
    assert not (tmp_path / ".protonfs" / "events.log").exists()
```
```python
# tests/test_migrations.py (append)
def test_event_log_gitignore_migration(tmp_path) -> None:
    from protonfs.config import init_config
    from protonfs.migrations import pending_migrations, run_migrations
    init_config(tmp_path, "/my-files/test")
    # simulate an old repo whose .protonfs/.gitignore lacks the event-log lines
    (tmp_path / ".protonfs" / ".gitignore").write_text("index.json\n")
    assert any(m.id == "event-log-gitignore" for m in pending_migrations(tmp_path))
    run_migrations(tmp_path, dry_run=False)
    assert "events.log" in (tmp_path / ".protonfs" / ".gitignore").read_text()
```

- [ ] **Step 2: Verify fail** — run the three; expect FAIL.

- [ ] **Step 3: Implement gitignore template** (`setup.py`)

```python
_PROTONFS_GITIGNORE = (
    "# Managed by `protonfs setup` (#20, #21). Local-only, per-device state -- never commit\n"
    "# these; config.json, ignore, and include ARE committed (the shared sync contract).\n"
    "index.json\n"
    "refresh-state.json\n"
    "config.local.json\n"
    "events.log\n"
    "events.log.1\n"
)
```

- [ ] **Step 4: Implement deinit list** (`deinit.py`)

```python
LOCAL_ONLY_FILES = (
    LOCAL_CONFIG_FILE_NAME,
    INDEX_FILE_NAME,
    REFRESH_STATE_FILE,
    "events.log",
    "events.log.1",
)
```

- [ ] **Step 5: Implement migration** (`migrations.py`)

```python
def _event_log_gitignored(root: Path) -> bool:
    """is_applied: whether .protonfs/.gitignore already excludes the event log."""
    gitignore = root / ".protonfs" / ".gitignore"
    if not gitignore.exists():
        return False
    return "events.log" in gitignore.read_text()


def _event_log_gitignore_apply(root: Path) -> None:
    """apply: append the event-log gitignore lines (idempotent via write_git_control_files)."""
    write_git_control_files(root)
```
Append to `MIGRATIONS` (version 5):
```python
    Migration(
        id="event-log-gitignore",
        version=5,
        description="gitignore .protonfs/events.log(.1) (verbosity/event-log feature)",
        is_applied=_event_log_gitignored,
        apply=_event_log_gitignore_apply,
    ),
```
Note: `_event_log_gitignored` returns `False` when the file exists but lacks the line, so an old repo is flagged pending; a fresh setup (template already has the lines) reports applied.

- [ ] **Step 6: Run tests** — `python -m pytest tests/commands/test_setup.py tests/commands/test_deinit.py tests/test_migrations.py -q` → PASS.

- [ ] **Step 7: Commit** `feat(events): gitignore + deinit-clean + migrate the event-log file`.

---

## Task 7: Frozen surface + docs

**Files:**
- Modify: `docs/stability.rst` (global options + config keys), `docs/reference/index.rst` (new "Diagnostics & verbosity" section + `config` key list), `docs/getting-started/guide.rst` (task row)
- Test: docs build (`sphinx-build`), `interrogate`

- [ ] **Step 1: stability.rst** — add a "Global options" note under Command surface:

```rst
Global options (before the subcommand)
--------------------------------------
* ``-v`` / ``--verbose`` -- repeatable, ``-v``..``-vvvv``; raises console detail.
* ``--progress-inline`` / ``--progress-lines`` -- progress render style (default:
  ``defaults.progress_style``, else inline on a TTY).
* ``--event-log`` / ``--no-event-log`` -- write ``.protonfs/events.log`` (default:
  ``defaults.event_log``, else off).
```
And add to the config-keys table: `defaults.event_log` (bool, ``PROTONFS_EVENT_LOG``) and `defaults.progress_style` (inline|lines, ``PROTONFS_PROGRESS_STYLE``).

- [ ] **Step 2: reference/index.rst** — add a "Diagnostics & verbosity" section documenting the ladder (table from the spec), the two flags, the event log path/rotation/format, and the two config keys. Add the two keys to the `config` command's key list.

- [ ] **Step 3: guide.rst** — add a task row:
```rst
   * - See what protonfs is doing, or capture a debug log
     - :ref:`protonfs -v <cmd-...>` (more ``v`` = more detail); ``--event-log`` writes
       ``.protonfs/events.log``
```
(Use plain text if no clean anchor exists — global options have no per-command anchor.)

- [ ] **Step 4: Verify**

Run: `interrogate -c pyproject.toml src/ && python -m ruff check src/ tests/ && python -m pytest tests/ -q && sphinx-build -q -b html docs/ /tmp/db 2>&1 | grep -ciE "warning|error"`
Expected: interrogate 100%, ruff clean, tests pass, docs `0`.

- [ ] **Step 5: Commit** `docs: document -v verbosity ladder, progress style, and event log`.

---

## Self-review notes (author)

- Spec coverage: ladder (T2/T3/T4), progress style + invariant (T2), whole-package phases (T5a–g), config keys (T1), event-log file+rotation (T3), gitignore/deinit/migration (T6), backend passthrough (T5h), frozen surface + docs (T7). All covered.
- Interfaces consistent: `Reporter` method names (`phase/progress/item/warn/done/timed`) identical across T2 definition and T5 usage; `configure_logging` signature identical in T3 def and T4 call; config field names identical across T1 and T4.
- No placeholders: every code step shows real code; instrumentation tasks name exact phases and insertion points.
