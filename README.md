# protonfs

Sync a local directory tree with [Proton Drive](https://proton.me/drive), via
the official [Proton Drive CLI](https://github.com/ProtonDriveApps/sdk/tree/main/cli),
with conflict-aware push/pull and a local sync manifest.

Originally built to replace git-lfs as the storage layer for large,
write-once simulation output — data that doesn't need version history, just
somewhere durable to live and a way to fetch it back on demand.

**Status: early scaffolding.** The command surface (`setup`, `status`, `ls`,
`push`, `pull`, `rm`, `restore`) is defined but not yet implemented — see
`src/protonfs/cli.py`.

## Requirements

- Python >= 3.9
- The [`proton-drive`](https://proton.me/download/drive/cli/index.html) CLI
  binary on `PATH`, authenticated (`proton-drive auth login`)

## Install

```bash
pip install protonfs
```

## License

[PolyForm Noncommercial 1.0.0](LICENSE) — free for noncommercial use with
attribution; contact the author for commercial use.
