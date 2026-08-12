# protonfs TODO

Working scratch list (uncommitted). Latest release: **v1.10.0** (proton-drive 0.6.0 pin,
`pass` credentials-store autofallback, `--resolve` vocab alignment, per-binary test matrix).

---

## Config: expose more attrs as user-settable defaults/overrides

Today the only config-backed surface is `Defaults` in `src/protonfs/config.py`
(`on_conflict`, `low_io`, `event_log`, `progress_style`, `batch_size`), each with a
`PROTONFS_*` env override via `_ENV_DEFAULTS_OVERRIDES`. Everything below is currently
**env-only** (read straight from `os.environ`) or sticky-file only, and could/should be
configurable through the existing layered config (global > shared > local > env).

**Layering nuance to decide per key:** committed `config.json` (shared, per-repo) vs
`config.local.json` (per-device, gitignored) vs global. Host-specific settings
(credentials store, keyring, binary path) belong in **local/global**, never the committed
shared file — they differ per machine. Repo-tuning settings (throttle/timeouts) fit the
shared file. The `_ENV_DEFAULTS_OVERRIDES` pattern is the template to extend.

- [ ] **`credentials_store`** (primary ask). Today: `PROTONFS_CREDENTIALS_STORE` env +
  sticky file `state_dir()/credentials-store` (`src/protonfs/credstore.py`). Add a config
  key so a user can pin `keychain`/`pass`/`auto` without an env var. Host-specific →
  local/global config (NOT committed shared). Precedence to preserve:
  native `PROTON_DRIVE_CREDENTIALS_STORE` > `PROTONFS_CREDENTIALS_STORE` env > config key >
  sticky file > auto. Update `credstore.drive_env`/`establish`/`active_store` to consult
  config, `docs/reference/index.rst` (envvar + new confval), and `test_credstore.py`.
- [ ] **Throttle/timeout knobs** (`src/protonfs/drive.py`, env-only today): `LIST_TIMEOUT`,
  `LIST_RETRIES`, `LIST_BACKOFF`, `LIST_BACKOFF_CAP`, `TRANSFER_TIMEOUT`,
  `TRANSFER_RETRIES`, `TRANSFER_BACKOFF`, `TRANSFER_BACKOFF_CAP` (all `PROTONFS_*`). Promote
  to `Defaults` fields so a user can persist per-repo tuning (valuable on throttled/HPC
  hosts) instead of exporting 8 env vars. Repo-tunable → shared config is fine. These are
  currently module-level constants read at import; would need to move to config-read.
- [ ] **Binary/version selection** (`src/protonfs/install.py`, env-only): `PROTONFS_DRIVE_BIN`,
  `PROTONFS_DRIVE_VERSION` (and `PROTONFS_DRIVE_SHA512`). Host-specific → local/global
  config could let a machine pin a binary path/version without env. Lower priority.
- [ ] **Keyring bootstrap** (`src/protonfs/secretservice.py`, env-only):
  `PROTONFS_NO_KEYRING_BOOTSTRAP` (bool) is a reasonable local-config key;
  `PROTONFS_KEYRING_PASSWORD` should stay env/secret (do NOT persist a secret to config).
- [ ] Audit for any other hardcoded-or-env constant that a user might reasonably want to set
  (e.g. GPG key params/algorithm in `credstore._GPG_GEN_PARAMS`, managed dir locations).
  Most are fine as-is; list is above in priority order.

Design note: a broad "make everything configurable" is a mini-project — do it as a single
"config surface v2" pass (schema additions + `_ENV_*_OVERRIDES` wiring + confval/envvar
docs + `to_dict`/`from_dict` round-trip tests + stability-contract update), not piecemeal.

## Binary test matrix — deepen the auth tier

`tests/test_binary_matrix.py` currently does install+verify, version parse, and the
pass-compat gate per binary (offline), plus a live list-root round-trip (auth tier). The
version-adaptive code paths are still only fake-tested:

- [ ] Per-version **restore semantics** in the auth tier: 0.4.6 accepts original-path
  `filesystem restore`; 0.5.0+ resolves `/trash/<name>` by decrypted name (first match
  wins). `drive.restore()` branches on this — exercise it against the *real* 0.4.6 and
  0.5.0 binaries (create → trash → restore → verify) to prove the adaptation.
- [ ] **Pass session round-trip** in the matrix for 0.6.0 (login under
  `PROTONFS_CREDENTIALS_STORE=pass`, `auth status` reads back) — validated manually on exo2
  this session, but not yet an automated matrix row.

## Docs

- [x] Version matrix (`docs/upgrading.rst` "Supported proton-drive versions") checked —
  current: lists 0.6.0 (highest + pass-required), 0.5.0, 0.4.6, matching
  `install.SUPPORTED_DRIVE_VERSIONS`. No change needed.
- [ ] Minor: `docs/upgrading.rst` still says "(the support matrix in :doc:`stability`)"
  while the explicit table now lives in `upgrading.rst` itself. Reconcile the cross-ref so
  there's one canonical location.

## v2 (deferred — do not action until v2 is kicked off)

- [ ] Deprecate then remove the **divergent push/pull `--resolve` vocabulary**. v1.10.0
  unified on `remote|local|both` but kept the proton-drive strategy names
  (`merge|keep-both|replace|skip` on push) and the `replace` alias (pull) as first-class
  synonyms for backward compat. For v2: add deprecation warnings on the legacy values, then
  drop them so only `remote|local|both` remains. This is a breaking change (frozen 1.0
  contract + existing scripts/schedule jobs store `--resolve=replace`) → major bump, needs a
  migration for stored schedule jobs (`schedule.py` persists the raw resolve value).
