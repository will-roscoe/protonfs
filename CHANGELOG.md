# Changelog

All notable changes to protonfs are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versioning
follows [Semantic Conventions](https://semver.org/) via the automated
`auto-release.yml` pipeline (issue #31): every merge to `main` is classified
from its Conventional Commit messages and, if warranted, tagged automatically.

## [Unreleased]

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

[Unreleased]: https://github.com/will-roscoe/protonfs/compare/v0.17.0...HEAD
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
