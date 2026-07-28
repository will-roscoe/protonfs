from __future__ import annotations

from dataclasses import dataclass

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
            if "--quick-generate-key" in cmd or "--generate-key" in cmd or "--gen-key" in cmd:
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
    # a second call is a no-op (store already initialized): it returns before touching
    # the runner at all, so no gpg/pass command is issued.
    fake2 = FakePass(has_key=True)
    result2 = cs.ensure_pass_store(runner=fake2)
    assert result2.ready is True
    assert fake2.calls == []


def test_ensure_pass_store_missing_tools_warns(home, monkeypatch):
    monkeypatch.setattr(cs.shutil, "which", lambda tool: None)
    result = cs.ensure_pass_store(runner=FakePass())
    assert result.ready is False
    assert any("pass" in w or "gpg" in w for w in result.warnings)


class _TimeoutOnKeygenPass:
    """A runner whose keygen call hangs (TimeoutExpired) -- Fix 4 regression."""

    def __call__(self, cmd, env, stdin=None):
        if cmd[0] == "gpg":
            if "--list-secret-keys" in cmd:
                return FakeCompleted(returncode=2, stderr="no secret keys")
            if "--quick-generate-key" in cmd:
                import subprocess

                raise subprocess.TimeoutExpired(cmd="gpg", timeout=30)
        return FakeCompleted()


