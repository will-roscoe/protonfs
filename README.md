# protonfs

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

## License

[PolyForm Noncommercial 1.0.0](LICENSE) — free for noncommercial use with
attribution; contact the author for commercial use.
