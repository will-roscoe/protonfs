# Design: `protonfs schedule` — managed cron jobs for automated push/pull

Encodes the cron mechanism hand-built this session (`pfpush_retry.sh`) as a first-class
command, so users don't re-derive the sharp edges. Modelled on `completions` (flag-based
command delegating to a `commands/schedule.py` module that writes user-level state).

## Why this is needed (learned the hard way this session)
A naive `crontab -e` running `protonfs push` breaks in ways that took a full session to
diagnose:
- **cron's PATH is stripped** → `proton-drive` (in `~/.local/bin`) not found. Must set
  `PROTONFS_DRIVE_BIN` to an absolute path.
- **no overlap guard** → a slow run collides with the next tick. Need `flock`.
- **default `LIST_TIMEOUT` (45s) is below the real ~34s+cold list cost** → verify
  times out into a retry storm that looks like a hang. Need a generous list timeout.
- **no logging** → invisible failures (the runs failed silently for a day).
- push must be **idempotent + resumable** (it is: index + low_io + v1.7.0 adopt).
`schedule` bakes all of these in as defaults so the user can't trip them.

## Command surface (mirrors `completions`; matches the requested flag style)

```
protonfs schedule                         # == --list (safe default: never mutate on bare call)
protonfs schedule --list [--json]         # list this machine's protonfs jobs: id, schedule, target, last run, log
protonfs schedule --add [options]         # install a job; prints the assigned id
protonfs schedule --uninstall <id> | -U <id>
protonfs schedule --uninstall --all       # remove every protonfs job
protonfs schedule --run <id>              # run a job now (foreground), for testing
```

Install (`--add`) options:
- `--every <spec>`  friendly cadence: `hourly` | `daily` | `6h` | `30m` | `weekly`.
- `--cron "<expr>"` raw 5-field crontab expression (mutually exclusive with --every).
- `--at <hours>`    restrict to off-peak hours, e.g. `--at 1,3,5` (expands into the cron expr).
- `--command push|pull|sync`  what to run (default: push). `sync` = pull then push.
- `--path <subpath>`  scope to a subtree (default: whole repo).
- `--resolve <strategy>`  passed through to push/pull.
- `--repo <dir>`  the protonfs repo to operate on (default: cwd; stored absolute).
- Tuning (sensible defaults from this session, overridable):
  `--list-timeout 120` `--transfer-timeout 1200` `--batch-size 50`.
- `--label <text>`  human label shown in --list.

Bare `schedule` prints the list (never installs implicitly — installing is an explicit
`--add`, to avoid a footgun). `--add` + `--uninstall` + `--list` are mutually exclusive.

## Job identity
Short random hex id (e.g. `a1d3ae`, 6 chars from `secrets.token_hex(3)`) — stable across
list reorders, collision-resistant. `--list` also shows a 1-based index as a convenience,
and `--uninstall` accepts either the id or the index. (Numeric-only ids would renumber on
removal and break scripts; hex is canonical.)

## Storage & crontab mechanics
- **Job manifest:** `.protonfs/schedule.local.json` (gitignored, per-device — the schedule
  lives on THIS machine, like `config.local.json`/`device_id`). Records per job: id, cron
  expr, command, path, resolve, repo (absolute), tuning, label, created-at, wrapper path,
  log path. Source of truth for `--list`.
- **Wrapper script per job:** `.protonfs/schedule/<id>.sh` — the generated equivalent of
  pfpush_retry.sh: exports PATH + `PROTONFS_DRIVE_BIN` (resolved via the same
  `secretservice`/`which` logic doctor uses) + the tuning env, `cd`s to the repo, and runs
  the command under `flock -n` with timestamped logging to `.protonfs/schedule/<id>.log`.
  Regenerated from the manifest, so `schedule` is the single source of truth.
- **crontab line:** one line per job, tagged so we own only our lines:
  `\n<cronexpr> "<abs wrapper path>"  # protonfs-schedule:<id>` .
  Install = read `crontab -l`, drop any line with our marker for this id, append the new
  one, write back via `crontab -`. Uninstall = read, drop matching marker line(s), write
  back, delete the wrapper+manifest entry (keep the log unless `--purge`). Never touch
  non-protonfs crontab lines. Idempotent.

## Cross-platform
- v1: **cron** via `crontab` (Linux + macOS both have it). Detect `crontab` on PATH;
  if absent, error with guidance (matches how `completions`/`doctor` handle missing tools).
- Follow-ups (note, don't build): **launchd** on macOS (the native, non-deprecated path)
  and **systemd --user timers** on Linux. Abstract the backend behind a small interface
  (`install(job)`, `remove(id)`, `list()`) so cron is just the first implementation.

## Safety / edges
- Refuse to install if the cwd/`--repo` is not a protonfs repo (no `.protonfs/config.json`).
- `--run <id>` executes the wrapper in the foreground for a quick smoke test (surfacing the
  PATH/auth issues immediately instead of silently overnight).
- `doctor` gains a check: for each scheduled job, is its wrapper present, is the crontab
  line present, did the last run exit non-zero (tail the log) — surfaced as `[warn]`.
- Store the resolved absolute `PROTONFS_DRIVE_BIN` at install time AND re-resolve in the
  wrapper (belt and suspenders against `~/.local/bin` moving).

## Testing
- schedule.py unit tests with an injected `crontab` runner (like secretservice's `runner=`):
  add appends exactly one tagged line + writes manifest + wrapper; uninstall removes only
  the tagged line; list parses the manifest; ids are stable; --uninstall by index maps
  correctly; bare call never mutates; non-repo refuses; --every/--cron mutual exclusion.
- No real crontab touched in tests (inject the runner).

## Versioning / scope
New command → `feat` → minor bump. Ships as its own PR. `.. versionadded:: <next>` on the
command + module. Depends on nothing else; independent of the durability rework.

## Open decisions (confirm before building)
1. Command surface: flag-style (`schedule --add/--list/--uninstall`, matches the request
   and `completions`) vs. subcommand group (`schedule add/list/remove`, matches `trash`/
   `config`). Recommend flag-style per the request.
2. Job id: short hex (`a1d3ae`) canonical + numeric index convenience — recommended — vs.
   pure numeric.
3. Scope now: cron-only (recommended) vs. also launchd/systemd in v1.
4. `sync` command (pull-then-push) in v1, or push/pull only.