def test_ensure_pass_store_degrades_on_gpg_timeout(home, monkeypatch):
    monkeypatch.setattr(cs.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    result = cs.ensure_pass_store(runner=_TimeoutOnKeygenPass())
    assert result.ready is False
    assert result.warnings


def test_drive_env_pass_when_sticky_pass(home, monkeypatch):
    monkeypatch.setattr(cs, "read_store_choice", lambda: cs.PASS)
    out = cs.drive_env({"PATH": "/usr/bin"})
    assert out[cs.STORE_ENV] == "pass"
    assert out["PASSWORD_STORE_DIR"] == str(cs.password_store_dir())


def test_drive_env_delegates_to_secretservice_for_keychain(home, monkeypatch):
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


def test_establish_prefers_keychain_when_secret_service_ready(home, monkeypatch):
    # The autouse no_keyring_bootstrap fixture sets DISABLE_ENV; clear it so this
    # test exercises the real auto-resolution path (matches test_secretservice.py).
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
    monkeypatch.delenv(cs.secretservice.DISABLE_ENV, raising=False)
    monkeypatch.setattr(cs.secretservice, "is_linux", lambda: True)
    monkeypatch.setattr(
        cs.secretservice, "ensure_secret_service",
        lambda e=None, runner=None: cs.secretservice.SecretsResult(env=dict(e or {}), ready=True),
    )
    monkeypatch.setattr(cs.secretservice, "secret_service_state", lambda e, runner=None: "locked")
    monkeypatch.setattr(
        cs, "ensure_pass_store",
        lambda runner=None: cs.PassResult(ready=True, actions=["init"]),
    )
    result = cs.establish({"PATH": "/x"}, notify=notes.append)
    assert result.store == "pass"
    assert result.env[cs.STORE_ENV] == "pass"
    assert cs.read_store_choice() == "pass"
    assert notes and "pass" in notes[0].lower()


def test_establish_no_store_when_neither_available(home, monkeypatch):
    monkeypatch.delenv(cs.secretservice.DISABLE_ENV, raising=False)
    monkeypatch.setattr(cs.secretservice, "is_linux", lambda: True)
    monkeypatch.setattr(
        cs.secretservice, "ensure_secret_service",
        lambda e=None, runner=None: cs.secretservice.SecretsResult(env=dict(e or {}), ready=True),
    )
    monkeypatch.setattr(cs.secretservice, "secret_service_state", lambda e, runner=None: "missing")
    monkeypatch.setattr(
        cs, "ensure_pass_store",
        lambda runner=None: cs.PassResult(ready=False, warnings=["no pass"]),
    )
    result = cs.establish({"PATH": "/x"})
    assert result.store is None
    assert cs.read_store_choice() is None  # nothing persisted on total failure


def test_establish_preserves_launched_bus_env_on_pass_fallback_failure(home, monkeypatch):
    # Fix 1 regression: on a host where secret_service_state comes back "unknown"
    # (e.g. gdbus missing) and pass is also unavailable, the bus that
    # ensure_secret_service just launched must still be carried into the returned
    # env -- not discarded in favor of the pristine `base`.
    monkeypatch.delenv(cs.secretservice.DISABLE_ENV, raising=False)
    monkeypatch.setattr(cs.secretservice, "is_linux", lambda: True)
    monkeypatch.setattr(
        cs.secretservice, "ensure_secret_service",
        lambda e=None, runner=None: cs.secretservice.SecretsResult(
            env={**(e or {}), "DBUS_SESSION_BUS_ADDRESS": "unix:abstract=xyz"}, ready=False,
        ),
    )
    monkeypatch.setattr(cs.secretservice, "secret_service_state", lambda e, runner=None: "unknown")
    monkeypatch.setattr(
        cs, "ensure_pass_store",
        lambda runner=None: cs.PassResult(ready=False, warnings=["no pass"]),
    )
    result = cs.establish({"PATH": "/x"})
    assert result.env["DBUS_SESSION_BUS_ADDRESS"] == "unix:abstract=xyz"
    assert result.store is None


def test_establish_disable_env_returns_no_store(home, monkeypatch):
    # PROTONFS_NO_KEYRING_BOOTSTRAP set (read from os.environ, like ensure_secret_service):
    # establish must resolve no store and persist nothing, and must not touch the keyring.
    monkeypatch.setenv(cs.secretservice.DISABLE_ENV, "1")
    monkeypatch.setattr(cs.secretservice, "is_linux", lambda: True)

    def _boom(*a, **k):  # establish must not bootstrap anything when disabled
        raise AssertionError("must not call ensure_secret_service when DISABLE_ENV is set")

    monkeypatch.setattr(cs.secretservice, "ensure_secret_service", _boom)
    result = cs.establish({"PATH": "/x"})
    assert result.store is None
    assert cs.read_store_choice() is None


def test_active_store_native_env_wins(home):
    assert cs.active_store({cs.STORE_ENV: "pass"}) == ("pass", "native-env")


def test_active_store_protonfs_env_over_sticky(home, monkeypatch):
    monkeypatch.setattr(cs, "read_store_choice", lambda: cs.KEYCHAIN)
    assert cs.active_store({cs.PROTONFS_STORE_ENV: "PASS"}) == ("pass", "protonfs-env")


def test_active_store_sticky_when_no_env(home, monkeypatch):
    monkeypatch.setattr(cs, "read_store_choice", lambda: cs.PASS)
    assert cs.active_store({"PATH": "/x"}) == ("pass", "sticky")


def test_active_store_auto_default_when_nothing_set(home, monkeypatch):
    monkeypatch.setattr(cs, "read_store_choice", lambda: None)
    assert cs.active_store({"PATH": "/x"}) == ("keychain", "auto-default")


class _ScriptedPass:
    """A pass runner scripted per command name for probe_pass_store tests."""

    def __init__(self, *, insert=0, show_rc=0, show_out="protonfs-selftest"):
        self.insert = insert
        self.show_rc = show_rc
        self.show_out = show_out
        self.calls: list[list[str]] = []

    def __call__(self, cmd, env, stdin=None):
        self.calls.append(cmd)
        if cmd[1] == "insert":
            return FakeCompleted(returncode=self.insert, stderr="insert boom")
        if cmd[1] == "show":
            return FakeCompleted(returncode=self.show_rc, stdout=self.show_out, stderr="show boom")
        return FakeCompleted()


def test_probe_pass_store_missing_tools(home, monkeypatch):
    monkeypatch.setattr(cs, "pass_tools_present", lambda: False)
    ok, detail = cs.probe_pass_store(runner=_ScriptedPass())
    assert ok is False
    assert "not installed" in detail


def test_probe_pass_store_round_trip_ok(home, monkeypatch):
    monkeypatch.setattr(cs, "pass_tools_present", lambda: True)
    fake = _ScriptedPass()
    ok, detail = cs.probe_pass_store(runner=fake)
    assert ok is True
    # inserted, read back, then removed -> the rm call must have happened.
    assert any(c[1] == "rm" for c in fake.calls)


def test_probe_pass_store_insert_failure(home, monkeypatch):
    monkeypatch.setattr(cs, "pass_tools_present", lambda: True)
    ok, detail = cs.probe_pass_store(runner=_ScriptedPass(insert=1))
    assert ok is False
    assert detail == "insert boom"


def test_probe_pass_store_readback_mismatch(home, monkeypatch):
    monkeypatch.setattr(cs, "pass_tools_present", lambda: True)
    ok, detail = cs.probe_pass_store(runner=_ScriptedPass(show_out="wrong"))
    assert ok is False
    assert "did not read back" in detail or detail == "show boom"
