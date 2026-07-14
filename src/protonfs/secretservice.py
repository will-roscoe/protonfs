# src/protonfs/secretservice.py
"""Make the freedesktop Secret Service reachable on a headless Linux host.

`proton-drive` persists its session to the OS keyring. On Linux that means the
freedesktop Secret Service, reached over the **D-Bus session bus** -- not a file.
A plain SSH login has neither, which produces two failures in sequence:

1. No session bus at all. libdbus falls back to *autolaunch*, which needs X11, so
   every command dies with::

       Failed to load session from secrets (...): Cannot autolaunch D-Bus without X11 $DISPLAY

2. Once a bus exists, a Secret Service provider must own `org.freedesktop.secrets`
   **and** its default collection must be unlocked. A host that has ever had a
   graphical login carries a `~/.local/share/keyrings/login.keyring` sealed with
   that login password; it is the default collection, it is locked, and nothing
   can unlock it without a password the headless user does not have::

       org.freedesktop.Secret.Error.IsLocked: Cannot create an item in a locked collection

This module resolves both without root: it reuses or launches a session bus, and
it runs `gnome-keyring-daemon` against a **protonfs-owned XDG_DATA_HOME**, so the
daemon creates and unlocks a fresh keyring of ours instead of fighting the sealed
system one. The keyring password is generated once and stored 0600 alongside it.

That password buys no security on its own -- anyone who can read the password file
can read the keyring -- but it is not meant to. It is a local-attacker-equivalent
store, exactly like the session file `proton-drive` would otherwise write, and the
alternative (no session at all on this host) is strictly worse. Set
PROTONFS_KEYRING_PASSWORD to supply your own instead, or PROTONFS_NO_KEYRING_BOOTSTRAP=1
to opt out of all of this and manage the environment yourself.
"""
from __future__ import annotations

import os
import platform as _platform
import secrets as _secrets
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

KEYRING_PASSWORD_ENV = "PROTONFS_KEYRING_PASSWORD"
DISABLE_ENV = "PROTONFS_NO_KEYRING_BOOTSTRAP"
BUS_ENV = "DBUS_SESSION_BUS_ADDRESS"

SECRETS_BUS_NAME = "org.freedesktop.secrets"
_BUS_TIMEOUT = 15  # seconds; a wedged bus must not hang every protonfs command
_REGISTER_TIMEOUT = 10.0  # seconds to wait for a just-started daemon to claim the bus name
_POLL_INTERVAL = 0.2


class SecretServiceError(RuntimeError):
    """Raised with an instructive message when no usable keyring can be provided."""


@dataclass
class SecretsResult:
    """The environment `proton-drive` must run under, plus what we had to do."""

    env: dict[str, str]
    ready: bool
    actions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def is_linux(system: str | None = None) -> bool:
    return (system or _platform.system()).lower() == "linux"


def state_dir() -> Path:
    """Where protonfs keeps host-level (not repo-level) state."""
    return Path.home() / ".local" / "share" / "protonfs"


def secrets_home() -> Path:
    """The XDG_DATA_HOME we hand *only* to gnome-keyring-daemon.

    Overriding XDG_DATA_HOME for the daemon makes it read/write
    `<secrets_home>/keyrings/` instead of `~/.local/share/keyrings/`, which is how
    we get a keyring that is ours and unlocked rather than the sealed system one.
    Nothing else in protonfs or proton-drive sees this override -- clients reach
    the keyring over D-Bus and never touch the files.
    """
    return state_dir() / "secrets"


def runtime_dir() -> Path:
    """A per-boot directory for the cached bus address (tmpfs when available)."""
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "protonfs"


def bus_cache_file() -> Path:
    return runtime_dir() / "bus"


def keyring_password_file() -> Path:
    return secrets_home() / "keyring-password"


