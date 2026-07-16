# src/protonfs/drive.py
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

BINARY_ENV_VAR = "PROTONFS_DRIVE_BIN"
DEFAULT_BINARY = "proton-drive"

# #33: the Proton API throttles hard from rate-limited hosts (HPC login nodes), where a
# `filesystem list` degrades from <1s to 15-30s and then hangs for minutes. Cap each list
# with a timeout and retry with exponential backoff so one wedged directory fails that
# directory (and is retried) instead of hanging the whole walk. Overridable via env for
# low-latency vs. throttled hosts.
LIST_TIMEOUT_SECONDS = float(os.environ.get("PROTONFS_LIST_TIMEOUT", "45"))
LIST_MAX_RETRIES = int(os.environ.get("PROTONFS_LIST_RETRIES", "4"))
LIST_BACKOFF_BASE_SECONDS = float(os.environ.get("PROTONFS_LIST_BACKOFF", "2"))
LIST_BACKOFF_CAP_SECONDS = float(os.environ.get("PROTONFS_LIST_BACKOFF_CAP", "60"))


class DriveError(RuntimeError):
    pass


class DriveThrottleError(DriveError):
    """A `filesystem list` kept timing out / erroring under throttle past the retry budget.

    Distinct from a generic DriveError so callers (and the CLI) can report "the remote is
    throttling" clearly, and so a whole-tree walk fails one directory rather than hanging.
    """


class DriveAuthError(DriveError):
    pass


class DriveSecretsError(DriveError):
    """proton-drive could not reach/write the OS keyring that holds its session.

    Distinct from DriveAuthError: the user is not logged out, the machine simply has
    nowhere to keep the session. Re-running `auth login` cannot fix it and would just
    fail again at the same point -- `protonfs doctor` can.
    """


@dataclass
class TransferResult:
    transferred_items: int
    skipped_items: int
    failed_items: int
    failures: list[dict]

    @classmethod
    def from_json(cls, data: dict) -> TransferResult:
        return cls(
            transferred_items=data.get("transferredItems", 0),
            skipped_items=data.get("skippedItems", 0),
            failed_items=data.get("failedItems", 0),
            failures=data.get("failures", []),
        )


@dataclass
class RemoteEntry:
    rel_path: str
    is_dir: bool
    size: int  # encrypted totalStorageSize; runs slightly larger than the plaintext size
    # Plaintext identity (files only). `claimed_size`/`sha1` come from proton's decrypted
    # `claimedSize`/`claimedDigests.sha1`; either may be None if proton-drive did not
    # report it. Prefer these over `size` for any local-vs-remote comparison -- a local
    # byte size matches `claimed_size` exactly, with no encryption-overhead tolerance.
    claimed_size: int | None = None
    sha1: str | None = None


@dataclass
class RemoteIdentity:
    """Plaintext identity of a remote file, for verifying a local file against the remote.

    Proton exposes the *decrypted* original size (`claimedSize`) and content digests
    (`claimedDigests`) per file, distinct from the encrypted `totalStorageSize` which runs
    ~0.008% + padding larger. Always compare local files against these claimed* fields --
    a local byte size matches `claimed_size` exactly, with no encryption-overhead tolerance.
    Either field may be None if proton-drive did not report it.
    """

    claimed_size: int | None
    sha1: str | None


def binary_path() -> str:
    return os.environ.get(BINARY_ENV_VAR, DEFAULT_BINARY)


# Specific phrases that signal a genuine auth failure. Tightened from the v0.1 broad
# `"auth" in message` check, which false-positived on unrelated words ("author",
# "unauthorized"/permission errors). D5.1: we prefer a false negative (a real auth
# error surfaced as a generic DriveError, still an error) over a false positive
# (a non-auth error mislabelled as auth). If proton-drive's exact wording is later
# captured via a deliberate logout probe, add it here.
_AUTH_ERROR_SIGNALS = (
    "unauthenticated",
    "not authenticated",
    "not logged in",
    "log in first",
    "login required",
    "authentication required",
    "session expired",
    "auth required",
    "please authenticate",
)


# Phrases proton-drive/libsecret emit when the keyring itself is unreachable or
# sealed, captured verbatim from a headless CentOS 7 host. These must be checked
# *before* the auth signals: "Failed to load session from secrets" contains no auth
# wording, but it is a keyring fault, and telling the user to log in again sends
# them round a loop that fails at exactly the same place.
_SECRETS_ERROR_SIGNALS = (
    "cannot autolaunch d-bus",
    "err_secrets_platform_error",
    "locked collection",
    "islocked",
    "load session from secrets",
    "org.freedesktop.secret",
    "secret service",
)


def _is_secrets_error(message: str) -> bool:
    lowered = message.lower()
    return any(signal in lowered for signal in _SECRETS_ERROR_SIGNALS)


def _is_auth_error(message: str) -> bool:
    lowered = message.lower()
    return any(signal in lowered for signal in _AUTH_ERROR_SIGNALS)


