# Spec B — Versioning-directive convention

Date: 2026-07-18
Status: implementing unattended (user delegated: "implement spec b to your best ability, unattended")
Scope: `src/` docstrings, `docs/`, `CONTRIBUTING.md`. No behavior changes.

## Policy (version assignment)

Release history is compressed: 0.3.0→1.0.0 all shipped 2026-07-16 (rapid pre-stable
churn); 1.0.0 is the first **stable** release; only 1.1.0–1.4.0 follow. Precise per-symbol
dating within the 0.x churn is false precision, so:

- **Baseline = v1.0.0.** Public API present at 1.0.0 → `.. versionadded:: 1.0.0`.
- **Post-1.0 additions** get their real version from the CHANGELOG/git.
- `.. versionchanged::` where an existing API changed post-1.0.
- No `deprecated` — nothing is deprecated in the codebase.

Accurate module introduction (git `--diff-filter=A`, first containing tag):
- **1.0.0 baseline** (present at first stable): batching, cli, config, context, diff, drive,
  ignore, index, install, lfs, localscan, locking, migrations, refreshstate, secretservice.
- **1.3.0**: logs, reporting.
- **1.4.0**: argv.

Post-1.0 feature map (CHANGELOG):
- 1.1.0: multiple pathspecs; interactive push/pull batch progress; `ls --dirs`/`--state`,
  `--format` on ls/status.
- 1.2.0: `ls --visual` treemap/waffle.
- 1.3.0: layered `-v` verbosity, progress styles, rotating event log (logs.py, reporting.py,
  `Reporter`, config keys `event_log`/`progress_style`).
- 1.4.0: position-independent flags (argv.py, `PositionalFlagGroup`), readable transfer logs.

## Application surface

1. **Module docstrings (18)** — `.. versionadded::` per the map above.
2. **New public symbols in older modules** — `cli.PositionalFlagGroup` (1.4.0);
   `reporting.Reporter` (1.3.0); `argv.reorder_argv` (1.4.0). `config.Defaults` gets
   `.. versionchanged:: 1.3.0` (added `event_log`, `progress_style`).
3. **`docs/reference/config.rst`** — `:ver-added:`… no; use `.. versionadded::` inside each
   `confval`/`envvar` (1.0.0 baseline; 1.3.0 for event_log/progress_style + their env vars).
4. **`docs/reference/index.rst` narrative** — `.. versionadded::`/`.. versionchanged::` in the
   diagnostics section (1.3.0) and per-command sections (ls 1.1.0/1.2.0, push/pull/status 1.1.0,
   global position-independent flags 1.4.0). Not in Click help strings (they show in --help).
5. **Forward rule** — `CONTRIBUTING.md` section + agent memory: new public API always gets
   `versionadded`; behavior changes get `versionchanged`.

## Verification

- Strict docs build `sphinx-build -W --keep-going` stays clean (versionadded/changed are
  standard directives, no ref risk).
- Full test suite green (docstring-only src edits).
- Spot-check rendered admonitions appear in `commands`/`config`/api pages.

## Out of scope

Per-symbol `versionadded:: 1.0.0` on every baseline function (noise; module-level covers it).
Automated "new API must have versionadded" lint (future custom check).
