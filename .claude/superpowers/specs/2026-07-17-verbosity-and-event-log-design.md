# Design: Layered verbosity + event log

**Date:** 2026-07-17
**Status:** Approved (brainstorm), pending implementation plan
**Issue:** none filed yet — user request ("add better debugging")

## Goal

Add readable, layered diagnostics across the whole `protonfs` package:

1. A counted global verbosity flag `-v`/`-vv`/`-vvv`/`-vvvv` controlling console output.
2. A config-toggled **event log** file (`.protonfs/events.log`) that always records
   full detail when enabled — auto git-ignored and protonfs-sync-ignored, kept inside
   `.protonfs/`.
3. A flag to switch progress-poll rendering between **in-place update** (one live line)
   and **new lines** (full scroll history).
4. Useful, non-spammy logging throughout the package — curated at low levels, deep at
   high levels.

## Architecture (Approach A: Reporter + stdlib logging, both feeding the event log)

Two layers, one event log:

- **Reporter** (`protonfs/reporting.py`) — the curated-UX layer. Commands call it at
  phase boundaries; it decides *what* to show and *how often* based on the level and
  renders human lines to **stderr**. This is what keeps `-v`/`-vv` readable: low levels
  only ever show deliberate reporter calls, never incidental logger spam.
- **stdlib logging** (`protonfs/logs.py` config) — the diagnostic layer. The existing
  `logging.getLogger(__name__)` calls (in `drive`, `push`, `offload`, `locking`, and
  new ones added throughout) surface on console only at `-vvv` (INFO) / `-vvvv` (DEBUG
  + backend passthrough).
- **Event-log file** — when enabled, both layers always write to it at full DEBUG detail
  regardless of console `-v`, so a quiet run that hit a bug still yields a complete log.

All human output is stderr; each command's **result summary line** (e.g.
`transferred=200 skipped=0 failed=0`) stays on **stdout** for scripts. This supersedes
and unifies the TTY progress line shipped in #93 (`cli._progress_printer` is removed;
its callers move to the Reporter).

## The verbosity ladder (contract)

| Level | Console shows | Progress cadence |
|-------|---------------|------------------|
| (none) | Silent except warnings/errors + existing stdout summary. Unchanged for scripts. | — |
| `-v` | Curated phases ("Indexing remote…", "Downloading 200 files…"), throttled progress ("Downloading 120/200 (60%)"), phase durations ("Downloaded 200 files in 1m 14s"), warnings. | every 30s or on completion |
| `-vv` | + per-phase sub-steps with timing ("Indexed 200 files in 03pol021/, 3.2s") and per-item paths ("↓ 03pol021/dump_0007"). | every 5s + per item |
| `-vvv` | + INFO diagnostics interleaved (classify results, index decisions, lock acquire/release, config resolution, remote-walk frontier). | every 1s |
| `-vvvv` | Deepest: DEBUG from every protonfs module + raw proton-drive CLI argv & subprocess stderr + third-party. "As full as possible without being useless." | continuous |

Level→layer mapping:
- Reporter renders at level ≥1 (phases/progress/done/warn), ≥2 also `item()` + faster
  throttle, ≥3 faster, ≥4 continuous.
- stdlib console level: WARNING for 0–2, INFO at 3, DEBUG (+ backend/third-party) at 4.
- Throttle interval by level: {1: 30s, 2: 5s, 3: 1s, 4: 0 (every update)}.
- `-v` count capped at 4 (extra `v`s clamp).

## Progress render style

Global paired flag `--progress-inline` / `--progress-lines`, affecting **only** the
throttled progress-poll lines:

- `--progress-inline` (default on a TTY): consecutive progress polls rewrite the same
  line via `\r` — one live "Downloading 120/200 (60%)" line.
- `--progress-lines`: every progress poll is its own new line (full scroll record; the
  right choice when piping `-v` output to a file).

**Invariant — progress never clobbers other messages:** the Reporter tracks whether a
progress line is "open". Any non-progress output (phase change, per-item path, warning,
diagnostic) first **closes** the open progress line with a newline (preserving it),
prints its message on a fresh line, and the next poll opens a new progress line. So
inline mode shows one live line between interruptions with everything else scrolling
above it; only a progress line is ever overwritten, and only by a newer progress reading.

Guardrails:
- **Non-TTY forces `--progress-lines`** — `\r` is meaningless in a pipe/file/CI; inline
  silently degrades to lines.
- Persistable as a config default (`defaults.progress_style`), flag overrides config.

## Whole-package phase coverage

Every command routes user-facing narration through the one Reporter:

| Command | Phases (reporter calls) |
|---|---|
| pull | scan → (refresh: walk remote) → classify → download (progress+items) → resolve-fetch → done |
| push | scan → classify → ensure-dirs → upload (progress+items) → verify → done |
| refresh | walk remote (progress per dir seeded, frontier) → done(seeded/changed/deleted) |
| offload | scan candidates → verify (progress) → delete (items) → done(reclaimed) |
| status / ls | scan (+ remote walk if `--remote`) → render |
| setup / upgrade / install-drive | each `ensure_*` / download / verify / migrate step as a phase |
| rm / restore / trash | per-path trash/delete/restore items |
| doctor | each check as a step |

`drive.py` gets the `-vvvv` backend passthrough (proton-drive argv + subprocess stderr).
Concretely: `drive.py` logs each invocation's argv at DEBUG and, when the passthrough
flag is set, the subprocess stderr at DEBUG too. Console shows these only at `-vvvv`
(DEBUG); the event-log file captures them whenever it is enabled **and** the passthrough
flag is set — i.e. backend detail lands in the log at `-vvvv`, not at lower levels, to
avoid bloating the log with subprocess noise on every quiet run. Command result-summary
lines stay on stdout unchanged.