# Phrases that indicate transient rate-limiting rather than a permanent fault, so a `list`
# is worth retrying with backoff (#33). The dominant throttle signature is a timeout (the
# call hangs), handled separately; these cover the cases where proton-drive/the API returns
# an explicit rate-limit error instead.
_THROTTLE_ERROR_SIGNALS = (
    "429",
    "too many requests",
    "rate limit",
    "rate-limit",
    "ratelimit",
    "throttl",
    "temporarily unavailable",
    "try again",
    "timed out",
    "timeout",
)


def _is_throttle_error(message: str) -> bool:
    lowered = message.lower()
    return any(signal in lowered for signal in _THROTTLE_ERROR_SIGNALS)


def _classify(message: str) -> DriveError:
    if _is_secrets_error(message):
        return DriveSecretsError(
            f"proton-drive could not use the OS keyring: {message}\n"
            "This host has no usable Secret Service (common over SSH with no desktop "
            "session). Run `protonfs doctor --fix`, then retry."
        )
    if _is_auth_error(message):
        return DriveAuthError(f"proton-drive auth required: {message}")
    return DriveError(message)


def decrypted_name(entry: dict) -> str | None:
    """The decrypted filename of a `filesystem list` entry, or None if its name could
    not be decrypted (``name.ok`` is false). Central helper so every consumer parses
    the ``{"name": {"ok": bool, "value": str}}`` shape identically."""
    name = entry.get("name", {})
    if not name.get("ok"):
        return None
    return name.get("value")


