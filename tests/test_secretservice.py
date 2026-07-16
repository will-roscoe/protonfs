from __future__ import annotations

import subprocess
from dataclasses import dataclass

import pytest

from protonfs import secretservice as ss

BUS = "unix:abstract=/tmp/dbus-test"


@dataclass
class FakeCompleted:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class FakeRunner:
    """Stands in for subprocess.run, scripted by command name.

    The real thing needs a live session bus and would leave daemons behind, so the
    D-Bus conversation is faked -- but faked at the exact shape of gdbus's output,
    since parsing that output is itself one of the things under test.
    """

    def __init__(self, *, has_secrets=True, locked=False, bus_alive=True):
        self.has_secrets = has_secrets
        self.locked = locked
        self.bus_alive = bus_alive
        self.calls: list[list[str]] = []

    def __call__(self, cmd, env, stdin=None):
        self.calls.append(cmd)
        if cmd[0] == "dbus-launch":
            return FakeCompleted(stdout=f"{ss.BUS_ENV}='{BUS}';\nexport {ss.BUS_ENV};\n")
        if cmd[0] == "gnome-keyring-daemon":
            self.has_secrets = True
            self.locked = False
            return FakeCompleted(stdout="GNOME_KEYRING_CONTROL=/run/user/1/keyring\n")
        if cmd[0] == "gdbus":
            method = cmd[cmd.index("--method") + 1]
            if method.endswith("ListNames"):
                if not self.bus_alive:
                    return FakeCompleted(returncode=1, stderr="no bus")
                names = "'org.freedesktop.secrets'," if self.has_secrets else ""
                return FakeCompleted(stdout=f"([{names}'org.freedesktop.DBus'],)")
            if method.endswith("ReadAlias"):
                return FakeCompleted(
                    stdout="(objectpath '/org/freedesktop/secrets/collection/login',)"
                )
            if method.endswith("Properties.Get"):
                return FakeCompleted(stdout=f"(<{str(self.locked).lower()}>,)")
        if cmd[0] == "secret-tool":
            if self.locked:
                return FakeCompleted(returncode=1, stderr="Cannot create an item in a locked")
            return FakeCompleted(stdout="protonfs-selftest\n" if cmd[1] == "lookup" else "")
        return FakeCompleted()


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(ss.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    (tmp_path / "run").mkdir()
    monkeypatch.delenv(ss.BUS_ENV, raising=False)
    monkeypatch.delenv(ss.DISABLE_ENV, raising=False)
    monkeypatch.delenv(ss.KEYRING_PASSWORD_ENV, raising=False)
    monkeypatch.setattr(ss.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    # This whole module tests the Linux Secret Service bootstrap through a fully
    # mocked runner (FakeRunner) -- no real dbus/gnome-keyring process ever runs.
    # Force is_linux() to True so that logic is exercised deterministically on any
    # CI host OS (e.g. the macOS runners added in #72), not just on real Linux.
    # test_ensure_is_a_noop_off_linux overrides this back to "Darwin" explicitly.
    monkeypatch.setattr(ss._platform, "system", lambda: "Linux")


def test_parse_sh_exports_handles_dbus_launch_syntax():
    parsed = ss._parse_sh_exports(f"{ss.BUS_ENV}='{BUS}';\nexport {ss.BUS_ENV};\nDBUS_PID=12;\n")
    assert parsed[ss.BUS_ENV] == BUS
    assert parsed["DBUS_PID"] == "12"


def test_resolve_bus_launches_and_caches_when_none_present():
    runner = FakeRunner()
    address, how = ss.resolve_bus({}, runner)
    assert (address, how) == (BUS, "launched")
    assert ss.bus_cache_file().read_text() == BUS

    # A second process must adopt the cached bus, not launch a second daemon.
    runner2 = FakeRunner()
    address, how = ss.resolve_bus({}, runner2)
    assert (address, how) == (BUS, "cached")
    assert not any(cmd[0] == "dbus-launch" for cmd in runner2.calls)


def test_resolve_bus_ignores_a_cached_address_whose_daemon_is_gone():
    ss.bus_cache_file().parent.mkdir(parents=True, exist_ok=True)
    ss.bus_cache_file().write_text("unix:abstract=/tmp/dbus-dead")

    class DeadThenAlive(FakeRunner):
        def __call__(self, cmd, env, stdin=None):
            if cmd[0] == "gdbus" and env.get(ss.BUS_ENV) == "unix:abstract=/tmp/dbus-dead":
                return FakeCompleted(returncode=1, stderr="Failed to connect")
            return super().__call__(cmd, env, stdin)

    address, how = ss.resolve_bus({}, DeadThenAlive())
    assert (address, how) == (BUS, "launched")


def test_launch_bus_raises_instructively_when_dbus_launch_missing(monkeypatch):
    monkeypatch.setattr(ss.shutil, "which", lambda tool: None)
    with pytest.raises(ss.SecretServiceError, match="dbus-launch"):
        ss.launch_bus({}, FakeRunner())


def test_secret_service_state_reports_missing_locked_and_ready():
    env = {ss.BUS_ENV: BUS}
    assert ss.secret_service_state(env, FakeRunner(has_secrets=False)) == "missing"
    assert ss.secret_service_state(env, FakeRunner(locked=True)) == "locked"
    assert ss.secret_service_state(env, FakeRunner()) == "ready"


def test_ensure_starts_keyring_when_secret_service_missing():
    runner = FakeRunner(has_secrets=False)
    result = ss.ensure_secret_service({}, runner)

    assert result.ready
    assert result.env[ss.BUS_ENV] == BUS
    keyring = [c for c in runner.calls if c[0] == "gnome-keyring-daemon"]
    assert keyring == [["gnome-keyring-daemon", "--unlock", "--components=secrets"]]


def test_ensure_replaces_the_daemon_holding_a_locked_collection():
    """The exo2 failure: a Secret Service *is* running, but its default collection is
    the sealed login.keyring. Starting another daemon is not enough -- we must take
    the bus name from the incumbent, or writes keep hitting the locked collection."""
    runner = FakeRunner(locked=True)
    result = ss.ensure_secret_service({}, runner)

    assert result.ready
    keyring = [c for c in runner.calls if c[0] == "gnome-keyring-daemon"]
    assert keyring == [
        ["gnome-keyring-daemon", "--replace", "--unlock", "--components=secrets"]
    ]


def test_ensure_is_a_noop_when_the_keyring_already_works():
    runner = FakeRunner()
    result = ss.ensure_secret_service({ss.BUS_ENV: BUS}, runner)

    assert result.ready and result.actions == []
    assert not any(cmd[0] in ("dbus-launch", "gnome-keyring-daemon") for cmd in runner.calls)


def test_ensure_respects_the_opt_out(monkeypatch):
    monkeypatch.setenv(ss.DISABLE_ENV, "1")
    runner = FakeRunner(has_secrets=False)
    result = ss.ensure_secret_service({}, runner)

    assert result.ready and runner.calls == []


def test_ensure_is_a_noop_off_linux(monkeypatch):
    monkeypatch.setattr(ss._platform, "system", lambda: "Darwin")
    runner = FakeRunner(has_secrets=False)

    assert ss.ensure_secret_service({}, runner).ready
    assert runner.calls == []


def test_keyring_password_is_generated_once_and_stored_0600():
    first = ss.keyring_password()
    assert first == ss.keyring_password()
    assert ss.keyring_password_file().stat().st_mode & 0o777 == 0o600


def test_keyring_password_prefers_the_env_override(monkeypatch):
    monkeypatch.setenv(ss.KEYRING_PASSWORD_ENV, "hunter2")
    assert ss.keyring_password() == "hunter2"
    assert not ss.keyring_password_file().exists()


def test_keyring_daemon_gets_our_own_xdg_data_home():
    """The crux of the fix: the daemon must look for keyrings in protonfs's directory,
    so it creates a fresh unlocked one instead of finding the sealed system keyring."""
    seen: dict[str, str] = {}

    def runner(cmd, env, stdin=None):
        if cmd[0] == "gnome-keyring-daemon":
            seen.update(env)
        return FakeRunner()(cmd, env, stdin)

    ss.start_keyring({ss.BUS_ENV: BUS}, replace=False, runner=runner)
    assert seen["XDG_DATA_HOME"] == str(ss.secrets_home())
    assert (ss.secrets_home() / "keyrings").is_dir()


def test_start_keyring_raises_instructively_when_daemon_missing(monkeypatch):
    monkeypatch.setattr(ss.shutil, "which", lambda tool: None)
    with pytest.raises(ss.SecretServiceError, match="gnome-keyring"):
        ss.start_keyring({}, replace=False, runner=FakeRunner())


def test_drive_env_never_raises_when_bootstrap_fails(monkeypatch):
    def boom(env=None, runner=None):
        raise ss.SecretServiceError("nope")

    monkeypatch.setattr(ss, "ensure_secret_service", boom)
    assert ss.drive_env({"PATH": "/bin"}) == {"PATH": "/bin"}


def test_probe_detects_a_locked_collection():
    ok, detail = ss.probe_secret_service({ss.BUS_ENV: BUS}, FakeRunner(locked=True))
    assert not ok
    assert "locked" in detail


def test_probe_round_trips_a_secret_when_healthy():
    ok, _ = ss.probe_secret_service({ss.BUS_ENV: BUS}, FakeRunner())
    assert ok


def test_probe_is_skipped_when_secret_tool_absent(monkeypatch):
    monkeypatch.setattr(ss.shutil, "which", lambda tool: None)
    ok, detail = ss.probe_secret_service({}, FakeRunner())
    assert ok and "skipped" in detail


def test_bus_timeout_does_not_hang_forever(monkeypatch):
    def timeout(cmd, env, stdin=None):
        raise subprocess.TimeoutExpired(cmd, ss._BUS_TIMEOUT)

    assert ss.bus_responds({ss.BUS_ENV: BUS}, timeout) is False


def test_ensure_waits_for_a_daemon_that_registers_late(monkeypatch):
    """gnome-keyring-daemon forks and returns before it owns org.freedesktop.secrets.
    Checking once, immediately, reports `missing` on a host where it is about to work
    -- which on exo2 made protonfs discard a good bus and call proton-drive missing."""
    monkeypatch.setattr(ss.time, "sleep", lambda _: None)

    class SlowToRegister(FakeRunner):
        def __init__(self):
            super().__init__(has_secrets=False)
            self.checks_after_start = 0
            self.started = False

        def __call__(self, cmd, env, stdin=None):
            if cmd[0] == "gnome-keyring-daemon":
                self.started = True
                # Deliberately does NOT set has_secrets: the daemon has forked, but the
                # bus name is not claimed yet.
                self.calls.append(cmd)
                return FakeCompleted()
            if self.started and cmd[0] == "gdbus":
                self.checks_after_start += 1
                if self.checks_after_start > 3:
                    self.has_secrets = True
                    self.locked = False
            return super().__call__(cmd, env, stdin)

    runner = SlowToRegister()
    result = ss.ensure_secret_service({}, runner)

    assert result.ready
    assert runner.checks_after_start > 3


def test_wait_for_secret_service_gives_up_and_reports_the_state(monkeypatch):
    monkeypatch.setattr(ss.time, "sleep", lambda _: None)
    state = ss.wait_for_secret_service(
        {ss.BUS_ENV: BUS}, FakeRunner(has_secrets=False), timeout=0.01
    )
    assert state == "missing"


def test_ensure_raises_when_the_daemon_never_becomes_usable(monkeypatch):
    monkeypatch.setattr(ss.time, "sleep", lambda _: None)
    monkeypatch.setattr(ss, "_REGISTER_TIMEOUT", 0.01)

    class NeverRegisters(FakeRunner):
        def __call__(self, cmd, env, stdin=None):
            if cmd[0] == "gnome-keyring-daemon":
                self.calls.append(cmd)
                return FakeCompleted()  # forked, but never claims the name
            return super().__call__(cmd, env, stdin)

    with pytest.raises(ss.SecretServiceError, match="still missing"):
        ss.ensure_secret_service({}, NeverRegisters(has_secrets=False))