def keyring_password() -> str:
    """The protonfs keyring password: from the env override, else a generated one
    persisted 0600. Generated once and reused, so the keyring stays openable across
    logins without ever prompting."""
    override = os.environ.get(KEYRING_PASSWORD_ENV)
    if override:
        return override

    path = keyring_password_file()
    if path.exists():
        return path.read_text().strip()

    path.parent.mkdir(parents=True, exist_ok=True)
    password = _secrets.token_urlsafe(32)
    # Create with 0600 from the outset -- never write the secret to a
    # default-permission file and chmod it afterwards.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w") as fh:
        fh.write(password)
    return password


def _run(cmd: list[str], env: dict[str, str], stdin: str | None = None):
    return subprocess.run(
        cmd,
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=_BUS_TIMEOUT,
    )


def _gdbus_available() -> bool:
    return shutil.which("gdbus") is not None


def bus_responds(env: dict[str, str], runner=_run) -> bool:
    """Whether the bus named by env[BUS_ENV] is actually alive.

    A cached address survives the daemon it pointed at, so every address we adopt
    (env, cache file, socket path) gets pinged before we trust it.
    """
    if not env.get(BUS_ENV) or not _gdbus_available():
        return False
    try:
        result = runner(
            [
                "gdbus", "call", "--session",
                "--dest", "org.freedesktop.DBus",
                "--object-path", "/org/freedesktop/DBus",
                "--method", "org.freedesktop.DBus.ListNames",
            ],
            env,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _parse_sh_exports(text: str) -> dict[str, str]:
    """Parse `VAR='value';` lines as emitted by dbus-launch/gnome-keyring-daemon."""
    parsed: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip().rstrip(";")
        if line.startswith("export ") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"").rstrip(";")
        if key.isidentifier() and value:
            parsed[key] = value
    return parsed


def launch_bus(env: dict[str, str], runner=_run) -> str:
    """Start a private session bus and return its address. Raises if impossible."""
    if shutil.which("dbus-launch") is None:
        raise SecretServiceError(
            "no D-Bus session bus and `dbus-launch` is not installed, so proton-drive "
            "cannot reach the OS keyring. Install dbus-x11 (Debian/Ubuntu) or dbus "
            "(RHEL/CentOS), or set "
            f"{DISABLE_ENV}=1 and provide {BUS_ENV} plus a Secret Service yourself."
        )
    try:
        result = runner(["dbus-launch", "--sh-syntax"], env)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SecretServiceError(f"failed to start a D-Bus session bus: {exc}") from exc
    if result.returncode != 0:
        raise SecretServiceError(
            f"`dbus-launch` failed: {result.stderr.strip() or result.returncode}"
        )
    address = _parse_sh_exports(result.stdout).get(BUS_ENV)
    if not address:
        raise SecretServiceError(
            f"`dbus-launch` did not report a {BUS_ENV}: {result.stdout!r}"
        )
    return address


def resolve_bus(env: dict[str, str], runner=_run) -> tuple[str, str]:
    """Return (bus_address, how) where how is one of inherited|cached|xdg|launched.

    Ordered cheapest-first, and every candidate is pinged before adoption. The cache
    is what stops each protonfs command leaking its own dbus-daemon: the first
    command launches one, writes the address here, and later commands adopt it.
    """
    if env.get(BUS_ENV) and bus_responds(env, runner):
        return env[BUS_ENV], "inherited"

    cache = bus_cache_file()
    if cache.exists():
        cached = cache.read_text().strip()
        if cached and bus_responds({**env, BUS_ENV: cached}, runner):
            return cached, "cached"

    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        socket = Path(xdg) / "bus"
        if socket.exists():
            address = f"unix:path={socket}"
            if bus_responds({**env, BUS_ENV: address}, runner):
                return address, "xdg"

    address = launch_bus(env, runner)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(address)
    return address, "launched"


