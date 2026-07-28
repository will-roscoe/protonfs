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
