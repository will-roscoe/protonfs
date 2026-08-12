# Plan: push accepts file pathspecs (and errors on nonexistent paths)

## Problem (evidence-backed)

`protonfs push mload002/mload002_00134` and glob-expanded forms like
`push mload002/mload002_013{4,5,6,7,8,9}` scan **zero** files and report
`transferred=0 skipped=0 failed=0`, exit 0. A typo'd path (`mload00140`) does the
same — silent success for a no-op.

Root cause: `localscan.scan()` (localscan.py:104) does `base.rglob("*")`.
`Path.rglob` on a **file** yields nothing; on a **nonexistent** path yields nothing.
Neither raises. So a file pathspec and a typo both degrade to an empty scan,
indistinguishable from "nothing to do".

The CLI already *advertises* file pathspecs: `_normalize_paths` docstring (cli.py:131)
says globs expand to many args and "every PATH-taking command accepts many". The
variadic plumbing exists; `scan()` never honoured the file case.

Verified rglob semantics:
```
rglob on a DIRECTORY : ['t/d/f1']
rglob on a FILE      : []
rglob on NONEXISTENT : []
```

## Definition of pathspec (confirmed with user)

A pathspec is: a directory path, a single file path, or a shell glob (which the
shell expands to a series of file/dir paths *before* protonfs sees them — protonfs
receives concrete paths via `nargs=-1`, never a `*`). So `scan()` must handle a
`subpath` that resolves to a file, a directory, or `.` (whole repo).

## Scope decision: two behaviours, two homes

1. **File-pathspec support → `scan()`** (shared by all 5 callers: push, pull,
   status, ls, refresh). Safe for every caller: naming a file to scan just that
   file is universally sensible.