def secret_service_state(env: dict[str, str], runner=_run) -> str:
    """One of ready | missing | locked | unknown.

    `missing` and `locked` are genuinely different faults with near-identical
    symptoms, and they need different remedies -- start a provider vs. replace the
    one holding a sealed collection -- so we distinguish them explicitly rather
    than inferring from a failed call.
    """
    if not _gdbus_available():
        return "unknown"

    names = runner(
        [
            "gdbus", "call", "--session",
            "--dest", "org.freedesktop.DBus",
            "--object-path", "/org/freedesktop/DBus",
            "--method", "org.freedesktop.DBus.ListNames",
        ],
        env,
    )
    if names.returncode != 0 or SECRETS_BUS_NAME not in names.stdout:
        return "missing"

    alias = runner(
        [
            "gdbus", "call", "--session",
            "--dest", SECRETS_BUS_NAME,
            "--object-path", "/org/freedesktop/secrets",
            "--method", "org.freedesktop.Secret.Service.ReadAlias", "default",
        ],
        env,
    )
    if alias.returncode != 0:
        return "unknown"
    collection = _parse_object_path(alias.stdout)
    if collection is None or collection == "/":
        # A provider with no default collection cannot store the session either.
        return "locked"

    locked = runner(
        [
            "gdbus", "call", "--session",
            "--dest", SECRETS_BUS_NAME,
            "--object-path", collection,
            "--method", "org.freedesktop.DBus.Properties.Get",
            "org.freedesktop.Secret.Collection", "Locked",
        ],
        env,
    )
    if locked.returncode != 0:
        return "unknown"
    if "true" in locked.stdout:
        return "locked"
    return "ready"


def _parse_object_path(gdbus_output: str) -> str | None:
    """Pull `/org/freedesktop/secrets/collection/login` out of gdbus's tuple syntax."""
    start = gdbus_output.find("'")
    end = gdbus_output.find("'", start + 1)
    if start == -1 or end == -1:
        return None
    path = gdbus_output[start + 1 : end]
    return path if path.startswith("/") else None


def start_keyring(env: dict[str, str], replace: bool, runner=_run) -> dict[str, str]:
    """Run gnome-keyring-daemon's `secrets` component against our own keyring dir.

    `replace` is for the `locked` case: some other daemon already owns
    org.freedesktop.secrets with a collection we cannot unlock, so we must take the
    name from it. Returns any env vars the daemon asks us to export.
    """
    if shutil.which("gnome-keyring-daemon") is None:
        raise SecretServiceError(
            "no Secret Service on the session bus and `gnome-keyring-daemon` is not "
            "installed, so proton-drive has nowhere to store its session. Install "
            "gnome-keyring, or set "
            f"{DISABLE_ENV}=1 and provide a Secret Service yourself."
        )

    home = secrets_home()
    (home / "keyrings").mkdir(parents=True, exist_ok=True)

    # XDG_DATA_HOME is scoped to this one child process. It is the whole trick:
    # the daemon creates <secrets_home>/keyrings/login.keyring with *our* password
    # and leaves it unlocked, instead of finding the sealed system login.keyring.
    daemon_env = {**env, "XDG_DATA_HOME": str(home)}
    cmd = ["gnome-keyring-daemon", "--unlock", "--components=secrets"]
    if replace:
        cmd.insert(1, "--replace")

    try:
        result = runner(cmd, daemon_env, keyring_password())
    except (OSError, subprocess.SubprocessError) as exc:
        raise SecretServiceError(f"failed to start gnome-keyring-daemon: {exc}") from exc
    if result.returncode != 0:
        raise SecretServiceError(
            "gnome-keyring-daemon failed to start: "
            f"{result.stderr.strip() or result.returncode}"
        )
    return _parse_sh_exports(result.stdout)


def wait_for_secret_service(
    env: dict[str, str], runner=_run, timeout: float | None = None
) -> str:
    """Poll until the Secret Service is `ready`, or `timeout` elapses.

    gnome-keyring-daemon daemonizes: it forks and the parent exits *before* the child
    has claimed org.freedesktop.secrets on the bus. Checking the state immediately
    after start_keyring() therefore reports `missing` on a host where it is about to
    work perfectly -- observed on exo2, where the premature verdict made protonfs
    discard a perfectly good bus and then misreport proton-drive as uninstalled.
    """
    # Read the module constant at call time, not as a default argument, so it stays
    # tunable (and so tests can shorten it instead of really waiting).
    deadline = time.monotonic() + (_REGISTER_TIMEOUT if timeout is None else timeout)
    state = secret_service_state(env, runner)
    while state != "ready" and time.monotonic() < deadline:
        time.sleep(_POLL_INTERVAL)
        state = secret_service_state(env, runner)
    return state