## Components & interfaces

### `protonfs/reporting.py`
- `Reporter` methods: `phase(name, **fields)`, `progress(done, total, **fields)`
  (throttled), `item(action, path)` (rendered at ≥2), `warn(msg)`,
  `done(summary, **fields)`, `timed(phase)` context manager (durations).
- State: level (0–4), throttle interval, progress style, stderr stream, open-progress
  flag.
- Every call also emits a structured record to a `protonfs.events` logger, so the event
  log captures narration even at level 0.
- Level 0 is a real Reporter: silent on stderr, still warns + feeds event log.
- `get_reporter()` returns the process reporter (a null/level-0 reporter until
  configured). `set_reporter(...)` used by the CLI group callback.
- **Performance:** cheap early level checks so per-item `item()` in hot loops costs
  nothing when quiet.

### `protonfs/logs.py`
- `configure_logging(verbosity, progress_style, event_log, root) -> Reporter`:
  - stderr console handler at mapped level (WARNING ≤ `-vv`, INFO `-vvv`, DEBUG `-vvvv`).
  - When event-log on: `RotatingFileHandler(root/".protonfs"/"events.log",
    maxBytes≈5*1024*1024, backupCount=1)` at DEBUG on the `protonfs` logger tree, with
    the aligned-text formatter (`TS LEVEL cmd  msg key=val`).
  - `-vvvv` ungags third-party loggers + flips the backend-passthrough flag `drive.py`
    reads.
  - Builds the Reporter and calls `set_reporter`.

### Aligned-text formatter
`2026-07-17T14:03:11Z INFO  pull      start subpath=03pol021 resolve=none` — ISO-8601 UTC
timestamp, fixed-width level, fixed-width command/component, message, `key=value` fields.

### Wiring
- `main` Click group gains `-v/--verbose` (count), `--progress-inline/--progress-lines`,
  `--event-log/--no-event-log` (flags override config; `None` → config value). A group
  callback calls `configure_logging(...)` before any command runs.
- Command **core** functions take `reporter: Reporter | None = None` and resolve
  `reporter or get_reporter()` — library-callable + unit-testable (pass a fake), CLI
  wires it automatically. Same pattern as #93's `on_progress`, generalized.

## Config

Reuse the `Defaults` bag and existing get/set/env/layering machinery:
- `defaults.event_log` — bool, default `false`, env `PROTONFS_EVENT_LOG`.
- `defaults.progress_style` — `inline`|`lines`, default `inline`, env
  `PROTONFS_PROGRESS_STYLE`.
- Added to `Defaults` dataclass, `KNOWN_KEYS`, `_BOOL_KEYS` (event_log), env map,
  `to_dict`/`from_dict`. `progress_style` validated against {inline, lines}.

Verbosity itself is CLI-only (per-invocation), not config.

## Event-log file: ignore + teardown

- git-ignore: add `events.log` + `events.log.1` to `_PROTONFS_GITIGNORE` template in
  `setup.py`; `_ensure_lines` appends to existing repos on next `setup`.
- Migration (#67 registry): a small migration appends the two gitignore lines so existing
  repos pick it up via `upgrade` without a full re-setup.
- protonfs-sync-ignore: automatic — `localscan.scan` already excludes all of
  `.protonfs/` (verified). No code needed.
- deinit: add `events.log`/`events.log.1` to `LOCAL_ONLY_FILES` so teardown removes them.

## Frozen surface

- Three global options (`-v/--verbose`, `--progress-inline/--progress-lines`,
  `--event-log/--no-event-log`) + two config keys documented in `stability.rst`.
- `test_cli_surface.py` extended to freeze the group-level options (currently only
  per-command options are frozen).

## Testing

- **Reporter**: level gating (0/1/2/3/4), throttle (suppressed within interval, always
  flushed on `done`), inline vs lines (`\r` vs `\n`), close-open-progress-line-before-
  other-output invariant, non-TTY forces lines, duration formatting, level-0 silence but
  event-log capture.
- **logs.py**: handlers at correct levels; rotating file handler only when enabled;
  formatter shape; `-vvvv` ungags third-party + sets backend flag.
- **config**: new keys via get/set/env/layering; bool + choice validation.
- **CLI**: `-v…-vvvv` count parsing; group callback configures; a command emits expected
  phases per level (capture stderr); event-log file written iff enabled and absent when
  off; stdout summary unchanged; non-TTY ⇒ lines.
- **gitignore/deinit/migration**: template + migration add the lines; deinit removes them.
- **Regression**: existing exact-stdout tests still pass (narration is stderr-only);
  #93's `_progress_printer` tests migrate to the Reporter.

## Docs

- `docs/reference/index.rst`: a "Global options" / diagnostics section documenting `-v`
  levels, the two flags, the event log location/rotation, and the two config keys; plus
  the `config` section's key list.
- `docs/stability.rst`: global options + config keys added to the frozen tables.
- Task guide: a row for "see what protonfs is doing / capture a debug log".

## Out of scope (YAGNI)

- JSONL event-log format (aligned text only; revisit if tooling demand appears).
- Per-command verbosity flags (group-level `-v` only).
- Log levels beyond 4 / configurable rotation size/count (fixed ~5 MB, 1 backup).
- Live-integration tests for the reporter (deterministic unit tests suffice).