class DriveClient:
    def __init__(self, binary: str | None = None) -> None:
        self._binary = binary or binary_path()
        self._env: dict[str, str] | None = None

    @property
    def binary(self) -> str:
        return self._binary

    def binary_available(self) -> bool:
        """Whether the binary exists at all -- distinct from whether it *runs*. On a
        host with no keyring it exists and fails, and conflating the two tells users
        to reinstall a binary that is already there."""
        return shutil.which(self._binary) is not None or Path(self._binary).exists()

    def _binary_available(self) -> bool:
        return self.binary_available()

    def _drive_env(self) -> dict[str, str]:
        """Environment for proton-drive, with the keyring bootstrapped on first use.

        Resolved lazily and cached for the process: on a headless host the first
        call may launch a session bus, and doing that at import time would charge
        every `protonfs --help` for it.
        """
        from protonfs.secretservice import drive_env

        if self._env is None:
            self._env = drive_env()
        return self._env

    def _invoke(
        self, args: list[str], timeout: float | None = None
    ) -> subprocess.CompletedProcess:
        if not self._binary_available():
            raise DriveError(f"proton-drive binary not found: {self._binary}")
        return subprocess.run(
            [self._binary, *args, "--json"],
            capture_output=True,
            text=True,
            env=self._drive_env(),
            timeout=timeout,
        )

    def _run_json(self, args: list[str], timeout: float | None = None) -> dict | list:
        result = self._invoke(args, timeout=timeout)
        stdout = result.stdout.strip()
        try:
            parsed = json.loads(stdout) if stdout else {}
        except json.JSONDecodeError as exc:
            raise DriveError(f"unparseable output from proton-drive: {stdout!r}") from exc
        if result.returncode != 0:
            message = json.dumps(parsed) if parsed else result.stderr.strip()
            raise _classify(message)
        return parsed

    def _run_transfer(self, args: list[str]) -> TransferResult:
        result = self._invoke(args)
        stdout = result.stdout.strip()
        try:
            parsed = json.loads(stdout) if stdout else None
        except json.JSONDecodeError:
            parsed = None
        if not isinstance(parsed, dict) or "transferredItems" not in parsed:
            message = result.stderr.strip() or stdout
            raise _classify(message)
        return TransferResult.from_json(parsed)

    def version(self) -> str | None:
        if not self._binary_available():
            return None
        result = subprocess.run(
            [self._binary, "version"], capture_output=True, text=True, env=self._drive_env()
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    def is_authenticated(self) -> bool:
        """Whether a usable session exists. A *keyring* fault deliberately propagates:
        collapsing it to False would report "not authenticated" and send the user to
        `auth login`, which cannot succeed on a host with no writable keyring."""
        try:
            self._run_json(["filesystem", "list", "/"])
        except DriveSecretsError:
            raise
        except DriveError:
            return False
        return True

    def list(self, remote_path: str, timeout: float | None = None) -> list[dict]:
        result = self._run_json(["filesystem", "list", remote_path], timeout=timeout)
        return result if isinstance(result, list) else []

    def list_with_backoff(
        self,
        remote_path: str,
        *,
        timeout: float = LIST_TIMEOUT_SECONDS,
        retries: int = LIST_MAX_RETRIES,
        base_delay: float = LIST_BACKOFF_BASE_SECONDS,
        cap: float = LIST_BACKOFF_CAP_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> list[dict]:
        """`list`, but resilient to Proton API throttling (#33).

        Each attempt is bounded by `timeout`; a timeout (the throttle's degrade-then-hang
        signature) or a transient throttle error is retried with exponential backoff up to
        `retries` times, after which it raises `DriveThrottleError`. A genuine non-throttle
        error (auth, missing path) is raised immediately, not retried.
        """
        attempt = 0
        while True:
            try:
                return self.list(remote_path, timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                reason: str = f"list timed out after {timeout:g}s"
                last_error: Exception = exc
            except DriveError as exc:
                if not _is_throttle_error(str(exc)):
                    raise
                reason = str(exc)
                last_error = exc
            attempt += 1
            if attempt > retries:
                raise DriveThrottleError(
                    f"remote is throttling `list {remote_path}` ({reason}); "
                    f"gave up after {retries} retries. Back off and retry later."
                ) from last_error
            delay = min(base_delay * (2 ** (attempt - 1)), cap)
            logger.warning(
                "remote throttling on %s (%s); backing off %.1fs (retry %d/%d)",
                remote_path,
                reason,
                delay,
                attempt,
                retries,
            )
            sleep(delay)

    def remote_identities(self, remote_parent: str) -> dict[str, RemoteIdentity]:
        """Map decrypted filename -> plaintext identity for the files directly under
        `remote_parent`. Folders and entries with an undecryptable name are skipped.

        This is the single primitive every local-vs-remote comparison should route through
        (verify-after-push, offload-before-delete, cross-client drift): it reads the
        plaintext `claimedSize`/`claimedDigests.sha1`, never the encrypted `totalStorageSize`.
        """
        identities: dict[str, RemoteIdentity] = {}
        for entry in self.list(remote_parent):
            if entry.get("type") == "folder":
                continue
            name = decrypted_name(entry)
            if name is None:
                continue
            digests = entry.get("claimedDigests") or {}
            identities[name] = RemoteIdentity(
                claimed_size=entry.get("claimedSize"),
                sha1=digests.get("sha1"),
            )
        return identities

    def walk(
        self,
        remote_root: str,
        on_directory: Callable[[list[RemoteEntry]], None] | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> list[RemoteEntry]:
        """Breadth-first walk of the remote tree.

        Each directory is listed with throttle backoff (#33). If `on_directory` is given it
        is called with that directory's FILE entries right after the directory is listed, so
        a caller (refresh) can persist progress per directory -- if a later directory wedges
        past the retry budget, everything already handed to `on_directory` is durable.
        """
        root = remote_root.rstrip("/")
        results: list[RemoteEntry] = []
        # queue of (absolute remote path, rel prefix); deque gives O(1) popleft
        queue: deque[tuple[str, str]] = deque([(root, "")])
        while queue:
            abs_path, prefix = queue.popleft()
            dir_files: list[RemoteEntry] = []
            for entry in self.list_with_backoff(abs_path, sleep=sleep):
                value = decrypted_name(entry)
                if value is None:
                    logger.warning(
                        "skipping remote entry with undecryptable name under %s", abs_path
                    )
                    continue
                rel = f"{prefix}{value}" if prefix else value
                child_abs = f"{abs_path}/{value}"
                if entry.get("type") == "folder":
                    results.append(RemoteEntry(rel_path=rel, is_dir=True, size=0))
                    queue.append((child_abs, f"{rel}/"))
                else:
                    digests = entry.get("claimedDigests") or {}
                    file_entry = RemoteEntry(
                        rel_path=rel,
                        is_dir=False,
                        size=entry.get("totalStorageSize", 0),
                        claimed_size=entry.get("claimedSize"),
                        sha1=digests.get("sha1"),
                    )
                    results.append(file_entry)
                    dir_files.append(file_entry)
            if on_directory is not None:
                on_directory(dir_files)
        return results

    def upload(
        self,
        local_paths: list[Path],
        remote_parent: str,
        file_strategy: str | None = None,
        folder_strategy: str | None = None,
    ) -> TransferResult:
        args = ["filesystem", "upload"]
        if file_strategy:
            args += ["-f", file_strategy]
        if folder_strategy:
            args += ["-d", folder_strategy]
        args += [str(p) for p in local_paths] + [remote_parent]
        return self._run_transfer(args)

    def download(
        self,
        remote_paths: list[str],
        local_folder: Path,
        file_strategy: str | None = None,
        folder_strategy: str | None = None,
    ) -> TransferResult:
        args = ["filesystem", "download"]
        if file_strategy:
            args += ["-f", file_strategy]
        if folder_strategy:
            args += ["-d", folder_strategy]
        args += remote_paths + [str(local_folder)]
        return self._run_transfer(args)

    def trash(self, remote_paths: list[str]) -> list[dict]:
        result = self._run_json(["filesystem", "trash", *remote_paths])
        return result if isinstance(result, list) else []

    def restore(self, remote_paths: list[str]) -> list[dict]:
        result = self._run_json(["filesystem", "restore", *remote_paths])
        return result if isinstance(result, list) else []

    def delete(self, remote_paths: list[str]) -> list[dict]:
        result = self._run_json(["filesystem", "delete", *remote_paths])
        return result if isinstance(result, list) else []

    def create_folder(self, parent_path: str, name: str) -> dict:
        result = self._run_json(["filesystem", "create-folder", parent_path, name])
        return result if isinstance(result, dict) else {}