def ensure_secret_service(env: dict[str, str] | None = None, runner=_run) -> SecretsResult:
    """Return an environment in which proton-drive can reach a usable keyring.

    Idempotent and cheap on the happy path: once a bus and an unlocked collection
    exist, this is two gdbus round-trips and no new processes.
    """
    base = dict(os.environ if env is None else env)

    if not is_linux():
        # macOS proton-drive uses the Keychain; there is nothing to bootstrap.
        return SecretsResult(env=base, ready=True)

    if os.environ.get(DISABLE_ENV):
        return SecretsResult(
            env=base,
            ready=True,
            actions=[f"{DISABLE_ENV} set; leaving the keyring environment untouched"],
        )

    address, how = resolve_bus(base, runner)
    base[BUS_ENV] = address
    actions = [] if how == "inherited" else [f"session bus: {how} ({address})"]

    state = secret_service_state(base, runner)
    if state == "ready":
        return SecretsResult(env=base, ready=True, actions=actions)

    if state == "unknown":
        return SecretsResult(
            env=base,
            ready=True,
            actions=actions,
            warnings=[
                "could not verify the Secret Service (gdbus not installed); proceeding "
                "anyway. If proton-drive reports a secrets error, install gdbus "
                "(glib2) so protonfs can diagnose and fix the keyring."
            ],
        )

    exported = start_keyring(base, replace=(state == "locked"), runner=runner)
    base.update(exported)
    actions.append(
        "started gnome-keyring (secrets) against "
        f"{secrets_home()}"
        + (", replacing the daemon holding a locked collection" if state == "locked" else "")
    )

    after = wait_for_secret_service(base, runner)
    if after in ("ready", "unknown"):
        return SecretsResult(env=base, ready=True, actions=actions)

    raise SecretServiceError(
        "started gnome-keyring but its default collection is still "
        f"{after}, so proton-drive cannot store its session. Remove "
        f"{secrets_home() / 'keyrings'} and {keyring_password_file()} to let protonfs "
        f"recreate the keyring from scratch, or set {KEYRING_PASSWORD_ENV} to the "
        "password of the existing keyring."
    )


def drive_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """The environment every proton-drive subprocess should inherit.

    Never raises: a keyring failure must surface as proton-drive's own error when a
    command actually needs the session, not as an import-time or list-time crash in
    protonfs. `protonfs doctor` is the place that reports the fault loudly.
    """
    try:
        return ensure_secret_service(env).env
    except SecretServiceError:
        return dict(os.environ if env is None else env)


def probe_secret_service(env: dict[str, str], runner=_run) -> tuple[bool, str]:
    """Write, read back and delete a throwaway secret. Returns (ok, detail).

    The strongest available check, and the only one that reproduces exactly what
    failed for a real user: `Locked=false` on the default collection is necessary
    but not sufficient evidence that an item can actually be created.
    """
    if shutil.which("secret-tool") is None:
        return True, "skipped (secret-tool not installed)"
    attrs = ["application", "protonfs-selftest"]
    try:
        store = runner(["secret-tool", "store", "--label=protonfs-selftest", *attrs],
                       env, "protonfs-selftest")
        if store.returncode != 0:
            return False, store.stderr.strip() or "secret-tool store failed"
        lookup = runner(["secret-tool", "lookup", *attrs], env)
        if lookup.returncode != 0 or lookup.stdout.strip() != "protonfs-selftest":
            return False, lookup.stderr.strip() or "stored secret did not read back"
        runner(["secret-tool", "clear", *attrs], env)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    return True, "stored, read back and cleared a test secret"
