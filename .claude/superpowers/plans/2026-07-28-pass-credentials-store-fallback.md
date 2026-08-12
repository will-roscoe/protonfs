# `pass` Credentials-Store Autofallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When proton-drive's Secret Service (`keychain`) store can't be made ready on a headless Linux host, protonfs automatically establishes and uses an isolated, protonfs-managed `pass` credentials store instead — no manual setup.

**Architecture:** A new `credstore.py` module owns store *selection* (which of `keychain`/`pass`) and `pass` *bootstrap* (GPG keygen + `pass init`). The choice is persisted (sticky) so it never flips mid-session. The per-command hot path (`DriveClient._drive_env` → `credstore.drive_env`) only *reads* the sticky choice and injects env vars; the expensive establishment (key generation) happens only at `auth login` and `doctor --fix`. `secretservice.py` is unchanged and remains the `keychain` specialist that `credstore` delegates to.

**Tech Stack:** Python 3, `subprocess` (injected-runner pattern), `pytest`, Click. External binaries: `pass`, `gpg` (both optional; absence degrades gracefully to the existing gnome-keyring path).

**Branch & release gate:** Work happens on `feat/credentials-store-pass-fallback`, branched off `chore/proton-drive-0.6.0` (PR #122) — NOT master, because the `pass` store only exists in proton-drive 0.6.0. Do not merge PR #122 or cut a release until this implementation works (unit tests green AND the exo2 live checklist passes). When both are ready, the pin and this feature ship together.

## Global Constraints

- Python floor and style: match the existing `src/protonfs/` code (from `__future__ import annotations`, dataclasses, module-level docstring with `.. versionadded::`). New public API gets `.. versionadded:: <next-release>` directives (baseline rule).
- Injected-runner testability: every function that shells out takes a `runner=_run` parameter defaulting to the module runner, so tests script it with a `FakeRunner` (see `tests/test_secretservice.py`). No test may launch a real daemon, generate a real GPG key, or require `pass`/`gpg` installed.
- Hot-path contract: `credstore.drive_env()` MUST NOT generate keys, run `pass init`, or raise — it mirrors `secretservice.drive_env`'s "never raise in the hot path" guarantee.
- Store stickiness: once a store is persisted to `state_dir()/credentials-store`, it is honored verbatim on every later call until an explicit override changes it.
- Override precedence (highest first): native `PROTON_DRIVE_CREDENTIALS_STORE` already in env → protonfs `PROTONFS_CREDENTIALS_STORE` (`keychain`|`pass`|`auto`, default `auto`) → sticky file → auto-resolution. `PROTONFS_NO_KEYRING_BOOTSTRAP=1` disables all bootstrap (both stores).
- Isolation: protonfs-managed `GNUPGHOME = state_dir()/gnupg` (0700) and `PASSWORD_STORE_DIR = state_dir()/password-store`. The GPG key is passphrase-less (identity `protonfs (Proton Drive CLI session store) <protonfs@localhost>`).
- Fixed proton-drive facts (verified against SDK `cli/src/credentials/`): store selector env var is `PROTON_DRIVE_CREDENTIALS_STORE` ∈ {`keychain`,`pass`}; the CLI runs `pass` with inherited env (so `PASSWORD_STORE_DIR`/`GNUPGHOME` are honored); session entry path is `ch.proton.drive/drive-sdk-cli/auth-session`.

---

### Task 1: `credstore.py` — paths, constants, sticky store-choice persistence

**Files:**
- Create: `src/protonfs/credstore.py`
- Test: `tests/test_credstore.py`

**Interfaces:**
- Consumes: `protonfs.secretservice.state_dir` (reused for the managed root; redirected in tests by patching `secretservice.Path.home`).
- Produces:
  - constants `STORE_ENV = "PROTON_DRIVE_CREDENTIALS_STORE"`, `PROTONFS_STORE_ENV = "PROTONFS_CREDENTIALS_STORE"`, `KEYCHAIN = "keychain"`, `PASS = "pass"`, `AUTO = "auto"`
  - `gnupg_home() -> Path`, `password_store_dir() -> Path`, `store_choice_file() -> Path`
  - `read_store_choice() -> str | None` (returns `"keychain"`/`"pass"` or `None`)
  - `write_store_choice(store: str) -> None` (writes the file, parents created)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_credstore.py
from __future__ import annotations

import pytest

from protonfs import credstore as cs
from protonfs import secretservice as ss


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(ss.Path, "home", staticmethod(lambda: tmp_path))
    return tmp_path


def test_managed_paths_live_under_state_dir(home):
    assert cs.gnupg_home() == ss.state_dir() / "gnupg"
    assert cs.password_store_dir() == ss.state_dir() / "password-store"
    assert cs.store_choice_file() == ss.state_dir() / "credentials-store"


def test_store_choice_round_trips(home):
    assert cs.read_store_choice() is None
    cs.write_store_choice(cs.PASS)
    assert cs.read_store_choice() == "pass"


def test_store_choice_rejects_garbage(home):
    cs.store_choice_file().parent.mkdir(parents=True, exist_ok=True)
    cs.store_choice_file().write_text("nonsense\n")
    assert cs.read_store_choice() is None  # unknown value is treated as unset
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_credstore.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'protonfs.credstore'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/protonfs/credstore.py
"""Select and bootstrap the proton-drive credentials store (keychain vs pass).

proton-drive 0.6.0 persists its session to `keychain` (freedesktop Secret Service,
the default) or `pass` (password-store), chosen by PROTON_DRIVE_CREDENTIALS_STORE.
On a headless host the Secret Service is expensive to provide (see
:mod:`protonfs.secretservice`); `pass` needs no D-Bus at all. This module picks the
store, falling back to a protonfs-managed `pass` store when the Secret Service cannot
be made ready, and makes that choice sticky so a later command never reads a different
(empty) store and reports "not authenticated".

.. versionadded:: <next-release>
"""
from __future__ import annotations

import os
from pathlib import Path

from protonfs.secretservice import state_dir

STORE_ENV = "PROTON_DRIVE_CREDENTIALS_STORE"
PROTONFS_STORE_ENV = "PROTONFS_CREDENTIALS_STORE"
KEYCHAIN = "keychain"
PASS = "pass"
AUTO = "auto"
_VALID_STORES = (KEYCHAIN, PASS)


def gnupg_home() -> Path:
    """The protonfs-managed GNUPGHOME handed to proton-drive's `pass`/`gpg`."""
    return state_dir() / "gnupg"


def password_store_dir() -> Path:
    """The protonfs-managed PASSWORD_STORE_DIR handed to proton-drive's `pass`."""
    return state_dir() / "password-store"


def store_choice_file() -> Path:
    """The file recording the sticky credentials-store choice for this host."""
    return state_dir() / "credentials-store"


def read_store_choice() -> str | None:
    """The persisted store choice (`keychain`/`pass`), or None if unset/unrecognized."""
    path = store_choice_file()
    if not path.exists():
        return None
    value = path.read_text().strip()
    return value if value in _VALID_STORES else None


def write_store_choice(store: str) -> None:
    """Persist the sticky store choice. Raises ValueError on an unknown store."""
    if store not in _VALID_STORES:
        raise ValueError(f"unknown credentials store: {store!r}")
    path = store_choice_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(store)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_credstore.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/protonfs/credstore.py tests/test_credstore.py
git commit -m "feat(credstore): managed paths + sticky store-choice persistence"
```

---

### Task 2: `pass` availability + env injection + `ensure_pass_store` (keygen + init)

**Files:**
- Modify: `src/protonfs/credstore.py`
- Test: `tests/test_credstore.py`

**Interfaces:**
- Consumes: `gnupg_home()`, `password_store_dir()` (Task 1).
- Produces:
  - `PASS_STORE_READY = "ch.proton.drive/drive-sdk-cli"` is NOT needed here; the entry path is proton-drive's concern.
  - `pass_env(base: dict[str, str]) -> dict[str, str]` — returns `base` plus `STORE_ENV=pass`, `PASSWORD_STORE_DIR`, `GNUPGHOME` (only filling ones the caller has not already set).
  - `class PassResult` dataclass `(ready: bool, actions: list[str], warnings: list[str])`.
  - `pass_tools_present() -> bool` — `pass` and `gpg` both on PATH.
  - `pass_store_initialized() -> bool` — `password_store_dir()/.gpg-id` exists.
  - `ensure_pass_store(runner=_run) -> PassResult` — idempotent; generates a passphrase-less GPG key in the managed GNUPGHOME (if none) and runs `pass init` (if `.gpg-id` absent).
  - `_run(cmd, env, stdin=None)` — the injectable subprocess runner (same shape as `secretservice._run`).

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_credstore.py  (top-of-file import: `from dataclasses import dataclass`)
from dataclasses import dataclass


@dataclass
class FakeCompleted:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class FakePass:
    """Scripts gpg/pass so ensure_pass_store can be exercised without real tools.

    Tracks whether a key exists and whether the store is initialized so the
    idempotent branches (already-ready, needs-keygen, needs-init) are all reachable.
    """

    def __init__(self, *, has_key=False):
        self.has_key = has_key
        self.calls: list[list[str]] = []

    def __call__(self, cmd, env, stdin=None):
        self.calls.append(cmd)
        if cmd[0] == "gpg":
            if "--list-secret-keys" in cmd:
                if not self.has_key:
                    return FakeCompleted(returncode=2, stderr="no secret keys")
                return FakeCompleted(stdout="sec:...\nfpr:::::::::ABCDEF0123456789:\n")
            if "--generate-key" in cmd or "--gen-key" in cmd:
                self.has_key = True
                return FakeCompleted()
        if cmd[0] == "pass" and cmd[1] == "init":
            # `pass init` writes .gpg-id; emulate that side effect.
            store = env["PASSWORD_STORE_DIR"]
            from pathlib import Path as _P
            _P(store).mkdir(parents=True, exist_ok=True)
            (_P(store) / ".gpg-id").write_text(cmd[2])
            return FakeCompleted()
        return FakeCompleted()


def test_pass_env_fills_only_unset(home):
    out = cs.pass_env({"PATH": "/usr/bin"})
    assert out[cs.STORE_ENV] == "pass"
    assert out["PASSWORD_STORE_DIR"] == str(cs.password_store_dir())
    assert out["GNUPGHOME"] == str(cs.gnupg_home())
    # a user-set PASSWORD_STORE_DIR is preserved
    out2 = cs.pass_env({"PASSWORD_STORE_DIR": "/custom"})
    assert out2["PASSWORD_STORE_DIR"] == "/custom"


def test_ensure_pass_store_generates_key_then_inits(home, monkeypatch):
    monkeypatch.setattr(cs.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    fake = FakePass(has_key=False)
    result = cs.ensure_pass_store(runner=fake)
    assert result.ready is True
    assert cs.pass_store_initialized() is True
    # a second call is a no-op: it returns on the `.gpg-id` check before touching
    # the runner, so NO gpg/pass command is issued. Assert on the calls directly
    # (asserting a specific flag is absent would pass vacuously if the flag string
    # ever changes).
    fake2 = FakePass(has_key=True)
    result2 = cs.ensure_pass_store(runner=fake2)
    assert result2.ready is True
    assert fake2.calls == []


def test_ensure_pass_store_missing_tools_warns(home, monkeypatch):
    monkeypatch.setattr(cs.shutil, "which", lambda tool: None)
    result = cs.ensure_pass_store(runner=FakePass())
    assert result.ready is False
    assert any("pass" in w or "gpg" in w for w in result.warnings)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_credstore.py -q`
Expected: FAIL — `AttributeError: module 'protonfs.credstore' has no attribute 'pass_env'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/protonfs/credstore.py  (imports at top: add os, shutil, subprocess; dataclasses)
# NOTE: `import os` is added HERE (not Task 1) because this is where `os.environ` is first
# used — keeping every commit ruff-clean (F401). Place `import os` before `from pathlib`.
import os
import shutil
import subprocess
from dataclasses import dataclass, field

_RUN_TIMEOUT = 30  # gpg keygen can be the slow one, but must not hang forever
GPG_IDENTITY = "protonfs (Proton Drive CLI session store) <protonfs@localhost>"


@dataclass
class PassResult:
    """Outcome of :func:`ensure_pass_store`."""

    ready: bool
    actions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _run(cmd: list[str], env: dict[str, str], stdin: str | None = None):
    """Run a subprocess with a bounded timeout (the injectable runner)."""
    return subprocess.run(
        cmd, env=env, input=stdin, capture_output=True, text=True, timeout=_RUN_TIMEOUT
    )


def pass_tools_present() -> bool:
    """Whether both `pass` and `gpg` are on PATH (required to use the pass store)."""
    return shutil.which("pass") is not None and shutil.which("gpg") is not None


def pass_store_initialized() -> bool:
    """Whether the managed pass store has been `pass init`'d (`.gpg-id` present)."""
    return (password_store_dir() / ".gpg-id").exists()


def pass_env(base: dict[str, str]) -> dict[str, str]:
    """`base` plus the pass selector and managed dirs, without clobbering user-set ones."""
    out = dict(base)
    out[STORE_ENV] = PASS
    out.setdefault("PASSWORD_STORE_DIR", str(password_store_dir()))
    out.setdefault("GNUPGHOME", str(gnupg_home()))
    return out


def _managed_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Env for running gpg/pass against the managed GNUPGHOME + store dir."""
    env = dict(os.environ if base is None else base)
    env["GNUPGHOME"] = str(gnupg_home())
    env["PASSWORD_STORE_DIR"] = str(password_store_dir())
    return env


def _gpg_fingerprint(env: dict[str, str], runner) -> str | None:
    """The fingerprint of the first secret key in the managed GNUPGHOME, or None."""
    result = runner(["gpg", "--list-secret-keys", "--with-colons", "--fingerprint"], env)
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("fpr:"):
            return line.split(":")[9]
    return None


def ensure_pass_store(runner=_run) -> PassResult:
    """Make the managed pass store usable: ensure a GPG key + `pass init`. Idempotent."""
    if not pass_tools_present():
        missing = " and ".join(
            t for t in ("pass", "gpg") if shutil.which(t) is None
        )
        return PassResult(
            ready=False,
            warnings=[
                f"cannot use the `pass` credentials store: {missing} not on PATH "
                f"(install `pass` and `gnupg2`)."
            ],
        )
    import stat as _stat

    gnupg_home().mkdir(parents=True, exist_ok=True)
    gnupg_home().chmod(_stat.S_IRWXU)  # gpg refuses a world-readable GNUPGHOME
    password_store_dir().mkdir(parents=True, exist_ok=True)
    if pass_store_initialized():
        return PassResult(ready=True)

    env = _managed_env()
    actions: list[str] = []
    fpr = _gpg_fingerprint(env, runner)
    if fpr is None:
        # Portable across GnuPG 2.0 and 2.1+ (the headless targets ship 2.0.x, which
        # lacks --quick-generate-key/--pinentry-mode). Params fed over stdin; %no-protection
        # gives a passphrase-less key on 2.1+ and is ignored on 2.0 (batch default is
        # passphrase-less). Subkey-Type: RSA yields the encryption subkey pass needs.
        gen = runner(["gpg", "--batch", "--gen-key"], env, _GPG_GEN_PARAMS)
        if gen.returncode != 0:
            return PassResult(
                ready=False,
                warnings=[f"gpg key generation failed: {gen.stderr.strip() or gen.returncode}"],
            )
        actions.append("generated a protonfs GPG key (passphrase-less)")
        fpr = _gpg_fingerprint(env, runner)
    if fpr is None:
        return PassResult(ready=False, warnings=["could not read the generated GPG key"])

    init = runner(["pass", "init", fpr], env)
    if init.returncode != 0 or not pass_store_initialized():
        return PassResult(
            ready=False,
            warnings=[f"`pass init` failed: {init.stderr.strip() or init.returncode}"],
        )
    actions.append(f"initialized the pass store at {password_store_dir()}")
    return PassResult(ready=True, actions=actions)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_credstore.py -q`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add src/protonfs/credstore.py tests/test_credstore.py
git commit -m "feat(credstore): pass-store bootstrap (keygen + init) and env injection"
```

---

### Task 3: Hot-path store selection — `drive_env()` (read-only, never keygen)

**Files:**
- Modify: `src/protonfs/credstore.py`
- Test: `tests/test_credstore.py`

**Interfaces:**
- Consumes: `read_store_choice()`, `pass_env()`, `secretservice.drive_env`, `secretservice.is_linux`, `secretservice.DISABLE_ENV`.
- Produces: `drive_env(env: dict[str, str] | None = None) -> dict[str, str]` — the environment every proton-drive subprocess inherits. Selection only; no establishment.

Selection logic (first match wins):
1. `STORE_ENV` already set in the incoming env → respect it; if it's `pass`, fill managed dirs via `pass_env`; if `keychain`, delegate to `secretservice.drive_env`. Never persist.
2. `PROTONFS_STORE_ENV` == `pass` → `pass_env` (no keygen here); `== keychain` → `secretservice.drive_env`.
3. sticky `read_store_choice()` == `pass` → `pass_env`; `== keychain` → `secretservice.drive_env`.
4. otherwise (`auto`/unset) → `secretservice.drive_env` (existing behavior; proton-drive defaults to keychain). No `pass` in the hot path without an established sticky choice.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_credstore.py
def test_drive_env_pass_when_sticky_pass(home, monkeypatch):
    monkeypatch.setattr(cs, "read_store_choice", lambda: cs.PASS)
    out = cs.drive_env({"PATH": "/usr/bin"})
    assert out[cs.STORE_ENV] == "pass"
    assert out["PASSWORD_STORE_DIR"] == str(cs.password_store_dir())


def test_drive_env_delegates_to_secretservice_for_keychain(home, monkeypatch):
    called = {}
    monkeypatch.setattr(cs, "read_store_choice", lambda: cs.KEYCHAIN)
    monkeypatch.setattr(cs.secretservice, "drive_env", lambda e=None: {"KEYCHAIN": "1"})
    out = cs.drive_env({"PATH": "/usr/bin"})
    assert out == {"KEYCHAIN": "1"}


def test_drive_env_respects_preset_native_var(home, monkeypatch):
    # user set PROTON_DRIVE_CREDENTIALS_STORE=pass themselves -> honor, fill dirs
    out = cs.drive_env({"PATH": "/x", cs.STORE_ENV: "pass"})
    assert out[cs.STORE_ENV] == "pass"
    assert out["GNUPGHOME"] == str(cs.gnupg_home())


def test_drive_env_auto_falls_back_to_secretservice(home, monkeypatch):
    monkeypatch.setattr(cs, "read_store_choice", lambda: None)
    monkeypatch.setattr(cs.secretservice, "drive_env", lambda e=None: {"AUTO": "1"})
    assert cs.drive_env({}) == {"AUTO": "1"}


def test_drive_env_protonfs_override_pass_beats_sticky(home, monkeypatch):
    # branch 2: PROTONFS_CREDENTIALS_STORE=pass wins even over a keychain sticky choice.
    monkeypatch.setattr(cs, "read_store_choice", lambda: cs.KEYCHAIN)
    out = cs.drive_env({"PATH": "/x", cs.PROTONFS_STORE_ENV: "pass"})
    assert out[cs.STORE_ENV] == "pass"
    assert out["PASSWORD_STORE_DIR"] == str(cs.password_store_dir())


def test_drive_env_preset_native_keychain_delegates(home, monkeypatch):
    # branch 1 (keychain leg): a preset native var wins over everything and delegates.
    monkeypatch.setattr(cs, "read_store_choice", lambda: cs.PASS)
    monkeypatch.setattr(cs.secretservice, "drive_env", lambda e=None: {"KC": "1"})
    out = cs.drive_env({"PATH": "/x", cs.STORE_ENV: "keychain"})
    assert out == {"KC": "1"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_credstore.py -q`
Expected: FAIL — `AttributeError: module 'protonfs.credstore' has no attribute 'drive_env'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/protonfs/credstore.py  (add `from protonfs import secretservice` at top)
def drive_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """The environment every proton-drive subprocess inherits (selection only).

    Reads the sticky/override store choice and injects the right env vars. Never
    generates keys or raises -- establishment (which may keygen) is done by
    :func:`establish` at auth-login / doctor --fix time.
    """
    base = dict(os.environ if env is None else env)

    preset = base.get(STORE_ENV)
    if preset == PASS:
        return pass_env(base)
    if preset == KEYCHAIN:
        return secretservice.drive_env(base)

    override = base.get(PROTONFS_STORE_ENV, AUTO).strip().lower()
    if override == PASS:
        return pass_env(base)
    if override == KEYCHAIN:
        return secretservice.drive_env(base)

    choice = read_store_choice()
    if choice == PASS:
        return pass_env(base)
    # keychain sticky, or auto/unset: existing Secret Service behavior.
    return secretservice.drive_env(base)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_credstore.py -q`
Expected: PASS (10 passed)

- [ ] **Step 5: Commit**

```bash
git add src/protonfs/credstore.py tests/test_credstore.py
git commit -m "feat(credstore): read-only hot-path store selection in drive_env"
```

---

### Task 4: `establish()` — auto-resolution with keygen, persisted sticky

**Files:**
- Modify: `src/protonfs/credstore.py`
- Test: `tests/test_credstore.py`

**Interfaces:**
- Consumes: `secretservice.ensure_secret_service`, `secretservice.secret_service_state`, `secretservice.is_linux`, `secretservice.DISABLE_ENV`, `ensure_pass_store`, `pass_env`, `write_store_choice`.
- Produces: `class EstablishResult` `(env: dict, store: str | None, actions: list[str], warnings: list[str])`; `establish(env=None, *, notify=None, runner=_run, ss_runner=None) -> EstablishResult`.
  - `notify` is an optional `Callable[[str], None]` for the one-line user notice (Click passes `click.echo`).
  - Persists the resolved store via `write_store_choice`, except when honoring a pre-set native `STORE_ENV` (that is the user's own config, not ours to persist).

Resolution: respect `STORE_ENV`/`PROTONFS_STORE_ENV`/sticky as in `drive_env`; only when `auto` and unset does it actively try keychain-then-pass:
- non-Linux → `keychain` (persist), env unchanged.
- `DISABLE_ENV` set → return env unchanged, store `None`, no persist.
- Linux auto: run `ensure_secret_service`; if `secret_service_state(env) == "ready"` → `keychain` (persist). Else `ensure_pass_store`; if ready → notify once, `pass` (persist, env via `pass_env`). Else → store `None`, warnings surfaced, no persist (proton-drive will emit its own keychain error — today's behavior).

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_credstore.py
# NOTE: the repo's autouse `no_keyring_bootstrap` fixture (tests/conftest.py) sets
# PROTONFS_NO_KEYRING_BOOTSTRAP in os.environ. `establish()` reads DISABLE_ENV from
# os.environ (consistent with secretservice.ensure_secret_service), so each auto-path
# test below must clear it: add `monkeypatch.delenv(cs.secretservice.DISABLE_ENV,
# raising=False)` as the first line (the test_secretservice.py idiom). Do NOT stub
# read_store_choice here — these tests assert real persistence via the tmp state dir.
# Also add a test for the DISABLE_ENV branch itself (set the var, assert store is None,
# nothing persisted, and ensure_secret_service is never called).
def test_establish_prefers_keychain_when_secret_service_ready(home, monkeypatch):
    monkeypatch.delenv(cs.secretservice.DISABLE_ENV, raising=False)
    monkeypatch.setattr(cs.secretservice, "is_linux", lambda: True)
    monkeypatch.setattr(
        cs.secretservice, "ensure_secret_service",
        lambda e=None, runner=None: cs.secretservice.SecretsResult(env=dict(e or {}), ready=True),
    )
    monkeypatch.setattr(cs.secretservice, "secret_service_state", lambda e, runner=None: "ready")
    result = cs.establish({"PATH": "/x"})
    assert result.store == "keychain"
    assert cs.read_store_choice() == "keychain"


def test_establish_falls_back_to_pass_and_persists(home, monkeypatch):
    notes = []
    monkeypatch.setattr(cs, "read_store_choice", lambda: None)
    monkeypatch.setattr(cs.secretservice, "is_linux", lambda: True)
    monkeypatch.setattr(
        cs.secretservice, "ensure_secret_service",
        lambda e=None, runner=None: cs.secretservice.SecretsResult(env=dict(e or {}), ready=True),
    )
    monkeypatch.setattr(cs.secretservice, "secret_service_state", lambda e, runner=None: "locked")
    monkeypatch.setattr(cs, "ensure_pass_store", lambda runner=None: cs.PassResult(ready=True, actions=["init"]))
    result = cs.establish({"PATH": "/x"}, notify=notes.append)
    assert result.store == "pass"
    assert result.env[cs.STORE_ENV] == "pass"
    assert cs.read_store_choice() == "pass"
    assert notes and "pass" in notes[0].lower()


def test_establish_no_store_when_neither_available(home, monkeypatch):
    monkeypatch.setattr(cs, "read_store_choice", lambda: None)
    monkeypatch.setattr(cs.secretservice, "is_linux", lambda: True)
    monkeypatch.setattr(
        cs.secretservice, "ensure_secret_service",
        lambda e=None, runner=None: cs.secretservice.SecretsResult(env=dict(e or {}), ready=True),
    )
    monkeypatch.setattr(cs.secretservice, "secret_service_state", lambda e, runner=None: "missing")
    monkeypatch.setattr(cs, "ensure_pass_store", lambda runner=None: cs.PassResult(ready=False, warnings=["no pass"]))
    result = cs.establish({"PATH": "/x"})
    assert result.store is None
    assert cs.read_store_choice() is None  # nothing persisted on total failure
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_credstore.py -q`
Expected: FAIL — `AttributeError: module 'protonfs.credstore' has no attribute 'establish'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/protonfs/credstore.py
from collections.abc import Callable


@dataclass
class EstablishResult:
    """Outcome of :func:`establish` — the env to use plus the chosen store."""

    env: dict[str, str]
    store: str | None
    actions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def establish(
    env: dict[str, str] | None = None,
    *,
    notify: Callable[[str], None] | None = None,
    runner=_run,
) -> EstablishResult:
    """Resolve and, if needed, bootstrap the credentials store (may generate a GPG key).

    Called at establishment moments (auth login, doctor --fix), never on the hot path.
    Persists the resolved store so :func:`drive_env` reads it thereafter.
    """
    base = dict(os.environ if env is None else env)

    preset = base.get(STORE_ENV)
    if preset in _VALID_STORES:
        # User configured proton-drive directly; honor without persisting our own choice.
        chosen_env = pass_env(base) if preset == PASS else secretservice.drive_env(base)
        return EstablishResult(env=chosen_env, store=preset)

    override = base.get(PROTONFS_STORE_ENV, AUTO).strip().lower()
    sticky = read_store_choice()
    forced = override if override in _VALID_STORES else sticky

    if forced == PASS:
        result = ensure_pass_store(runner=runner)
        if result.ready:
            write_store_choice(PASS)
            return EstablishResult(pass_env(base), PASS, result.actions, result.warnings)
        return EstablishResult(base, None, result.actions, result.warnings)
    if forced == KEYCHAIN:
        ss_res = secretservice.ensure_secret_service(base)
        write_store_choice(KEYCHAIN)
        return EstablishResult(ss_res.env, KEYCHAIN, ss_res.actions, ss_res.warnings)

    # auto + unset:
    if not secretservice.is_linux():
        write_store_choice(KEYCHAIN)
        return EstablishResult(base, KEYCHAIN)
    if os.environ.get(secretservice.DISABLE_ENV):
        return EstablishResult(base, None)

    ss_res = secretservice.ensure_secret_service(base)
    actions = list(ss_res.actions)
    if secretservice.secret_service_state(ss_res.env) == "ready":
        write_store_choice(KEYCHAIN)
        return EstablishResult(ss_res.env, KEYCHAIN, actions, ss_res.warnings)

    pass_res = ensure_pass_store(runner=runner)
    if pass_res.ready:
        if notify is not None:
            notify("no OS keyring available; using a protonfs-managed pass store")
        write_store_choice(PASS)
        return EstablishResult(
            pass_env(base), PASS, actions + pass_res.actions, ss_res.warnings + pass_res.warnings
        )
    return EstablishResult(
        base, None, actions + pass_res.actions, ss_res.warnings + pass_res.warnings
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_credstore.py -q`
Expected: PASS (13 passed)

- [ ] **Step 5: Commit**

```bash
git add src/protonfs/credstore.py tests/test_credstore.py
git commit -m "feat(credstore): establish() auto-resolves keychain-then-pass, persists choice"
```

---

### Task 5: Wire `DriveClient` and `auth login` to the resolver

**Files:**
- Modify: `src/protonfs/drive.py:404-408` (the `_drive_env` method — swap the import)
- Modify: `src/protonfs/commands/auth.py:46-62` (`auth_passthrough` — establish on login)
- Test: `tests/test_drive.py`, `tests/commands/test_auth.py`

**Interfaces:**
- Consumes: `credstore.drive_env`, `credstore.establish`.
- Produces: no new API; behavior change only.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_drive.py
def test_driveclient_env_uses_credstore(monkeypatch):
    from protonfs import credstore
    monkeypatch.setattr(credstore, "drive_env", lambda e=None: {"FROM_CREDSTORE": "1"})
    client = __import__("protonfs.drive", fromlist=["DriveClient"]).DriveClient(binary="/x")
    assert client._drive_env() == {"FROM_CREDSTORE": "1"}
```

```python
# add to tests/commands/test_auth.py (mirror its existing style)
def test_auth_login_establishes_store(monkeypatch):
    from protonfs.commands import auth
    from protonfs import credstore

    seen = {}
    monkeypatch.setattr(
        credstore, "establish",
        lambda env=None, notify=None, runner=None: credstore.EstablishResult(
            env={"ESTABLISHED": "1"}, store="pass"
        ),
    )
    monkeypatch.setattr(auth.shutil, "which", lambda b: "/usr/bin/proton-drive")

    def fake_runner(cmd, env):
        seen["env"] = env
        class R: returncode = 0
        return R()

    auth.auth_passthrough("login", binary="proton-drive", runner=fake_runner)
    assert seen["env"] == {"ESTABLISHED": "1"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_drive.py::test_driveclient_env_uses_credstore -q`
Expected: FAIL — `_drive_env` still returns the secretservice env (assert mismatch).

- [ ] **Step 3: Write minimal implementation**

In `src/protonfs/drive.py`, change `_drive_env`:

```python
    def _drive_env(self) -> dict[str, str]:
        """Environment for proton-drive, with the credentials store resolved on first use."""
        from protonfs.credstore import drive_env

        if self._env is None:
            self._env = drive_env()
        return self._env
```

In `src/protonfs/commands/auth.py`, `auth_passthrough` — establish (with a notice) for `login`, plain `drive_env` otherwise:

```python
    result_env = None
    if subcommand == "login":
        from protonfs.credstore import establish

        established = establish(notify=click.echo)
        result_env = established.env
        for action in established.actions:
            click.echo(f"  {action}")
        for warning in established.warnings:
            click.echo(f"  ! {warning}")
    if result_env is None:
        result_env = drive_env()
    result = runner([bin_path, "auth", subcommand], env=result_env)
    return result.returncode
```

Add `import click` to `auth.py` imports.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_drive.py tests/commands -q -k "auth or credstore or env"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/protonfs/drive.py src/protonfs/commands/auth.py tests/test_drive.py tests/commands
git commit -m "feat(auth): establish credentials store on login; DriveClient uses credstore"
```

---

### Task 6: `doctor` — credentials-store check + pass probe, conditional Secret Service checks

**Files:**
- Modify: `src/protonfs/commands/doctor.py:214-332` (`run_doctor`)
- Test: `tests/commands/test_doctor.py`

**Interfaces:**
- Consumes: `credstore.read_store_choice`, `credstore.PROTONFS_STORE_ENV`, `credstore.STORE_ENV`, `credstore.pass_tools_present`, `credstore.pass_store_initialized`, `credstore.password_store_dir`, `credstore.gnupg_home`, `credstore.establish`, `credstore.probe_pass_store` (new, below).
- Produces (in `credstore.py`): `active_store(env=None) -> tuple[str, str]` returning `(store, how)` where `how` ∈ {`native-env`,`protonfs-env`,`sticky`,`auto-default`}; `probe_pass_store(runner=_run) -> tuple[bool, str]` — insert/show/rm a throwaway entry.

Behavior: a new `credentials store` check reports `active_store`. When the active store is `pass`, run the pass checks (tools present, initialized, round-trip probe) and **skip** the Secret Service checks. When it is `keychain`/auto, keep the existing Secret Service checks unchanged.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/commands/test_doctor.py
def test_doctor_reports_pass_store_when_active(monkeypatch, tmp_path):
    from protonfs.commands import doctor
    from protonfs import credstore, secretservice
    monkeypatch.setattr(secretservice.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(credstore, "active_store", lambda env=None: ("pass", "sticky"))
    monkeypatch.setattr(credstore, "pass_tools_present", lambda: True)
    monkeypatch.setattr(credstore, "pass_store_initialized", lambda: True)
    monkeypatch.setattr(credstore, "probe_pass_store", lambda runner=None: (True, "round-trip ok"))
    checks = doctor.run_doctor(fix=False, root=tmp_path)
    names = [c.name for c in checks]
    assert "credentials store" in names
    assert not any(c.name == "secret service" for c in checks)  # suppressed for pass
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/commands/test_doctor.py -q -k pass_store`
Expected: FAIL — `AttributeError: 'credstore' has no attribute 'active_store'` (or "credentials store" not in names).

- [ ] **Step 3: Write minimal implementation**

Add to `credstore.py` (and add DIRECT unit tests in `tests/test_credstore.py`: `active_store`'s
four precedence branches — native-env / protonfs-env / sticky / auto-default — and
`probe_pass_store`'s missing-tools, success round-trip, insert-failure, and readback-mismatch
paths with a scripted fake runner; the doctor tests mock these two functions, so they need their
own coverage):

```python
def active_store(env: dict[str, str] | None = None) -> tuple[str, str]:
    """The store that would be used now and why, without bootstrapping anything."""
    base = dict(os.environ if env is None else env)
    if base.get(STORE_ENV) in _VALID_STORES:
        return base[STORE_ENV], "native-env"
    override = base.get(PROTONFS_STORE_ENV, AUTO).strip().lower()
    if override in _VALID_STORES:
        return override, "protonfs-env"
    sticky = read_store_choice()
    if sticky is not None:
        return sticky, "sticky"
    return KEYCHAIN, "auto-default"


def probe_pass_store(runner=_run) -> tuple[bool, str]:
    """Insert, read back and remove a throwaway pass entry. Returns (ok, detail)."""
    if not pass_tools_present():
        return False, "pass/gpg not installed"
    env = _managed_env()
    entry = "ch.proton.drive/drive-sdk-cli/protonfs-selftest"
    try:
        ins = runner(["pass", "insert", "-f", "-m", entry], env, "protonfs-selftest")
        if ins.returncode != 0:
            return False, ins.stderr.strip() or "pass insert failed"
        show = runner(["pass", "show", entry], env)
        if show.returncode != 0 or show.stdout.strip() != "protonfs-selftest":
            return False, show.stderr.strip() or "stored secret did not read back"
        runner(["pass", "rm", "-f", entry], env)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    return True, "inserted, read back and removed a test entry"
```

In `doctor.py` `run_doctor`, right before the `is_linux()` block, add the store report; and gate the Secret Service section on `store == keychain`:

```python
    from protonfs import credstore

    store, how = credstore.active_store()
    checks.append(Check("credentials store", True, f"{store} ({how})"))

    if store == credstore.PASS:
        checks.append(Check(
            "tool: pass/gpg",
            ok=credstore.pass_tools_present(),
            detail="present" if credstore.pass_tools_present() else "pass/gpg not installed",
            hint=None if credstore.pass_tools_present() else "Install `pass` and `gnupg2`.",
        ))
        init = credstore.pass_store_initialized()
        checks.append(Check(
            "pass store",
            ok=init,
            detail=str(credstore.password_store_dir()) if init else "not initialized",
            hint=None if init else "Run `protonfs doctor --fix` (or `protonfs auth login`).",
        ))
        ok, detail = credstore.probe_pass_store()
        checks.append(Check("pass read/write", ok=ok, detail=detail,
                            hint=None if ok else "Run `protonfs doctor --fix`."))
        return checks
```

(The existing Secret Service block below now runs only for the `keychain`/auto path, since the `pass` branch returns early.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/commands/test_doctor.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/protonfs/credstore.py src/protonfs/commands/doctor.py tests/commands
git commit -m "feat(doctor): credentials-store check + pass probe; gate Secret Service checks"
```

---

### Task 7: Docs, CHANGELOG, version directives, full-suite gate

**Files:**
- Modify: `docs/conf.py` — an uncommitted working change already adds `sphinx.ext.extlinks`
  and an `extlinks` map with `repo`/`issue`/`version` roles; **commit it as part of this task**
  (it is a prerequisite for the `:issue:`/`:version:` roles used below).
- Modify: `docs/getting-started/guide.rst` or the headless section (add a "credentials store / pass fallback" subsection)
- Modify: the environment-variable reference doc (add `PROTONFS_CREDENTIALS_STORE`; note `PROTON_DRIVE_CREDENTIALS_STORE` passthrough)
- Modify: `CHANGELOG.md` (Unreleased → Features)
- Verify: `.. versionadded::` directives present on new public `credstore` API

**Note on extlinks (already added, uncommitted):**
```python
# docs/conf.py — the roles now available for the docs prose below
extlinks={
    "repo": ('https://github.com/will-roscoe/protonfs',),
    "issue":('https://github.com/will-roscoe/protonfs/issues/%s', 'issue %s'),
    "version":('https://github.com/will-roscoe/protonfs/releases/tag/v%s', 'v%s')
}
```
Use `:issue:`` `NN`` `` and `:version:`` `X.Y.Z`` `` in the new docs where a change references an
issue or release. **Build-verification checkpoint:** `sphinx.ext.extlinks` requires each map
value to be a 2-tuple `(base_url, caption)`. The `repo` entry above is a 1-tuple and will fail
the strict (`-W`) build; if Step 4 errors on it, fix it to `"repo": ('https://github.com/will-roscoe/protonfs', None)`
(preserving the author's intent — a caption-less `:repo:` role) rather than removing it.

- [ ] **Step 1: Add the CHANGELOG entry**

```markdown
### Features

- **credentials store**: on a headless Linux host where the freedesktop Secret Service
  cannot be made ready, protonfs now falls back automatically to a protonfs-managed
  `pass` ([password-store](https://www.passwordstore.org/)) credentials store — generating
  a passphrase-less GPG key and initializing the store on first `auth login` — instead of
  failing to persist the proton-drive session. The choice is sticky per host. Force it with
  `PROTONFS_CREDENTIALS_STORE=keychain|pass`; `protonfs doctor` reports the active store.
  Requires proton-drive >= 0.6.0.
```

- [ ] **Step 2: Write the docs subsection**

Document: the fallback trigger, that a passphrase-less GPG key is generated (same posture as the keyring password), the `PROTONFS_CREDENTIALS_STORE` override, `PROTON_DRIVE_CREDENTIALS_STORE` passthrough precedence, and that `pass`+`gnupg2` must be installed (EPEL on CentOS 7).

- [ ] **Step 3: Verify version directives**

Run: `grep -n "versionadded" src/protonfs/credstore.py`
Expected: the module docstring carries `.. versionadded:: <next-release>` (replace `<next-release>` with the actual version the auto-release pipeline will cut).

- [ ] **Step 4: Full suite + lint**

Run: `python -m pytest -q -k "not live" --ignore=tests/test_live_integration.py --ignore=tests/test_live_workflows.py && ruff check src/protonfs/credstore.py`
Expected: all pass; ruff clean.

- [ ] **Step 5: Commit**

```bash
git add CHANGELOG.md docs/conf.py docs src/protonfs/credstore.py
git commit -m "docs(credstore): document pass fallback + PROTONFS_CREDENTIALS_STORE; enable extlinks roles"
```

---

## Post-implementation: exo2 live validation (manual, not a task gate)

Per the spec's validation checkpoint — run on exo2 (headless CentOS 7):
1. Ensure `pass` + `gnupg2` installed (EPEL for `pass`).
2. Fresh state (`rm -rf ~/.local/share/protonfs/{gnupg,password-store,credentials-store}`), no Secret Service.
3. `protonfs auth login` → confirm the notice prints, login completes, and the session persists.
4. Confirm proton-drive honors the inherited `PASSWORD_STORE_DIR`/`GNUPGHOME` (session lands under `~/.local/share/protonfs/password-store`, not `~/.password-store`).
5. `protonfs auth status` and a `filesystem list /`-backed command reuse the sticky `pass` store with no re-login.
6. `protonfs doctor` reports `credentials store: pass (sticky)` and a green pass probe.
7. Regression: on a host with a working Secret Service, confirm `keychain` is still selected.
```
