# protonfs

![](https://raw.githubusercontent.com/will-roscoe/protonfs/main/.github/badges/version.svg)
![](https://raw.githubusercontent.com/will-roscoe/protonfs/main/.github/badges/pytest.svg)
![](https://raw.githubusercontent.com/will-roscoe/protonfs/main/.github/badges/ruff.svg)
![](https://raw.githubusercontent.com/will-roscoe/protonfs/main/.github/badges/pyversion.svg)
![](https://raw.githubusercontent.com/will-roscoe/protonfs/main/.github/badges/coverage.svg)
![](https://raw.githubusercontent.com/will-roscoe/protonfs/main/.github/badges/interrogate-badge.svg)

Sync a local directory tree with [Proton Drive](https://proton.me/drive), via
the official [Proton Drive CLI](https://github.com/ProtonDriveApps/sdk/tree/main/cli),
with conflict-aware push/pull and a local sync manifest.

Originally built to replace git-lfs as the storage layer for large,
write-once simulation output — data that doesn't need version history, just
somewhere durable to live and a way to fetch it back on demand.

The command surface (`setup`, `status`, `ls`, `push`, `pull`, `rm`, `restore`,
`refresh`, `install-drive`, `auth`) is implemented — see `src/protonfs/cli.py`.

## Requirements

- Python >= 3.9
- The [`proton-drive`](https://proton.me/download/drive/cli/index.html) CLI
  binary — install it with `protonfs install-drive` (below), or supply your own
  on `PATH` / via `PROTONFS_DRIVE_BIN`.

## Install

```bash
pip install protonfs
protonfs install-drive     # downloads + SHA-512-verifies the official proton-drive binary
protonfs auth login        # opens a URL to authenticate (passthrough to proton-drive)
```

`install-drive` detects your platform, requires AVX2 for the linux-x64 prebuilt
(with an instructive fallback otherwise), and never installs a binary whose
SHA-512 does not match the pinned checksum. Override the version with
`PROTONFS_DRIVE_VERSION` and the expected checksum with `PROTONFS_DRIVE_SHA512`.

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

## License

[PolyForm Noncommercial 1.0.0](LICENSE) — free for noncommercial use with
attribution; contact the author for commercial use.
