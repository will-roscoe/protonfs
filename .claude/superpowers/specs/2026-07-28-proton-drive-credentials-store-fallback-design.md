# Design: `pass` credentials-store autofallback

**Date:** 2026-07-28
**Status:** APPROVED (2026-07-28) — §7.1 resolved: silent-with-notice keygen at `auth login`.
**Depends on:** proton-drive `0.6.0` pin (PR #122) — the `pass` store lands upstream in
`0.6.0`, so this feature must not ship in a protonfs release that still pins `0.5.0`.

---

## 1. Problem

`proton-drive` persists its authenticated session to a credentials store. On Linux the
default (`keychain`) is the freedesktop Secret Service, reached over a D-Bus session bus.
A headless host (SSH, no graphical login — e.g. exo2 / CentOS 7) has neither a session bus
nor an unlocked Secret Service collection, so protonfs carries `secretservice.py`: a ~470
line bootstrap that launches a private D-Bus bus and a `gnome-keyring-daemon` against a
protonfs-owned `XDG_DATA_HOME`, working around the sealed system `login.keyring`.

That bootstrap works but is the single most fragile part of protonfs on headless hosts:
it depends on `dbus-launch`, `gnome-keyring-daemon`, `gdbus`, a daemonize race
(`wait_for_secret_service`), and a locked-collection `--replace` dance.

proton-drive `0.6.0` adds a second store: `PROTON_DRIVE_CREDENTIALS_STORE=pass`
([password-store](https://www.passwordstore.org/)). `pass` is GPG-encrypted files with
**no D-Bus, no daemon, no session bus** — a structurally better fit for headless hosts.
This feature makes protonfs **fall back to `pass` automatically** when the Secret Service
cannot be made ready, so a headless login "just works" without the D-Bus bootstrap.

## 2. Goals / non-goals

**Goals**
- On a host where the Secret Service cannot be made ready, protonfs establishes and uses a
  `pass`-backed store instead, with no manual setup — including generating the GPG key and
  initializing the store, mirroring how protonfs already auto-generates and stores the
  gnome-keyring password.
- The store choice is **sticky**: once a host is established on a store, every later command
  uses the same one (else proton-drive reads an empty store and reports "not authenticated").
- The per-command hot path (`drive_env()`, run on *every* proton-drive subprocess) stays
  cheap and never generates keys or raises.
- `protonfs doctor` reports which store is active and whether `pass` is usable; `doctor --fix`
  can establish the `pass` store explicitly.
- User overrides (native `PROTON_DRIVE_CREDENTIALS_STORE`, and a protonfs-level
  `PROTONFS_CREDENTIALS_STORE`) always win and are never second-guessed.

**Non-goals**
- Not replacing the Secret Service path. `keychain` stays the default where it works
  (desktops, and headless hosts where the existing bootstrap succeeds).
- Not touching macOS: proton-drive uses the platform Keychain there; nothing to bootstrap.
- Not managing the user's real `~/.gnupg` or `~/.password-store`. protonfs uses an
  **isolated, protonfs-managed** GNUPGHOME + store dir (see §4), so it never pollutes or
  depends on the user's personal GPG/pass setup.

## 3. How proton-drive uses `pass` (verified against the SDK, `cli/src/credentials/`)

- Store selection is **only** `PROTON_DRIVE_CREDENTIALS_STORE` ∈ {`keychain` (default),
  `pass`}. No other knob.
- The CLI runs `Bun.spawn(['pass', ...args])` **with no explicit `env`**, so `pass`
  inherits the proton-drive process environment. Therefore setting `PASSWORD_STORE_DIR`
  and `GNUPGHOME` in the subprocess env redirects `pass`/`gpg` to a protonfs-managed store
  — the same mechanism as the existing `XDG_DATA_HOME` override for gnome-keyring.
- Fixed entry path: `ch.proton.drive/drive-sdk-cli/auth-session`.
  - save → `pass insert -f -m ch.proton.drive/drive-sdk-cli/auth-session` (needs an
    initialized store).
  - load → `pass show ch.proton.drive/drive-sdk-cli/auth-session` (missing entry ⇒ treated
    as "no session"; the CLI hints "ensure pass is installed and gpg-agent can decrypt").
- Consequence: the store must be `pass init`'d against a GPG key. A **passphrase-less** key
  means `gpg-agent` never prompts — required for unattended headless use.

> **Validation checkpoint (exo2):** confirm empirically that proton-drive's `pass` invocation
> honors the inherited `PASSWORD_STORE_DIR`/`GNUPGHOME`, and that `pass show`/`insert` against
> the protonfs-managed store round-trips a session. This is the one thing we can't fully prove
> from source alone (Bun env inheritance is documented, but the end-to-end path deserves a live
> check).

## 4. Architecture

New module `src/protonfs/credstore.py` — the credentials-store resolver and `pass`
bootstrap. `secretservice.py` stays the Secret Service specialist; `credstore.py` decides
*which* store and, for `pass`, owns its setup. `drive_env()` becomes the composition point.

**protonfs-managed paths** (all under the existing `state_dir()` =
`~/.local/share/protonfs`):
- `GNUPGHOME`         → `state_dir()/gnupg`   (0700)
- `PASSWORD_STORE_DIR`→ `state_dir()/password-store`
- sticky choice file  → `state_dir()/credentials-store` (contains `keychain` | `pass`)

**GPG key:** a passphrase-less key generated via `gpg --batch --generate-key` in the
protonfs GNUPGHOME, identity `protonfs (Proton Drive CLI session store)
<protonfs@localhost>`. Same security posture as today's 0600 keyring password (documented
in `secretservice.py`): local-attacker-equivalent, strictly better than "no session at all".

**Env vars**
- `PROTON_DRIVE_CREDENTIALS_STORE` — native selector protonfs sets for the subprocess.
- `PROTONFS_CREDENTIALS_STORE` (new) — protonfs-level override: `keychain` | `pass` | `auto`
  (default `auto`). Forces a store and persists it.
- `PROTONFS_NO_KEYRING_BOOTSTRAP` (existing) — unchanged; still opts out of *all* store
  bootstrapping (both Secret Service and pass).

## 5. Resolution order

`resolve_credentials_store(env)` → returns the store name + the env additions, deciding
**once** and persisting:

1. If `PROTON_DRIVE_CREDENTIALS_STORE` is already set in the incoming env → respect it
   verbatim (the user or their shell configured proton-drive directly). Do not override,
   do not persist. If it is `pass`, still inject the protonfs `PASSWORD_STORE_DIR`/`GNUPGHOME`
   **only if** the user has not set those themselves.
2. Else if `PROTONFS_CREDENTIALS_STORE` ∈ {`keychain`,`pass`} → use it, persist it.
3. Else if the sticky choice file exists → use it (this is the steady state after
   establishment).
4. Else (first run, `auto`):
   - macOS → `keychain` (no bootstrap), persist.
   - Linux → attempt the existing Secret Service bootstrap. If it reaches `ready` →
     `keychain`, persist. Otherwise, if `pass` is *establishable* (see §6) → `pass`, persist.
     If neither → leave `PROTON_DRIVE_CREDENTIALS_STORE` unset (proton-drive defaults to
     keychain and emits its own error) — identical to today's failure mode; do **not**
     persist a choice.

**Hot-path rule:** step 4's key-generation/`pass init` (the expensive part) does **not** run
inside `drive_env()`. `drive_env()` only: reads the sticky file (or respects an override) and
injects the env vars. Establishment happens at the moments in §7.

## 6. `pass` establishment (`ensure_pass_store`)

Idempotent. Returns `(ready: bool, actions, warnings)`.
1. If `pass` not on PATH or `gpg` not on PATH → not establishable; return a warning naming
   the missing tool (`pass` is `pass` in EPEL on CentOS 7; `gpg` is `gnupg2`).
2. Ensure `GNUPGHOME` (0700) and `PASSWORD_STORE_DIR` exist.
3. If `PASSWORD_STORE_DIR/.gpg-id` exists → already initialized; ready.
4. Else: ensure a protonfs GPG key exists in GNUPGHOME (generate passphrase-less if absent),
   then `pass init <fingerprint>` (with `PASSWORD_STORE_DIR`/`GNUPGHOME` in env). Ready when
   `.gpg-id` is written.

This is the only place keys are generated. It is called from §7's establishment points,
never from the per-command path.

## 7. Where establishment happens

- **`auth login`** (`commands/auth.py`): the session is about to be written and the user is
  interactive. Before the passthrough, resolve the store; if resolution selects `pass` and the
  store isn't ready, run `ensure_pass_store` (with a one-line notice), then set the env. This
  is what makes the autofallback actually kick in on exo2 at the right, once-only moment.
- **`doctor --fix`**: establishes the resolved store explicitly (Secret Service bootstrap as
  today, or `ensure_pass_store`), and reports the outcome as checks.
- **`drive_env()` (hot path)**: never establishes. Reads the sticky choice + injects env.
  If the sticky choice is `pass` but the store has gone missing, proton-drive surfaces its own
  "no session" error and `doctor` diagnoses it — consistent with the module's "never raise in
  the hot path" contract.

### 7.1 RESOLVED — key-gen at `auth login`: silent-with-notice
Decided (2026-07-28): during interactive `auth login`, when the fallback selects `pass` and the
store isn't ready, protonfs generates the passphrase-less GPG key and `pass init`s the store
automatically, printing a single-line notice (e.g. `no OS keyring available; initializing a
protonfs-managed pass store`). This mirrors `secretservice.py`'s existing auto-generation of the
keyring password. No confirm flag; `PROTONFS_NO_KEYRING_BOOTSTRAP=1` remains the global opt-out.

## 8. `doctor` changes

- New check **credentials store**: shows the active/resolved store and how it was chosen
  (override / sticky / auto), e.g. `pass (auto: Secret Service unavailable)`.
- When the resolved store is `pass`: checks for `pass`/`gpg` on PATH, store initialized
  (`.gpg-id` present), and a round-trip probe (`pass insert` a throwaway entry, `pass show`
  it, `pass rm`) — the `pass` analogue of `probe_secret_service`.
- The existing Secret Service checks remain but become conditional: only shown/failing when
  the active store is `keychain`. A host that has fallen back to `pass` should not FAIL doctor
  for a missing Secret Service it no longer uses.

## 9. Testing

**Unit (host-independent, injected runners — matches `test_secretservice.py` style):**
- resolution order: each of the 4 branches, override precedence, sticky read/write.
- `ensure_pass_store`: missing `pass`/`gpg` → warning; `.gpg-id` present → ready no-op; absent
  → keygen + init sequence (asserted via a fake runner, no real gpg).
- `drive_env()` hot path: never calls establishment; injects the right env for a sticky choice;
  respects a pre-set `PROTON_DRIVE_CREDENTIALS_STORE`.
- doctor: `pass` checks pass/fail rendering; Secret Service checks suppressed when store=pass.

**Live / exo2 (the validation checkpoint, §3):**
- On exo2: fresh state, no Secret Service → `auth login` establishes pass, session persists,
  `filesystem list /` works, a second command reuses the sticky pass store without re-login.
- Confirm proton-drive honors the inherited `PASSWORD_STORE_DIR`/`GNUPGHOME`.
- Regression: a host with a working Secret Service still selects `keychain` (no behavior change).

## 10. Risks

- **proton-drive env inheritance for `pass`** — validated from source (no explicit `env` in
  `Bun.spawn`), but must be confirmed live on exo2 (§3 checkpoint). If it does *not* honor the
  inherited dirs, fallback: use the user's default `~/.password-store`/`~/.gnupg` (drop the
  isolation), or require the user to pre-`pass init`. Decide only if the checkpoint fails.
- **`pass` not installed on exo2** — likely (needs EPEL). Then autofallback can't engage and we
  stay on the (fragile) gnome-keyring path; doctor must say so clearly. Not a regression.
- **GPG entropy on headless** — batch keygen can block on low entropy; use an ed25519/`default`
  key (fast, low entropy) and document `rng-tools`/`haveged` if it stalls.
- **Store flip breaking auth** — mitigated by stickiness (§5 step 3) and by never re-deciding
  once a choice is persisted.

## 11. Rollout

Separate branch/PR from the `0.6.0` re-pin (PR #122), since this needs design sign-off and
exo2 validation. Order: (1) `credstore.py` + unit tests, (2) wire `drive_env()`/`auth`/`doctor`,
(3) docs (headless guide + env-var reference), (4) exo2 live validation, (5) CHANGELOG +
version directives (`versionadded:: <next>` on new public API).