2. **Error on nonexistent path → `push` ONLY.** Must NOT live in `scan()`.
   - `pull` scans the local path then downloads from Drive: pulling a remote-only
     dir *requires* the local path be absent (that's the whole point of pull).
   - `status`/`ls` on offloaded content also legitimately hit absent local paths
     (polaris is currently 21,196 remote-only entries).
   - Only in `push` is a missing local path unambiguously a user error (you cannot
     upload what does not exist locally).

## Changes

### A. `localscan.scan()` — handle a file `subpath`
Currently:
```python
base = root / subpath if subpath != Path(".") else root
for file_path in sorted(base.rglob("*")):
    if not file_path.is_file():
        continue
    ...
```
New: if `base` is a file, iterate `[base]` instead of `base.rglob("*")`. Everything
downstream (ignore match, .protonfs skip, hash, LFS check) stays identical. A file
that is itself ignored is still excluded (correct: ignore rules are the sync
contract). Directory and `.` behaviour unchanged.

Implementation sketch:
```python
base = root / subpath if subpath != Path(".") else root
candidates = [base] if base.is_file() else sorted(base.rglob("*"))
for file_path in candidates:
    if not file_path.is_file():
        continue
    ...  # unchanged
```
`base.is_file()` is False for a nonexistent path, so scan() still returns `{}` for
absent paths — that's the behaviour pull/status/ls rely on. No signature change.

> **within_subpath invariant (reviewer 1):** pull/status/ls/refresh re-filter
> scan() output through `diff.within_subpath` (diff.py:73-83), whose `rel_path ==
> subpath` branch is what keeps a file-equal entry from being dropped. This is a
> load-bearing invariant for the file-subpath case in those 4 callers. Add a unit
> test asserting `within_subpath(file, file) is True`.

### B. `push` — reject a nonexistent local pathspec (REVISED)

Placement: in **`cli.push()`** (click layer), BEFORE `repo_lock` is acquired.
cli.push currently takes the lock (cli.py:425) *then* loops; restructure so
validation runs before the `with repo_lock`. This makes the abort cost nothing —
no lock, no 9-min hash.

Behaviour (revised per reviewers 1 & 2):
- Collect **all** non-None subpaths where `(ctx.root / sub)` does not exist, then
  raise **once** listing every bad path — not fail-on-first (reviewer 2).
- Raise `click.UsageError` → exit 2. Document the meaning in the push docstring,
  since `status` already uses exit 2 for "conflict present" (reviewer 2).
- Message: qualify **local**, quote with `!r`, actionable hint. e.g.
  `f"no such local path(s): {', '.join(map(repr, missing))} "
   f"(push uploads local files — check the path or your shell glob)"`.
- **Offloaded-dir case (reviewer 1):** the offload workflow keeps each sim dir's
  config files on disk ("config on file, data on Drive" floor), so an offloaded
  directory still `exists()` — only a *fully removed* dir or a literal typo trips
  this. That is the intended target. Make it an explicit, tested decision: test a
  genuinely-nonexistent dir → exit 2; an existing dir with no candidates → exit 0.
In `commands/push.py`, before/at the scan, when `subpath` is not None and the
resolved local path does not exist, raise a clean error. Preferred surface: a
`DriveError`/`ClickException`-friendly message via the existing error boundary, OR
raise `FileNotFoundError` -> map to click error. Simplest: raise a
`click.ClickException`-compatible error from the CLI layer, but push() is called
below the CLI. Decision: raise a `ValueError`/dedicated exception in push() and let
`_drive_error_boundary` OR a small catch in cli.push map it. Check what boundary
catches — it catches DriveError/RepoLockError/etc. Cleanest: validate in
`cli.push()` loop (has click available) OR add a `PushPathError(DriveError)`.

Chosen approach (least surprising, testable at the command layer): in `push()`,
after computing `scan_root`, check `(ctx.root / scan_root).exists()`; if not, append
a failure of a new `kind="missing-path"` to the TransferResult AND/OR raise. Since
push returns a TransferResult and the CLI aggregates, the honest signal is a
**failure**, not a silent skip. But a nonexistent path is a *usage* error, not a
per-file transfer failure — better to fail fast and loud.

FINAL decision: validate in `cli.push()` (the click layer) — iterate the
`_normalize_paths` result, and for any non-None subpath where `(ctx.root/sub)` does
not exist, `raise click.UsageError(f"path does not exist: {sub}")` before acquiring
the lock. Rationale: keeps `scan()` pure, keeps push() core logic clean, uses click's
native usage-error surface (exit 2), and fails before any Drive work. Downside: a
multi-path push with one bad path fails the whole batch — acceptable and arguably
correct (catches typos before a 9-minute hash).

Reconsider: should one bad path abort all? For a glob expansion, the shell only
produces paths that exist, so a nonexistent path means the user typed it literally
and wrongly — aborting is right. Keep fail-fast.

### C. Empty-push narration (REVISED — must be level-0 visible)
CRITICAL (reviewer 2): `reporter.done()` renders only at level >= 1
(reporting.py:114), so it would be INVISIBLE at default verbosity — the exact level
the user complained about. Do NOT use the Reporter.

Instead, mirror `pull`'s level-0 pattern (cli.py:485-487): in **`cli.push()`**,
after the aggregation loop, if the whole result is all-zero
(`transferred + skipped + failed == 0`), `click.echo("nothing to push")`. Shows at
level 0, consistent with pull, no per-subpath duplication. (The existing
`transferred=0 skipped=0 failed=0` summary at cli.py:428-431 still prints too; this
is additive plain-language.)

## Tests (TDD — write first, watch fail, then implement)

`tests/test_localscan.py`:
- `scan()` with a file subpath returns exactly that one entry.
- `scan()` with a file subpath that is ignored returns `{}`.
- `scan()` with a nonexistent subpath returns `{}` (regression guard for pull/status).
- (existing dir/nested/low_io tests must still pass unchanged.)

`tests/commands/test_push.py` (or test_cli.py push section):
- push with a file pathspec uploads that one file (mock drive).
- push with several file pathspecs (simulating brace/glob expansion) uploads each.
- push with a nonexistent pathspec raises UsageError / exits 2, does NO drive calls,
  acquires NO lock.
- push with a valid dir that yields no candidates prints "nothing to push" and
  exits 0.

`tests/test_cli.py`:
- confirm exit code 2 (usage) for the missing-path case, distinct from the exit 1
  used for transfer failures.

### Consolidated test list (reviewers 1 & 3)
scan-level (test_localscan.py):
- file subpath → exactly one entry (catches the original bug)
- ignored file subpath → {}
- nonexistent subpath → {} (regression guard for pull/status/ls)
- file subpath inside `.protonfs` → {} (NEW branch bypasses the rglob walk — must
  still hit the .protonfs skip)
- file subpath that is an LFS pointer stub → is_lfs_pointer=True

diff-level:
- `within_subpath(file, file) is True`

cli/push-level (test_push.py / test_cli.py):
- push one file pathspec → uploads that one file
- push mixed file + dir in one invocation → both handled
- push several file pathspecs (brace/glob expansion) → each uploaded
- push nonexistent path → exit 2, NO drive calls, NO lock. Assert no-lock by
  monkeypatching `protonfs.cli.repo_lock` with a spy CM (no existing pattern; the
  lock file exists even on normal acquire so file-existence is not a signal —
  reviewer 3). This test also guards the pre-lock ordering.
- push existing-but-ignored file → exit 0 "nothing to push", NOT exit 2 (the B/C
  seam — an ignored file exists on disk but scans to {})
- push valid dir with no candidates → exit 0 "nothing to push"

## Non-goals (separate PR)
- Streaming hash-then-push / hash-cache persistence / KeyboardInterrupt handler.
  That is the performance rework; this PR is correctness-only and must be small.

## Versioning
Public behaviour change to `push` (new accepted input shape + new error). Add
`.. versionchanged::` to push docstring and scan docstring per repo rule. Bump is
automatic via conventional commit on merge — use `fix(push):` (a bug fix; file
pathspecs were advertised but broken) which yields a patch bump.

## Verification before PR
- `pytest tests/test_localscan.py tests/commands/ tests/test_cli.py -q` green.
- Full `pytest -q` green (or targeted + rely on CI matrix for the rest).
- Ruff/lint clean.
- Manual: `protonfs push <onefile>` and `push <nonexistent>` against a temp repo.
