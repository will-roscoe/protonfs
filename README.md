<p align="center">
  <a href="https://github.com/will-roscoe/protonfs">
    <img
      src="https://raw.githubusercontent.com/will-roscoe/protonfs/main/.github/status/status.svg"
      alt="protonfs — version, links, test/coverage/lint/docs status, Python support and build matrix"
      width="900">
  </a>
</p>


<!-- SYNC:overview START - generated from docs/_shared/overview.rst, do not edit here -->

Sync a local directory tree with [Proton Drive](https://proton.me/drive), via the official [Proton Drive CLI](https://github.com/ProtonDriveApps/sdk/tree/main/cli), with conflict-aware push/pull and a local sync manifest.

Originally built to replace git-lfs as the storage layer for large, write-once simulation output -- data that does not need version history, just somewhere durable to live and a way to fetch it back on demand.

### Why protonfs

- **Conflict-aware push/pull** over a local sync manifest (`.protonfs/index.json`), so each machine knows what it has, what the remote has, and what diverged.
- **Verify-before-delete offload** -- reclaim local disk space only for files proven byte-for-byte present on Drive (via Proton's plaintext size/digest, not the encrypted size).
- **Headless-first**: a keyring/session-bus bootstrap and a `doctor` that diagnoses and repairs the Secret Service, so it works over SSH with no desktop.
- **Durable by design**: atomic index writes, an advisory repo lock, resumable refresh under API throttling, and SHA-512-pinned proton-drive binaries.
- **A frozen 1.0 command surface**: every command, option, exit code, and config key is a documented, stable contract.

### Requirements

- Python >= 3.9
- The `proton-drive` CLI binary -- install it with `protonfs install-drive`, or supply your own on `PATH` / via `PROTONFS_DRIVE_BIN`.

### Install

```bash
pip install protonfs
protonfs install-drive     # downloads + SHA-512-verifies the official proton-drive binary
protonfs auth login        # opens a URL to authenticate (passthrough to proton-drive)
```

### Quickstart

```bash
cd ~/my-project
protonfs setup             # init .protonfs/, prompt for the Drive path to sync into
protonfs push --dry-run    # preview what would upload (changes nothing)
protonfs push              # upload local files to Drive
protonfs status            # confirm everything is in sync (exit 0 == clean)
```

On a headless server, run `protonfs doctor --fix` before `auth login` to prepare the keyring first.

<!-- SYNC:overview END -->

More: **[full documentation](https://will-roscoe.github.io/protonfs)** ·
[task guide](https://will-roscoe.github.io/protonfs/getting-started/guide.html) ·
[command reference](https://will-roscoe.github.io/protonfs/reference/index.html).

`install-drive` detects your platform (linux-x64/arm64, macOS x64/arm64 — all
checksum-pinned), requires AVX2 for the linux-x64 prebuilt (with an instructive
fallback otherwise), and never installs a binary whose SHA-512 does not match
the pinned checksum. Override the version with `PROTONFS_DRIVE_VERSION` and the
expected checksum with `PROTONFS_DRIVE_SHA512`. On Windows, use WSL — native
Windows is out of scope for 1.0.

## Headless Linux (SSH, no desktop)

`proton-drive` keeps its session in the OS keyring, which on Linux means the
freedesktop Secret Service reached over the D-Bus **session bus**. An SSH login
has neither, which produces two failures that look like bugs in Proton Drive but
are really a missing environment:

```
Cannot autolaunch D-Bus without X11 $DISPLAY          # no session bus at all
Cannot create an item in a locked collection          # bus exists; the keyring is sealed
```

The second one is the nastier of the two: if the machine has ever had a graphical
login, `~/.local/share/keyrings/login.keyring` exists, is the *default* collection,
and is locked with a password you cannot type over SSH — so `auth login` completes
the whole browser flow and only then fails to save the session.

protonfs handles both for you. Every command that shells out to `proton-drive`
first reuses (or starts, and caches) a session bus, and runs `gnome-keyring-daemon`
against a protonfs-owned keyring directory so it never has to unlock the sealed
system keyring. To check a host:

```bash
protonfs doctor          # binary, session bus, Secret Service, and a real keyring write test
protonfs doctor --fix    # ...and repair what it can
```

Requires `dbus-launch`, `gnome-keyring-daemon` and `gdbus` (packages `dbus`/`dbus-x11`,
`gnome-keyring`, `glib2`). No root needed. To run the `proton-drive` binary by hand in
the same environment, use `eval "$(protonfs shell-init)"`.

Escape hatches: `PROTONFS_KEYRING_PASSWORD` supplies your own keyring password
instead of the generated one, and `PROTONFS_NO_KEYRING_BOOTSTRAP=1` turns all of
this off if you'd rather manage the environment yourself.

## Scoping what gets synced

`.protonfs/ignore` is a denylist in gitignore syntax, scoped to a repo and
independent of its own `.gitignore` — patterns like `*.tmp` or `core.*` are
excluded from every push/pull/refresh/status/ls.

### Syncing only matching files

Sometimes you want the opposite: sync *only* files of certain types (e.g. only
simulation dumps, ignoring notes/scratch/logs). Add an allowlist at
`.protonfs/include`, in the same gitignore syntax:

```gitignore
# .protonfs/include
*.ev
*.sink
*_[0-9][0-9][0-9][0-9][0-9]
```

When `.protonfs/include` exists and has at least one active (non-blank,
non-comment) line, a file is synced only if it matches one of its patterns —
and still not matched by `.protonfs/ignore`, which always wins over include.
If `include` is absent, or every line in it is blank/commented out, behaviour
is unchanged: everything not matched by `ignore` is synced.

Patterns are plain gitignore file patterns, matched only against file paths.
You don't need `!*/` or `dir/**` tricks here — directories are always
descended into regardless of include/ignore, so a plain `*.ev` reaches files
at any depth.

If you'd rather not add a second file, the same "only these files" behaviour
can be expressed with `.protonfs/ignore` alone, but it needs a double-negation
recipe and has two sharp edges:

```gitignore
# .protonfs/ignore -- sync only *.ev/*.sink and files ending in a 5-digit run number
*
!*/
!*_[0-9][0-9][0-9][0-9][0-9]
!*.ev
!*.sink
*mload*/**
```

- `!*/` is mandatory: once a parent directory is excluded, a later
  re-include pattern (like `!*.ev`) cannot resurrect files under it — gitignore
  semantics never descend into an already-excluded directory to re-evaluate
  its contents.
- to exclude a whole subtree again (here, anything under a `*mload*`
  directory) you must write `dir/**`, not `dir/` — a trailing-slash directory
  pattern does not match the files beneath it when tested against file paths
  the way protonfs's matcher does.

`.protonfs/include` avoids both pitfalls, which is why it exists as a
separate first-class file rather than only being achievable via `ignore`.

## Releasing (maintainers)

See [CHANGELOG.md](CHANGELOG.md) for release history and upgrade notes. To
upgrade an installation: `pip install --upgrade protonfs`, then `protonfs
upgrade` to bring the proton-drive binary and any repo state current (docs:
[Upgrading](https://will-roscoe.github.io/protonfs/upgrading.html)).

Merges to `main` auto-tag a release, but only after the full test matrix passes
with an 80% coverage floor on the exact commit being tagged (`auto-release.yml`
calls the CI workflow before creating the tag). Before milestone or manual tags,
also run the live suite against a **disposable** Drive directory — it exercises
real uploads/downloads that CI never can:

```bash
PROTONFS_TEST_REMOTE=/my-files/test .github/scripts/release_gate.sh
```

## License

[PolyForm Noncommercial 1.0.0](LICENSE) — free for noncommercial use with
attribution; contact the author for commercial use.
