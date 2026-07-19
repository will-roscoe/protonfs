# Changelog

All notable changes to protonfs are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versioning
follows [Semantic Conventions](https://semver.org/) via the automated
`auto-release.yml` pipeline (issue #31): every merge to `main` is classified
from its Conventional Commit messages and, if warranted, tagged automatically.

## [Unreleased]

## [1.5.1] - 2026-07-19

### Bug fixes

- **status**: inline header logo as data URI so it renders under GitHub CSP

### Documentation

- unify command reference + auto-link command mentions (#112)

### CI

- generate the status graphic from workflow results (#113)

## [1.5.0] - 2026-07-18

### Features

- protonfs completions command (shell completion for bash/zsh/fish) (#111)

### Documentation

- **config**: define operational/tuning env vars as envvar targets (#110)

## [1.4.1] - 2026-07-18

### Bug fixes

- **docs**: pin Sphinx to 8.x so the sphinx-click build passes in CI (#109)

### Documentation

- **logo**: normalize to 100x100 canvas and add tile/cutout/dark variants
- CLI/API documentation semantic layer (sphinx-click, confval/envvar, cross-refs, strict build) (#107)
- version directives across the codebase + forward rule (#108)

## [1.4.0] - 2026-07-17

### Features

- position-independent flags, readable large-transfer logs, project logo (#106)

## [1.3.0] - 2026-07-17

### Features

- layered -v verbosity, progress styles, and a rotating event log (#105)

### Documentation

- restore 100% docstrings, correct --visual sizing wording, add task-guide rows (#104)

## [1.2.0] - 2026-07-17

### Features

- **ls**: --visual treemap|waffle storage charts (#94) (#103)

### Documentation

- link task-guide "Run" commands to their reference sections + hover previews (#95)

### Tests

- **coverage**: cover doctor/setup/cli orchestration paths (#88) (#91)

## [1.1.0] - 2026-07-16

### Features

- **cli**: accept multiple pathspecs on PATH-taking commands (#92)
- **transfer**: show batch progress on interactive push/pull (#93)
- **ls**: --dirs aggregation with sizes, --state filter, --format on ls/status (#97, #94)

### Tests

- **live**: add end-to-end command workflow suite (dev-machine only)
- **pull**: single-file pathspec downloads only that file (#96 follow-up)

## [1.0.3] - 2026-07-16

### Bug fixes

- **pull**: scope pull/status to the given subpath (#96) (#98)

### Documentation

- docstring coverage 48→98%, task guide, README↔docs sync, furo fix (#89)
- add GitHub community-standards files (#90)

## [1.0.2] - 2026-07-16

### Bug fixes

- **ci**: remove workflow code injection; least-privilege GITHUB_TOKEN defaults (#87)

## [1.0.1] - 2026-07-16

### Bug fixes

- **release**: require +:<spec> directives to stand alone on their own line (#86)

## [1.0.0] - 2026-07-16

protonfs 1.0.0 — the initial stable release. The command-line surface frozen in
[`docs/stability.rst`](docs/stability.rst) — every command, option name, exit
code, config location, and environment variable documented there — is now a
stable contract for the whole `1.x` series. This entry also collects the
v0.18.0–v0.25.0 changes (released earlier the same day) that led up to it.

### Added

- `protonfs upgrade [--check] [--drive-only|--repo-only]`: upgrades the
  proton-drive binary to this release's highest supported version
  (SHA-512-verified before an atomic swap, session checked afterwards) and runs
  pending repo-state migrations. When upstream ships something newer than this
  release supports, `upgrade` says so and installs nothing — upgrading protonfs
  itself is the path to a newer proton-drive. `--check` previews everything and
  exits `0`/`1` for scripts (#66; v0.23.0).
- Explicit proton-drive support matrix per protonfs release:
  `SUPPORTED_DRIVE_VERSIONS` / `highest_supported()` / `is_supported()`, plus
  `DriveClient.drive_version()`, documented as a table in the stability page
  (#65; v0.18.0).
- Versioned repo-state migrations since 0.2.0, runnable via `protonfs upgrade`:
  index schema re-save, `device_id` relocation to `config.local.json`, and
  control-file backfill, each idempotent and probing real on-disk state. A
  `layout_version` marker in `config.local.json` records the migrated level
  (#67; v0.19.0).
- `protonfs trash list` / `protonfs trash empty`: inspect Drive's trash
  (including same-named duplicate counts — the ambiguity that can block
  `restore`) and empty it behind an explicit typed confirmation (#70; v0.22.0).
- `protonfs deinit [--dry-run] [--yes]`: clean teardown of a protonfs root —
  removes `.protonfs/` bookkeeping only, never synced payload files, and
  reports (never runs) the follow-up git steps (#71; v0.21.0).
- Throttle backoff for upload/download at parity with `list`: transient Proton
  API throttling mid-push/pull is retried with bounded exponential backoff
  instead of failing the run; new `PROTONFS_TRANSFER_TIMEOUT/RETRIES/BACKOFF/
  BACKOFF_CAP` env knobs (#69; v0.20.0).
- `protonfs doctor` now includes a pre-upgrade advisor: installed proton-drive
  version vs the support matrix, an upstream-ahead advisory, index-schema
  currency, pending migrations, and config-layering sanity, as non-fatal
  `[warn]` findings (#73; v0.24.0).
- macOS CI: `macos-latest` (arm64) and `macos-15-intel` runners now execute the
  full suite, backing the pinned darwin binaries with real coverage, with
  per-arch README badges (#72).
- Docs: `docs/upgrading.rst` — the full upgrade story (pip, binary, migrations,
  session caveat) (#68).
- `+:<spec>` release-override directives, the SemVer 2.0.0 pre-release ladder
  (`alpha` → `beta` → `rc`), and auto-generated, type-grouped release notes
  (#85; see below).

### Upgrade notes

- **Coming from any 0.x**: run `protonfs upgrade --check` inside each protonfs
  repo to preview pending repo-state migrations, then `protonfs upgrade` to
  apply them. Old repos keep working unmigrated — every consumer still migrates
  what it reads on the fly — but migrating makes the on-disk state current in
  one step. See the [Upgrading](https://will-roscoe.github.io/protonfs/upgrading.html)
  docs page.
- **Versioning from here**: 1.0.0 makes the stability contract binding — a
  breaking change to anything in `docs/stability.rst` now bumps the major
  version (the pre-1.0 demotion of breaking→minor no longer applies).

### Features

- **release**: +:<spec> version directives, prerelease ladder, generated release notes (#85)

### Documentation

- upgrade guide for protonfs upgrade + support matrix (#83)

### Tests

- **live**: poll for post-restore visibility instead of asserting at t=0 (#84)

## [0.17.0] - 2026-07-16

### Added

- `protonfs config` command plus layered config resolution: env vars >
  `.protonfs/config.local.json` (per-device, gitignored) >
  `.protonfs/config.json` (per-repo, committed) >
  `~/.config/protonfs/config.json` (global user defaults) > built-in defaults
  (#61).

### Upgrade notes

- **Config layering / `device_id` migration**: previously `device_id` lived in
  the committed `.protonfs/config.json`. It now belongs in the per-device,
  gitignored `.protonfs/config.local.json`. Existing repos keep working
  unchanged — resolution still reads `device_id` from either file (local
  wins) — and running `protonfs setup` again on an existing repo opportunistically
  migrates an embedded `device_id` from `config.json` into `config.local.json`
  for you, with no change to the resolved config. `.protonfs/.gitignore` is
  updated at the same time to ignore `config.local.json`.

## [0.16.0] - 2026-07-16

### Added

- `.protonfs/include`: a first-class allowlist file for cases the `ignore`
  pathspec can't express cleanly, e.g. re-including a few files under an
  otherwise-excluded directory (#59).

## [0.15.1] - 2026-07-16

### Fixed

- `protonfs auth status` is now implemented locally instead of forwarding to
  `proton-drive`, which had inconsistent behavior across pinned versions
  (#60).

## [0.15.0] - 2026-07-16

### Added

- CI: proton-drive pin badge and per-architecture Linux build badges (#58).

## [0.14.1] - 2026-07-16

### Fixed

- `restore`: translate original paths to `/trash` entries for
  `proton-drive >= 0.5.0`, which stopped accepting original-path restores
  (#57, regression from the 0.5.0 pin bump in #54, caught by the live
  release gate from #11).

### Upgrade notes

- **proton-drive 0.5.0 restore semantics**: upstream 0.5.0 removed
  original-path restore — `filesystem restore` now only accepts `/trash/<name>`
  paths, resolved by decrypted name with first-match-wins (node UIDs are
  rejected under `/trash`, so same-named trash entries can't be disambiguated
  by UID). protonfs handles this transparently from this version onward: it
  tries the original-path form first (so `PROTONFS_DRIVE_BIN` downgrades to
  0.4.6 keep working unchanged), falls back to resolving the `/trash` entry
  by (basename, parent UID) on newer versions, and refuses to act when a
  stale same-named trash entry would shadow the intended one rather than
  silently restoring the wrong node.

## [0.14.0] - 2026-07-16

### Added

- Install: pin proton-drive **0.5.0** for all platforms (linux-x64,
  linux-arm64, darwin-x64, darwin-arm64), each pin independently verified
  against the upstream release manifest and a downloaded binary hash. Windows
  scope for 1.0 is WSL-only — native Windows stays unsupported since
  protonfs's Secret Service keyring integration and POSIX path handling are
  untested there (#54, closes #8, #9, #10).
- CI: gate release tags on the full test matrix passing with an 80% coverage
  floor, plus a live-suite release gate for milestone/manual releases (#55,
  issue #11).

### Upgrade notes

- **proton-drive pin bump 0.4.6 -> 0.5.0**: the CLI surface protonfs uses
  (`filesystem list/upload/download/trash/restore/delete/create-folder`,
  `auth`, `version`) is unchanged between 0.4.6 and 0.5.0 except for an added
  `sharing leave` subcommand that protonfs does not use. The one behavioral
  break upstream introduced — `restore` no longer accepting original paths —
  surfaced immediately in the live release gate and is fixed transparently in
  0.14.1 (see above); no action is needed on the strength of this release
  alone if you upgrade straight to 0.14.1+.

## [0.13.0] - 2026-07-16

### Added

- `setup`/`push`: subdir-safe setup (skips the repo-wide git-LFS migration
  when the protonfs root is a subdirectory of a larger git repo rather than
  the git toplevel), LFS-exempt control files, and auto-creation of the
  configured `remote_root` on Drive (#53, closes #17, #19, #20).

### Upgrade notes

- **`.protonfs/.gitignore` and `.protonfs/.gitattributes` control files**:
  `protonfs setup` now writes `.protonfs/.gitattributes` so protonfs's own
  control files are exempt from git-LFS (a clone without an LFS pull
  previously got 130-byte pointer stubs instead of real config), and
  `.protonfs/.gitignore` so the local-only `index.json` and
  `refresh-state.json` stay untracked while `config.json` and `ignore`
  remain tracked. Both are written idempotently and preserve any
  user-added lines, so re-running `setup` on an existing repo is safe.

## [0.12.0] - 2026-07-16

### Added

- `refresh`: resumable BFS-frontier persistence, so an interrupted remote
  walk resumes from where it left off instead of restarting (#52, closes
  #33 item 2).

## [0.11.0] - 2026-07-16

### Added

- `pull`: `--resolve=remote|local|both` to control how local/remote
  divergence is resolved (#49).

## [0.10.0] - 2026-07-16

### Added

- `offload`: reclaim local disk space for tracked files already verified
  present on the remote (#51, closes #25).

## [0.9.0] - 2026-07-16

### Added

- `lfs`: detect git-LFS pointer stubs during scan/classify and never push a
  stub over real remote content (#50, closes #32).

## [0.8.0] - 2026-07-16

### Added

- `status`: distinct exit codes for clean/drift/conflict states, so scripts
  can branch on sync state without parsing output (#48, closes #7).

## [0.7.0] - 2026-07-16

### Added

- `diff`: direction-aware sync states and local-deletion detection (#47,
  closes #24).

### Docs

- `rm`: document the permanent-delete duplicate-basename limitation, plus a
  uid-probe test (#46, closes #6).

## [0.6.0]–[0.6.1] - 2026-07-16

### Added

- `locking`: an advisory repo lock held around index-mutating commands, so
  concurrent protonfs invocations against the same repo don't race on the
  index (#38, closes #2).

### Fixed

- Release pipeline: pass the version tag explicitly through `workflow_call`
  and drop a stray tag on `main`, plus disable PEP 740 attestations so the
  auto-release path can publish to PyPI (#44, #45 — follow-ups to #31).

## [0.3.1]–[0.5.0] - 2026-07-15 to 2026-07-16

Four rapid successive merges landed as separate patch/minor releases within
minutes of each other during initial hardening; grouped here as one entry.

### Added

- `index`: schema versioning and a forward migration path, so future index
  format changes don't strand existing repos (#41, closes #4).
- `refresh`: incremental persistence and throttle backoff for the remote
  walk, so a long walk survives interruption and backs off under rate
  limiting (#42, closes #33).
- `push`/`pull`: resumable, idempotent transfers via per-group persistence,
  so a batch that fails partway can be safely re-run (#43, closes #3).
- CI: auto-bump + auto-tag SemVer on merge to `main` — the automation this
  changelog process itself now plugs into (#34, closes #31).

### Fixed

- `index`: write the index atomically via a temp file + `os.replace`, so a
  crash mid-write can't corrupt it (#35, closes #1).
- `push`: verify uploads against what the remote actually reports rather
  than trusting `proton-drive`'s own counts (#36, closes #22).

## [0.3.0] - 2026-07-15

Initial hardened release. protonfs's core command surface, established over
the preceding development history and its first tagged release:

### Added

- Core commands: `config`, `index`, `ignore`, `drive` (subprocess wrapper),
  `localscan`, `diff`, `context`, `status`, `ls`, `push`, `pull`, `rm`/
  `restore`, `setup` (with git-LFS migration), `refresh`, and recursive
  `ls --remote` / `RemoteEntry` remote listing.
- `setup`: self-diagnosing proton-drive installer with auth passthrough.
- CI/release scaffolding: package CI, PyPI release workflow, docs, and a
  version badge resolved from git tags.

### Fixed

- `secretservice`: wait for `gnome-keyring` to claim the D-Bus name, making
  proton-drive usable on headless hosts.
- hatch-vcs configured to exclude local version identifiers, restoring PyPI
  compatibility.
- Assorted final-review hardening: drive-error boundary, `.gitignore`
  line-matching, git-mutation error wrapping, `pathspec` deprecation, subpath
  prune data-loss fix.

[Unreleased]: https://github.com/will-roscoe/protonfs/compare/v1.5.1...HEAD
[1.5.1]: https://github.com/will-roscoe/protonfs/compare/v1.5.0...v1.5.1
[1.5.0]: https://github.com/will-roscoe/protonfs/compare/v1.4.1...v1.5.0
[1.4.1]: https://github.com/will-roscoe/protonfs/compare/v1.4.0...v1.4.1
[1.4.0]: https://github.com/will-roscoe/protonfs/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/will-roscoe/protonfs/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/will-roscoe/protonfs/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/will-roscoe/protonfs/compare/v1.0.3...v1.1.0
[1.0.3]: https://github.com/will-roscoe/protonfs/compare/v1.0.2...v1.0.3
[1.0.2]: https://github.com/will-roscoe/protonfs/compare/v1.0.1...v1.0.2
[1.0.1]: https://github.com/will-roscoe/protonfs/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/will-roscoe/protonfs/compare/v0.17.0...v1.0.0
[0.17.0]: https://github.com/will-roscoe/protonfs/compare/v0.16.0...v0.17.0
[0.16.0]: https://github.com/will-roscoe/protonfs/compare/v0.15.1...v0.16.0
[0.15.1]: https://github.com/will-roscoe/protonfs/compare/v0.15.0...v0.15.1
[0.15.0]: https://github.com/will-roscoe/protonfs/compare/v0.14.1...v0.15.0
[0.14.1]: https://github.com/will-roscoe/protonfs/compare/v0.14.0...v0.14.1
[0.14.0]: https://github.com/will-roscoe/protonfs/compare/v0.13.0...v0.14.0
[0.13.0]: https://github.com/will-roscoe/protonfs/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/will-roscoe/protonfs/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/will-roscoe/protonfs/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/will-roscoe/protonfs/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/will-roscoe/protonfs/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/will-roscoe/protonfs/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/will-roscoe/protonfs/compare/v0.6.1...v0.7.0
[0.6.1]: https://github.com/will-roscoe/protonfs/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/will-roscoe/protonfs/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/will-roscoe/protonfs/compare/v0.3.1...v0.5.0
[0.3.1]: https://github.com/will-roscoe/protonfs/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/will-roscoe/protonfs/releases/tag/v0.3.0
</content>
