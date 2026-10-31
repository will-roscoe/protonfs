# src/protonfs/drive.py
"""The proton-drive CLI wrapper: :class:`DriveClient` plus its result/error types.

Every Drive operation protonfs performs shells out to the pinned ``proton-drive``
binary and parses its JSON. This module wraps that: it classifies the CLI's error
output into typed exceptions (auth vs keyring vs throttle vs generic), rides out API
throttling with bounded exponential backoff on both listing and transfers, and models
remote files with their *plaintext* identity (``claimedSize``/``claimedDigests``) so
local-vs-remote comparisons never trip over Proton's encryption-size overhead.

.. versionadded:: 1.0.0
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path

from protonfs.logs import backend_passthrough_enabled

logger = logging.getLogger(__name__)

BINARY_ENV_VAR = "PROTONFS_DRIVE_BIN"
DEFAULT_BINARY = "proton-drive"

# Matches the semver embedded in `proton-drive version` output, e.g.
# "Proton Drive CLI cli-drive@0.5.0+73e40d90" -> "0.5.0". The build metadata after
# `+` (if any) is deliberately not captured (issue #65: the support matrix compares
# on the semver only).
_VERSION_RE = re.compile(r"cli-drive@(\d+\.\d+\.\d+)")
# The last line of the `proton-drive version` banner. cli-drive 0.8.0+ then fetches
# proton.me's release manifest (up to 5 s) to print an update hint; protonfs pins its
# own version and checks upstream itself, so version() stops reading here.
_VERSION_BANNER_END = "Proton Drive SDK"

# #179: cli-drive@0.8.0 renamed the conflict strategies. protonfs speaks the 0.4.6-0.7.0
# names everywhere; these are translated, per command and option, only for 0.8.0 onwards.
_STRATEGY_RENAMES_FROM = (0, 8, 0)
_STRATEGY_RENAMES = {
    ("upload", "-f"): {"merge": "create-new-revision", "keep-both": "rename"},
    ("upload", "-d"): {"keep-both": "rename"},
    ("download", "-f"): {"keep-both": "rename", "replace": "remove"},
    ("download", "-d"): {"keep-both": "rename", "replace": "remove"},
}

# #33: the Proton API throttles hard from rate-limited hosts (HPC login nodes), where a
# `filesystem list` degrades from <1s to 15-30s and then hangs for minutes. Cap each list
# with a timeout and retry with exponential backoff so one wedged directory fails that
# directory (and is retried) instead of hanging the whole walk. Overridable via env for
# low-latency vs. throttled hosts.
LIST_TIMEOUT_SECONDS = float(os.environ.get("PROTONFS_LIST_TIMEOUT", "45"))
LIST_MAX_RETRIES = int(os.environ.get("PROTONFS_LIST_RETRIES", "4"))
LIST_BACKOFF_BASE_SECONDS = float(os.environ.get("PROTONFS_LIST_BACKOFF", "2"))
LIST_BACKOFF_CAP_SECONDS = float(os.environ.get("PROTONFS_LIST_BACKOFF_CAP", "60"))

# #69: the same throttling that degrades `list` (#33) also hits `filesystem upload`/
# `download` mid-transfer -- one throttled batch otherwise fails the whole push/pull run.
# Push/pull are resumable (#3) so a re-run recovers, but the run itself should ride out
# transient throttling the same way `list_with_backoff` does. Timeout defaults higher than
# list's: a transfer batch legitimately takes longer than a directory listing. Overridable
# via env, named to mirror the PROTONFS_LIST_* knobs.
TRANSFER_TIMEOUT_SECONDS = float(os.environ.get("PROTONFS_TRANSFER_TIMEOUT", "300"))
TRANSFER_MAX_RETRIES = int(os.environ.get("PROTONFS_TRANSFER_RETRIES", "4"))
TRANSFER_BACKOFF_BASE_SECONDS = float(os.environ.get("PROTONFS_TRANSFER_BACKOFF", "2"))
TRANSFER_BACKOFF_CAP_SECONDS = float(os.environ.get("PROTONFS_TRANSFER_BACKOFF_CAP", "60"))

# #184: a fixed timeout cannot fit every batch: one carrying several GiB over a slow uplink
# legitimately runs for hours, and retrying it with the same timeout fails it the same way
# each time. Each transfer's timeout is therefore at least its payload over this floor rate
# (bytes per second), the slowest transfer still counted as progress rather than a hang.
TRANSFER_MIN_RATE = float(os.environ.get("PROTONFS_TRANSFER_MIN_RATE", str(512 * 1024)))

# Proton checks each upload's signatures against its own clock and rejects any dated ahead
# of it. Measured 31 Oct 2026: signed 60 s ahead went through, 90 s ahead failed. A host
# whose clock runs fast therefore fails every file, so upload measures the offset first.
#: Seconds this host's clock may run ahead of Proton's before uploads are refused
#: (default ``30``, env ``PROTONFS_CLOCK_AHEAD_LIMIT``).
CLOCK_AHEAD_LIMIT_SECONDS = float(os.environ.get("PROTONFS_CLOCK_AHEAD_LIMIT", "30"))
PROTON_TIME_URL = "https://drive-api.proton.me/core/v4/tests/ping"
CLOCK_PROBE_TIMEOUT_SECONDS = 10.0
_SIGNATURE_TIME_ERRORS = (
    "Could not verify the nodeKey was used for encrypting",
    "Signature creation time is in the future",
)


class DriveError(RuntimeError):
    """Base class for every proton-drive failure protonfs surfaces.

    The subclasses distinguish causes that need different remedies (auth, keyring,
    throttle); a plain ``DriveError`` is any other Drive-side failure.
    """


class DriveLockError(DriveError):
    """proton-drive's local SQLite cache is locked by another proton-drive process (#136).

    Distinct from :class:`DriveThrottleError` because it is not transient: it persists
    until the holding process goes away, so it must not be retried with backoff.

    .. versionadded:: 1.11.0
    """


class DriveThrottleError(DriveError):
    """A `filesystem list` kept timing out / erroring under throttle past the retry budget.

    Distinct from a generic DriveError so callers (and the CLI) can report "the remote is
    throttling" clearly, and so a whole-tree walk fails one directory rather than hanging.
    """


class DriveTransferTimeoutError(DriveError):
    """A ``filesystem upload``/``download`` batch timed out on every attempt (#184).

    Distinct from :class:`DriveThrottleError`: the remote did not refuse anything, the
    batch carried more data than the link moved within the timeout. The remedy is a
    longer timeout or a smaller batch, which the message names.

    .. versionadded:: 2.5.0
    """


class DriveClockSkewError(DriveError):
    """This host's clock runs ahead of Proton's, so Proton rejects every upload it signs.

    Raised by :meth:`DriveClient.upload` before sending anything when the measured offset
    is over :data:`CLOCK_AHEAD_LIMIT_SECONDS`, or after a call in which every file failed
    on a signature dated in the future. Not transient: retrying fails the same way until
    the clock is synced.

    :ivar offset: seconds the clock was measured ahead of Proton's, or ``None`` if the
        offset could not be measured.

    .. versionadded:: 3.3.0
    """

    def __init__(self, offset: float | None) -> None:
        """Build the message for a clock measured ``offset`` seconds ahead.

        :param offset: seconds ahead of Proton's clock, or ``None`` if unmeasured.
        """
        self.offset = offset
        ahead = f"{offset:.0f} s ahead of" if offset is not None else "ahead of"
        super().__init__(
            f"this host's clock is {ahead} Proton's servers, and Proton rejects uploads "
            f"signed in the future (limit {CLOCK_AHEAD_LIMIT_SECONDS:g} s): sync the clock "
            "(NTP), or set PROTONFS_CLOCK_AHEAD_LIMIT if the measurement is wrong"
        )


class DriveAuthError(DriveError):
    """proton-drive has no usable session -- the user must ``auth login`` again.

    Distinct from :class:`DriveSecretsError`: here the credentials/session are the
    problem, not the keyring that would store them.
    """


class DriveSecretsError(DriveError):
    """proton-drive could not reach/write the OS keyring that holds its session.

    Distinct from DriveAuthError: the user is not logged out, the machine simply has
    nowhere to keep the session. Re-running `auth login` cannot fix it and would just
    fail again at the same point -- `protonfs doctor` can.
    """


@dataclass
class TransferResult:
    """Summary of a ``filesystem upload``/``download`` batch.

    :ivar transferred_items: files actually transferred.
    :ivar skipped_items: files the CLI skipped (already in sync, or excluded).
    :ivar failed_items: files that failed to transfer.
    :ivar failures: the raw per-failure dicts from the CLI, for reporting.
    :ivar adopted_items: files already present on the remote (an upload name-conflict)
        that push verified match the local copy and recorded in the index without
        re-uploading (#self-heal). Not part of the CLI's JSON; populated by push.

    :ivar unsettled_items: files push held back because they were modified within its
        settle window (``min_age``); not part of the CLI's JSON; populated by push.
    :ivar local_deleted_items: files this machine held that are gone locally (deleted, or
        moved away) and were left alone: by pull with ``no_restore``, or by push when it
        neither moved nor trashed them; not part of the CLI's JSON.
    :ivar moved_items: files push moved or renamed on Drive to follow a local move,
        instead of uploading them again; not part of the CLI's JSON; populated by push.
    :ivar trashed_items: files push moved to the Drive trash because they were deleted
        locally (``deletions="trash"``); with a dry run, the ones it would. Populated by
        push.
    :ivar trashed: the repo-relative paths counted in ``trashed_items``.
    :ivar excluded_items: files pull left on Drive because the store's
        ``.protonfs/ignore``/``include`` rules exclude them. Populated by pull.

    .. versionchanged:: 1.7.0
       Added ``adopted_items`` for the push self-heal-adopt path.

    .. versionchanged:: 2.4.0
       Added ``unsettled_items`` for push's settle window (#168).

    .. versionchanged:: 3.0.0
       Added ``local_deleted_items`` for the local deletions pull and push left alone,
       ``moved_items`` for the local moves push applies on Drive, and ``trashed_items``
       and ``trashed`` for the local deletions it sends to the Drive trash (#197).

    .. versionchanged:: 3.1.0
       Added ``excluded_items``.
    """

    transferred_items: int
    skipped_items: int
    failed_items: int
    failures: list[dict]
    adopted_items: int = 0
    unsettled_items: int = 0
    local_deleted_items: int = 0
    moved_items: int = 0
    excluded_items: int = 0
    trashed_items: int = 0
    trashed: list[str] = field(default_factory=list)

    @classmethod
    def from_json(cls, data: dict) -> TransferResult:
        """Build a :class:`TransferResult` from the CLI's JSON, defaulting missing counts to 0.

        :param data: the parsed ``value`` object from a transfer invocation.
        :returns: the populated result.
        """
        return cls(
            transferred_items=data.get("transferredItems", 0),
            skipped_items=data.get("skippedItems", 0),
            failed_items=data.get("failedItems", 0),
            failures=data.get("failures", []),
        )


@dataclass
class RemoteEntry:
    """One entry from a remote directory walk.

    :ivar rel_path: path relative to the walk root.
    :ivar is_dir: whether the entry is a directory.
    :ivar size: the encrypted ``totalStorageSize`` (slightly larger than plaintext).
    :ivar claimed_size: proton's decrypted ``claimedSize`` (files only), or ``None``.
    :ivar sha1: proton's plaintext ``claimedDigests.sha1`` (files only), or ``None``.
    :ivar revision: the uid of the file's active revision (files only), or ``None``.

    .. versionchanged:: 2.1.0
       Added ``revision``.

    .. note:: Prefer ``claimed_size``/``sha1`` over ``size`` for any local-vs-remote
        comparison -- a local byte size matches ``claimed_size`` exactly, whereas
        ``size`` carries Proton's encryption overhead.
    """

    rel_path: str
    is_dir: bool
    size: int  # encrypted totalStorageSize; runs slightly larger than the plaintext size
    # Plaintext identity (files only). `claimed_size`/`sha1` come from proton's decrypted
    # `claimedSize`/`claimedDigests.sha1`; either may be None if proton-drive did not
    # report it. Prefer these over `size` for any local-vs-remote comparison -- a local
    # byte size matches `claimed_size` exactly, with no encryption-overhead tolerance.
    claimed_size: int | None = None
    sha1: str | None = None
    revision: str | None = None


@dataclass
class RemoteIdentity:
    """Plaintext identity of a remote file, for verifying a local file against the remote.

    Proton exposes the *decrypted* original size (`claimedSize`) and content digests
    (`claimedDigests`) per file, distinct from the encrypted `totalStorageSize` which runs
    ~0.008% + padding larger. Always compare local files against these claimed* fields --
    a local byte size matches `claimed_size` exactly, with no encryption-overhead tolerance.
    Either field may be None if proton-drive did not report it.

    ``revision`` is the uid of the node's active revision: re-uploading a file as a new
    revision (``-f merge``) changes it, so it identifies which copy of a path Drive holds.

    .. versionchanged:: 2.1.0
       Added ``revision``.
    """

    claimed_size: int | None
    sha1: str | None
    revision: str | None = None


def binary_path() -> str:
    """Return the proton-drive binary to invoke: ``$PROTONFS_DRIVE_BIN`` or the default.

    :returns: the configured binary path/name (``"proton-drive"`` by default).
    """
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
    """Return whether a CLI error message signals an unreachable/sealed OS keyring.

    :param message: the CLI's stderr text.
    :returns: ``True`` if it matches a known keyring-fault signal (checked before auth).
    """
    lowered = message.lower()
    return any(signal in lowered for signal in _SECRETS_ERROR_SIGNALS)


def _is_auth_error(message: str) -> bool:
    """Return whether a CLI error message signals a genuine authentication failure.

    :param message: the CLI's stderr text.
    :returns: ``True`` if it matches a known auth-failure phrase.
    """
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


# #136: proton-drive keeps a local SQLite cache and takes an exclusive lock on it. A
# proton-drive process that outlives its protonfs parent (SIGTERM/SIGHUP kill the parent
# without running its cleanup) keeps that lock, and then EVERY later proton-drive call on
# the host fails. proton-drive reports it as a non-JSON error blob, so without this it
# surfaced as the generic "unparseable output from proton-drive: ...", which points
# nowhere near the actual remedy (find and kill the stale process).
_LOCK_ERROR_SIGNALS = ("sqlite_busy", "database is locked")


def _is_lock_error(message: str) -> bool:
    """Whether a CLI message signals proton-drive's SQLite cache lock being held.

    Deliberately NOT folded into :func:`_is_throttle_error`: a held lock is not transient
    the way rate-limiting is. Retrying with backoff would burn the whole retry budget while
    the stale process kept holding it, so this must surface immediately.
    """
    lowered = message.lower()
    return any(signal in lowered for signal in _LOCK_ERROR_SIGNALS)


_LOCK_ERROR_HINT = (
    "proton-drive could not open its local cache: the database is locked by another "
    "proton-drive process.\nThis is usually a stale process left behind when a protonfs "
    "run was killed. Check with `pgrep -af proton-drive` and kill any leftover process, "
    "then retry."
)


# #137: set once a run has already reported degraded verification, so a push spanning many
# parent directories says it once rather than once per directory.
_CLAIMED_METADATA_WARNED = False


def _warn_if_verification_degraded(
    identities: dict[str, RemoteIdentity], describe_version: Callable[[], str | None]
) -> None:
    """Warn (once per process) when a non-empty listing carries no plaintext metadata.

    Every local-vs-remote comparison routes through ``claimedSize``/``claimedDigests.sha1``.
    When neither reaches us, nothing in the listing can be verified: push reports such
    files as unverified and does not index them (#144), and offload refuses to delete
    them. Before #144 push instead passed them on name presence alone (#137). Either way
    the cause must not be silent, so this says once where it comes from; push reports
    the affected files individually.

    Historically this fired because the fields were read from the wrong level of the
    entry (#147) rather than because proton-drive withheld them, so the message points at
    the listing shape instead of telling the user to upgrade.

    An EMPTY listing is not evidence either way (a first push into a new folder sees one),
    so it never warns.

    ``describe_version`` is a CALLABLE, resolved only when a warning is actually emitted:
    reading the version shells out to ``proton-drive``, which carries seconds of SDK
    startup, and this runs after every verify listing (once per parent directory on a
    push). Resolving it eagerly would put a subprocess spawn on that hot path -- and would
    make the function unusable for any caller that has not stubbed the binary.
    """
    global _CLAIMED_METADATA_WARNED
    if _CLAIMED_METADATA_WARNED or not identities:
        return
    if any(i.claimed_size is not None or i.sha1 for i in identities.values()):
        return
    _CLAIMED_METADATA_WARNED = True
    version = describe_version()
    logger.warning(
        "no claimedSize/claimedDigests on any entry of this listing (proton-drive %s), "
        "so no remote copy in it can be verified: push will report those files as "
        "unverified and leave them unindexed, and offload will not delete them. This is "
        "usually a listing shape protonfs does not yet parse rather than a missing "
        "capability, so please report it with the output of "
        "`proton-drive filesystem list <path> --json`.",
        version or "unknown version",
    )


def _is_throttle_error(message: str) -> bool:
    """Return whether a CLI error message signals transient throttling (worth a retry).

    :param message: the CLI's stderr text.
    :returns: ``True`` for rate-limit/timeout signatures, ``False`` for permanent faults.
    """
    lowered = message.lower()
    return any(signal in lowered for signal in _THROTTLE_ERROR_SIGNALS)


# A `filesystem upload`/`download` argv carries every file path in the batch (hundreds
# for a big sim dir). Spelling all of them out in a throttle warning or error made the
# output unreadable (issue: "unreadable if pushing 700 files"), so collapse the middle
# path run to a count while keeping the verb and the destination.
_SUMMARIZE_THRESHOLD = 6


def _summarize_command(args: list[str]) -> str:
    """Render an argv for logs/errors, collapsing a long file list to a count.

    Short commands pass through verbatim. A long one keeps its first two tokens (e.g.
    ``filesystem upload``) and its final argument (the remote/local destination), and
    replaces the file paths in between with ``(<n> files)``.

    :param args: the argument vector handed to ``proton-drive``.
    :returns: a bounded, human-readable one-line description.
    """
    parts = [str(a) for a in args]
    if len(parts) <= _SUMMARIZE_THRESHOLD:
        return " ".join(parts)
    middle = parts[2:-1]
    return f"{parts[0]} {parts[1]} ({len(middle)} files) {parts[-1]}"


# proton-drive hands any local path containing one of these to Node's fs.glob (cli 0.9.0,
# cli/src/commands/fileSystem/localPath.ts), and a pattern that matches nothing aborts
# the whole upload/download before anything moves. Node's glob runs minimatch with
# windowsPathsNoEscape, so a backslash is a path separator and cannot escape anything;
# a one-character class ([[], [*], ...) can. Brace lists are expanded before classes
# are parsed, so a name holding {a,b} cannot be written as a pattern at all.
_GLOB_TRIGGER = re.compile(r"[*?\[{]")
_GLOB_ESCAPE = re.compile(r"[*?\[{(]")  # "(" so +(x) @(x) !(x) stay literal too
_BRACE_LIST = re.compile(r"\{.*,.*\}", re.DOTALL)


def _glob_literal(path: str) -> str | None:
    """Return ``path`` as proton-drive must be given it to mean exactly that path.

    :param path: an absolute local path.
    :returns: ``path`` unchanged when proton-drive will not glob it, else the path with
        each glob metacharacter wrapped in a one-character class; ``None`` when no
        pattern can name it (it holds a backslash or a ``{a,b}`` brace list).
    """
    if not _GLOB_TRIGGER.search(path):
        return path
    if "\\" in path or _BRACE_LIST.search(path):
        return None
    return _GLOB_ESCAPE.sub(lambda m: f"[{m.group()}]", path)


_UNNAMEABLE = (
    "proton-drive reads this path as a glob pattern, and a name holding a backslash or "
    "a {a,b} brace list cannot be escaped for it; rename the file to transfer it"
)


def _fatal_line(*outputs: str) -> str:
    """The line of a proton-drive crash report that says what went wrong.

    :param outputs: the CLI's stderr and whatever followed its summary on stdout (a
        crash prints the summary, then the code frame and stack trace, on stdout).
    :returns: the first line that is not a code frame, stack frame, caret, banner or
        details block, e.g. ``No paths matched: …`` or ``ENOENT: no such file …``; the
        joined, stripped outputs when there is no such line.
    """
    for line in "\n".join(outputs).splitlines():
        text = line.strip()
        if (
            not text
            or line[0].isspace()
            or text[0] in "{}"
            or set(text) <= {"=", "^", " "}
            or re.match(r"\d+ \|", text)
            or text.startswith("Error details")
        ):
            continue
        return text
    return "\n".join(o.strip() for o in outputs if o.strip())


# proton-drive's ILLEGAL_SEGMENT_CHARS (cli 0.9.0, downloadPathValidation.ts).
_DOWNLOAD_ILLEGAL = re.compile(r'[\x00-\x1f\x7f<>:"|?*\\/]')
_WIN_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$", re.IGNORECASE)


def downloaded_name(name: str) -> str:
    """Return the name proton-drive gives a Drive file it downloads called ``name``.

    proton-drive replaces each control character and each of ``< > : " | ? * \\ /``
    with ``_``, turns ``.`` and ``..`` into ``_`` and ``__``, and on Windows also
    prefixes a reserved device name with ``_`` and replaces trailing dots and spaces.
    Two Drive names can therefore land under one local name (``a?.dat`` and ``a*.dat``
    both become ``a_.dat``).

    :param name: the file's name on Drive.
    :returns: the name of the file the download writes.

    .. versionadded:: 3.1.1
    """
    segment = _DOWNLOAD_ILLEGAL.sub("_", name)
    if segment in ("", "."):
        return "_"
    if segment == "..":
        return "__"
    if os.name == "nt":
        if _WIN_RESERVED.match(segment):
            segment = f"_{segment}"
        segment = re.sub(r"[. ]+$", lambda m: "_" * len(m.group()), segment)
    return segment


class _TransferTimedOut(Exception):
    """One transfer attempt hit its timeout; carries the timeout that expired (#184)."""

    def __init__(self, seconds: float) -> None:
        super().__init__(f"timed out after {seconds:.0f}s")
        self.seconds = seconds


def transfer_timeout(timeout: float, payload_bytes: int | None) -> float:
    """Return the timeout for a transfer of ``payload_bytes``: ``timeout``, or longer.

    The result is ``max(timeout, payload_bytes / TRANSFER_MIN_RATE)``, so a batch
    carrying several GiB is not cut off by a timeout sized for small files (#184).

    :param timeout: the configured timeout in seconds (``$PROTONFS_TRANSFER_TIMEOUT``).
    :param payload_bytes: bytes the transfer moves, or ``None`` when unknown.
    :returns: the timeout in seconds; ``timeout`` itself when ``payload_bytes`` is
        ``None`` or :data:`TRANSFER_MIN_RATE` is not positive.

    .. versionadded:: 2.5.0
    """
    if not payload_bytes or TRANSFER_MIN_RATE <= 0:
        return timeout
    return max(timeout, payload_bytes / TRANSFER_MIN_RATE)


def _payload_bytes(paths: list[Path]) -> int:
    """Total size of the regular files at or under ``paths``; unreadable entries count 0."""
    total = 0
    for path in paths:
        try:
            if path.is_dir():
                total += sum(
                    child.stat().st_size for child in path.rglob("*") if child.is_file()
                )
            else:
                total += path.stat().st_size
        except OSError:
            continue
    return total


def _retry_with_backoff(
    attempt_fn: Callable[[], object],
    *,
    describe: str,
    retries: int,
    base_delay: float,
    cap: float,
    sleep: Callable[[float], None],
) -> object:
    """Shared throttle-retry loop behind `list_with_backoff` (#33) and the transfer
    backoff (#69): call `attempt_fn`, retrying with exponential backoff (capped at `cap`)
    on a timeout or a throttle-classified `DriveError` (via `_is_throttle_error`), up to
    `retries` times. A genuine non-throttle `DriveError` propagates immediately, unretried.
    Past the retry budget, raises `DriveThrottleError` describing what was being retried.
    A `_TransferTimedOut` (raised only by transfers, #184) is retried without a sleep and
    re-raised as itself past the budget, since a transfer timeout is not throttling.
    """
    attempt = 0
    while True:
        try:
            return attempt_fn()
        except _TransferTimedOut as exc:
            attempt += 1
            if attempt > retries:
                raise
            logger.warning(
                "%s timed out after %.0fs; retrying with %.0fs (retry %d/%d)",
                describe,
                exc.seconds,
                exc.seconds * 2,
                attempt,
                retries,
            )
            continue
        except subprocess.TimeoutExpired as exc:
            reason: str = f"{describe} timed out"
            last_error: Exception = exc
        except DriveError as exc:
            if not _is_throttle_error(str(exc)):
                raise
            reason = str(exc)
            last_error = exc
        attempt += 1
        if attempt > retries:
            raise DriveThrottleError(
                f"remote is throttling {describe} ({reason}); "
                f"gave up after {retries} retries. Back off and retry later."
            ) from last_error
        delay = min(base_delay * (2 ** (attempt - 1)), cap)
        logger.warning(
            "remote throttling on %s (%s); backing off %.1fs (retry %d/%d)",
            describe,
            reason,
            delay,
            attempt,
            retries,
        )
        sleep(delay)


def proton_clock_offset(
    url: str = PROTON_TIME_URL,
    timeout: float = CLOCK_PROBE_TIMEOUT_SECONDS,
    opener: Callable = urllib.request.urlopen,
    clock: Callable[[], float] = time.time,
) -> float | None:
    """Return how many seconds this host's clock is ahead of Proton's (negative: behind).

    Reads the ``Date`` header of one ``HEAD`` request to ``url``; an HTTP error status
    still carries the header. The header has whole-second resolution, so the result is
    good to about a second plus half the round trip.

    :param url: a Proton endpoint whose response is dated by Proton's clock.
    :param timeout: request timeout in seconds.
    :param opener: ``urllib.request.urlopen`` or a stand-in (for tests).
    :param clock: the local clock, ``time.time`` or a stand-in (for tests).
    :returns: the offset in seconds, or ``None`` if Proton could not be reached or sent
        no usable ``Date`` header.

    .. versionadded:: 3.3.0
    """
    request = urllib.request.Request(url, method="HEAD")
    before = clock()
    try:
        with opener(request, timeout=timeout) as response:
            date = response.headers.get("Date")
    except urllib.error.HTTPError as exc:
        date = exc.headers.get("Date") if exc.headers else None
    except (OSError, ValueError):
        return None
    after = clock()
    try:
        proton = parsedate_to_datetime(date).timestamp() + 0.5 if date else None
    except (TypeError, ValueError):
        proton = None
    return None if proton is None else (before + after) / 2 - proton


def _is_signature_time_error(failure: dict) -> bool:
    """Return True if a transfer failure is Proton refusing a signature dated in the future."""
    error = str(failure.get("error", ""))
    return any(marker in error for marker in _SIGNATURE_TIME_ERRORS)


def is_thumbnail_failure(failure: dict) -> bool:
    """Whether an upload failure is proton-drive refusing a file it cannot thumbnail.

    proton-drive needs the OS image codec to build a thumbnail for HEIC, AVIF or TIFF,
    and fails the file without one (``ValidationError: Failed to generate thumbnails``).
    The same file uploads with ``-t`` (#183).

    :param failure: one entry of :attr:`TransferResult.failures`.
    :returns: ``True`` when re-uploading with ``skip_thumbnails`` should succeed.

    .. versionadded:: 2.5.0
    """
    return "failed to generate thumbnail" in str(failure.get("error", "")).lower()


def _classify(message: str) -> DriveError:
    """Map a CLI error message to the most specific :class:`DriveError` subclass.

    Keyring faults are checked before auth (a keyring message carries no auth wording
    but must not send the user round the login loop).

    :param message: the CLI's error text.
    :returns: a :class:`DriveSecretsError`, :class:`DriveAuthError`, or plain
        :class:`DriveError`.
    """
    # #136: checked first -- a locked cache blocks every command regardless of auth or
    # keyring state, and its message carries none of the wording the other checks look for.
    if _is_lock_error(message):
        return DriveLockError(_LOCK_ERROR_HINT)
    if _is_secrets_error(message):
        return DriveSecretsError(
            f"proton-drive could not use the OS keyring: {message}\n"
            "This host has no usable Secret Service (common over SSH with no desktop "
            "session). Run `protonfs doctor --fix`, then retry."
        )
    if _is_auth_error(message):
        return DriveAuthError(f"proton-drive auth required: {message}")
    return DriveError(message)


def active_revision(entry: dict) -> dict | None:
    """The active revision of a `filesystem list` entry, or None if it is unreadable.

    Plaintext identity (``claimedSize``, ``claimedDigests``) lives on the revision, not
    on the entry, and the CLI nests it two different ways: ``cli-drive@0.5.0`` wraps it
    in the same ``{"ok", "value"}`` result envelope as ``name``, while ``0.8.0`` returns
    it flat. A single entry mixes both conventions -- ``name`` is enveloped on either
    build -- so this unwraps per field rather than per version.

    Returns ``{}`` when the entry carries no revision at all, and ``None`` when one is
    present but its envelope reports failure: "could not be read" is not the same as
    "not there", and only the former means the remote copy cannot be verified.
    """
    rev = entry.get("activeRevision")
    if rev is None:
        return {}
    if not isinstance(rev, dict):
        return None
    if "ok" in rev or "value" in rev:
        if not rev.get("ok"):
            return None
        value = rev.get("value")
        return value if isinstance(value, dict) else None
    return rev


def claimed_identity(entry: dict) -> tuple[int | None, str | None]:
    """``(claimed_size, sha1)`` for a file entry: its plaintext size and content digest.

    Both are ``None`` when the revision is absent or unreadable, which every caller must
    treat as "cannot verify this remote copy" rather than as a passing check.
    """
    rev = active_revision(entry)
    if not rev:
        return None, None
    digests = rev.get("claimedDigests") or {}
    return rev.get("claimedSize"), digests.get("sha1")


def revision_uid(entry: dict) -> str | None:
    """The uid of a `filesystem list` entry's active revision, or None if unreadable.

    Drive keeps a version history per file: an upload with ``-f merge`` adds a revision to
    the existing node, so this uid changes whenever the file's content does, while the
    node itself stays put.

    .. versionadded:: 2.1.0
    """
    rev = active_revision(entry)
    if not rev:
        return None
    uid = rev.get("uid")
    return uid if isinstance(uid, str) and uid else None


def decrypted_name(entry: dict) -> str | None:
    """The decrypted filename of a `filesystem list` entry, or None if its name could
    not be decrypted (``name.ok`` is false). Central helper so every consumer parses
    the ``{"name": {"ok": bool, "value": str}}`` shape identically."""
    name = entry.get("name", {})
    if not name.get("ok"):
        return None
    return name.get("value")


class DriveClient:
    """Thin wrapper over the ``proton-drive`` CLI: run commands, parse JSON, classify errors.

    Each method shells out to the binary (``$PROTONFS_DRIVE_BIN`` or ``proton-drive``),
    lazily bootstrapping the keyring environment on first use, and translates non-zero
    exits into typed :class:`DriveError`\\ s. Listing and transfers ride out transient
    API throttling with bounded exponential backoff.
    """

    def __init__(self, binary: str | None = None) -> None:
        """Construct a client bound to a proton-drive binary.

        :param binary: an explicit binary path, or ``None`` to resolve via
            :func:`binary_path` (``$PROTONFS_DRIVE_BIN`` or the default).
        """
        self._binary = binary or binary_path()
        self._env: dict[str, str] | None = None
        self._renamed_strategies: bool | None = None
        self._clock_checked = False
        self._clock_offset: float | None = None

    def _check_clock(self) -> None:
        """Refuse to upload from a clock running ahead of Proton's; measured once per client.

        :raises DriveClockSkewError: if :func:`proton_clock_offset` puts this host more
            than :data:`CLOCK_AHEAD_LIMIT_SECONDS` ahead. An unmeasurable offset passes.
        """
        if self._clock_checked:
            return
        self._clock_offset = proton_clock_offset()
        if self._clock_offset is not None and self._clock_offset > CLOCK_AHEAD_LIMIT_SECONDS:
            raise DriveClockSkewError(self._clock_offset)
        self._clock_checked = True

    @property
    def binary(self) -> str:
        """The proton-drive binary path/name this client invokes."""
        return self._binary

    def binary_available(self) -> bool:
        """Whether the binary exists at all -- distinct from whether it *runs*. On a
        host with no keyring it exists and fails, and conflating the two tells users
        to reinstall a binary that is already there."""
        return shutil.which(self._binary) is not None or Path(self._binary).exists()

    def _binary_available(self) -> bool:
        """Internal alias for :meth:`binary_available` (kept for call-site stability)."""
        return self.binary_available()

    def _drive_env(self) -> dict[str, str]:
        """Environment for proton-drive, with the credentials store resolved on
        first use (Secret Service bus for keychain, or the managed pass store).

        Resolved lazily and cached for the process: on a headless host the first
        call may launch a session bus, and doing that at import time would charge
        every `protonfs --help` for it.
        """
        from protonfs.credstore import drive_env

        if self._env is None:
            self._env = drive_env()
        return self._env

    def _invoke(self, args: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
        """Run ``proton-drive <args> --json`` and return the completed process.

        :param args: the subcommand + arguments (``--json`` is appended).
        :param timeout: per-call timeout in seconds, or ``None`` for none.
        :returns: the :class:`subprocess.CompletedProcess` (caller inspects returncode).
        :raises DriveError: when the binary is not available.
        :raises subprocess.TimeoutExpired: when the call exceeds ``timeout``.
        """
        if not self._binary_available():
            raise DriveError(f"proton-drive binary not found: {self._binary}")
        logger.debug("invoke %s", _summarize_command([self._binary, *args, "--json"]))
        result = subprocess.run(
            [self._binary, *args, "--json"],
            capture_output=True,
            text=True,
            env=self._drive_env(),
            timeout=timeout,
        )
        if backend_passthrough_enabled() and result.stderr:
            logger.debug("proton-drive stderr: %s", result.stderr.strip())
        return result

    def _run_json(self, args: list[str], timeout: float | None = None) -> dict | list:
        """Run a JSON subcommand and return its parsed output, classifying failures.

        :param args: the subcommand + arguments.
        :param timeout: per-call timeout in seconds, or ``None``.
        :returns: the parsed JSON (dict or list).
        :raises DriveError: (or a subclass, via :func:`_classify`) on a non-zero exit or
            unparseable output.
        """
        result = self._invoke(args, timeout=timeout)
        stdout = result.stdout.strip()
        try:
            parsed = json.loads(stdout) if stdout else {}
        except json.JSONDecodeError as exc:
            # #136: proton-drive prints a raw (non-JSON) blob for a locked cache, so the
            # failure arrives HERE rather than via the normal non-zero-exit path. Without
            # this it was reported as "unparseable output", which named the symptom and
            # not the cause. stderr is checked too, since which stream carries it varies.
            blob = f"{stdout}\n{result.stderr}"
            if _is_lock_error(blob):
                raise DriveLockError(_LOCK_ERROR_HINT) from exc
            raise DriveError(f"unparseable output from proton-drive: {stdout!r}") from exc
        if result.returncode != 0:
            message = json.dumps(parsed) if parsed else result.stderr.strip()
            raise _classify(message)
        return parsed

    def _run_transfer(self, args: list[str], timeout: float | None = None) -> TransferResult:
        """Run a transfer subcommand and parse its result into a :class:`TransferResult`.

        :param args: the ``filesystem upload``/``download`` argv.
        :param timeout: per-call timeout in seconds, or ``None``.
        :returns: the parsed transfer summary.
        :raises DriveError: (or a subclass) on a non-zero exit or unparseable output.
        """
        result = self._invoke(args, timeout=timeout)
        stdout = result.stdout.strip()
        # A crash prints the one-line summary, then its stack trace, on stdout.
        summary, _, trailer = stdout.partition("\n")
        try:
            parsed = json.loads(stdout) if stdout else None
            trailer = ""
        except json.JSONDecodeError:
            try:
                parsed = json.loads(summary)
            except json.JSONDecodeError:
                parsed = None
        if not isinstance(parsed, dict) or "transferredItems" not in parsed:
            message = result.stderr.strip() or stdout
            raise _classify(message)
        transfer = TransferResult.from_json(parsed)
        # proton-drive prints its summary even when the command died before moving
        # anything (a path that matched nothing, a file gone since the scan), then exits
        # 1. A non-zero exit with no item failures is that case: the summary's zeros do
        # not mean "nothing needed sending".
        if result.returncode != 0 and not transfer.failed_items:
            raise _classify(_fatal_line(result.stderr, trailer))
        return transfer

    def _run_transfer_with_backoff(
        self,
        args: list[str],
        *,
        timeout: float = TRANSFER_TIMEOUT_SECONDS,
        retries: int = TRANSFER_MAX_RETRIES,
        base_delay: float = TRANSFER_BACKOFF_BASE_SECONDS,
        cap: float = TRANSFER_BACKOFF_CAP_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        payload_bytes: int | None = None,
    ) -> TransferResult:
        """`_run_transfer`, but resilient to Proton API throttling (#69), parity with
        `list_with_backoff` (#33).

        The first attempt is bounded by :func:`transfer_timeout` of `timeout` and
        `payload_bytes`. A transient throttle error is retried with exponential backoff;
        a timeout is retried at once with the timeout doubled (#184). Both count against
        `retries`; past it, a throttle raises `DriveThrottleError` and a timeout raises
        `DriveTransferTimeoutError`. A genuine non-throttle failure (auth, quota, missing
        path) is raised immediately, not retried. Retries re-run the whole
        `filesystem upload`/`download` invocation; proton-drive skips the files that
        already landed, so only unfinished ones are sent again.
        """
        timeout = transfer_timeout(timeout, payload_bytes)
        describe = f"`{_summarize_command(args)}`"
        # #184: a timeout is not throttling. proton-drive skips files that already landed
        # with the same content, and a killed upload leaves no partial node, so a retry
        # resends only what was cut off; give it twice the time, without a backoff sleep.
        current = {"timeout": timeout}

        def attempt() -> TransferResult:
            try:
                return self._run_transfer(args, timeout=current["timeout"])
            except subprocess.TimeoutExpired as exc:
                timed_out = current["timeout"]
                current["timeout"] = timed_out * 2
                raise _TransferTimedOut(timed_out) from exc

        try:
            return _retry_with_backoff(  # type: ignore[return-value]
                attempt,
                describe=describe,
                retries=retries,
                base_delay=base_delay,
                cap=cap,
                sleep=sleep,
            )
        except _TransferTimedOut as exc:
            from protonfs.commands.ls import human_size  # ls imports this module

            size = f" moving {human_size(payload_bytes)}" if payload_bytes else ""
            raise DriveTransferTimeoutError(
                f"{describe} timed out after {exc.seconds:.0f}s{size}, on all "
                f"{retries + 1} attempts. Raise PROTONFS_TRANSFER_TIMEOUT or lower "
                "PROTONFS_TRANSFER_MIN_RATE (bytes/s), or lower PROTONFS_BATCH_SIZE so "
                "each batch carries less."
            ) from exc

    def version(self) -> str | None:
        """Return the ``proton-drive version`` banner, or ``None`` if it can't run.

        Reading stops after the ``Proton Drive SDK`` line and the process is killed,
        so the binary's own update check (cli-drive 0.8.0+) is never waited on.

        :returns: the banner lines, e.g. ``"Proton Drive CLI cli-drive@0.9.0+…\\nProton
            Drive SDK js@0.22.2+…"``, or ``None`` when the binary is missing or exits
            non-zero before finishing the banner (e.g. a keyring fault).

        .. versionchanged:: 2.5.0
           Returns the banner only, without the update hint cli-drive 0.8.0+ prints
           after it.

        .. seealso:: :meth:`drive_version` for the parsed semver used by the support matrix.
        """
        if not self._binary_available():
            return None
        lines: list[str] = []
        with subprocess.Popen(
            [self._binary, "version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=self._drive_env(),
        ) as proc:
            for line in proc.stdout:
                lines.append(line)
                if line.startswith(_VERSION_BANNER_END):
                    proc.kill()
                    return "".join(lines).strip()
        if proc.returncode != 0:
            return None
        return "".join(lines).strip()

    def drive_version(self) -> str | None:
        """The installed proton-drive's semver, e.g. "0.5.0", or None if the binary
        is missing, unrunnable, or its `version` output doesn't contain a parseable
        `cli-drive@X.Y.Z` token (issue #65: for comparison against the support
        matrix in `protonfs.install`)."""
        raw = self.version()
        if raw is None:
            return None
        match = _VERSION_RE.search(raw)
        return match.group(1) if match else None

    def _strategy(self, command: str, option: str, name: str) -> str:
        """Translate a conflict-strategy name into the installed binary's vocabulary (#179).

        :param command: ``"upload"`` or ``"download"``.
        :param option: ``"-f"`` (files) or ``"-d"`` (folders).
        :param name: the strategy in protonfs's (cli-drive 0.4.6-0.7.0) vocabulary.
        :returns: the name the binary accepts. An unreadable version passes the name
            through and is asked again next time, so a transient fault is not cached.
        """
        if self._renamed_strategies is None:
            version = self.drive_version()
            if version is None:
                return name
            parts = tuple(int(p) for p in version.split("."))
            self._renamed_strategies = parts >= _STRATEGY_RENAMES_FROM
        if not self._renamed_strategies:
            return name
        return _STRATEGY_RENAMES[(command, option)].get(name, name)

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
        """List the immediate children of a remote directory (raw CLI entry dicts).

        :param remote_path: the absolute Drive directory to list.
        :param timeout: per-call timeout in seconds, or ``None``.
        :returns: the raw ``filesystem list`` entries (empty list if not a list).
        :raises DriveError: on a Drive failure.

        .. seealso:: :meth:`list_with_backoff` for the throttle-resilient variant used
            by the recursive walk.
        """
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
        return _retry_with_backoff(  # type: ignore[return-value]
            lambda: self.list(remote_path, timeout=timeout),
            describe=f"`list {remote_path}`",
            retries=retries,
            base_delay=base_delay,
            cap=cap,
            sleep=sleep,
        )

    def remote_identities(
        self,
        remote_parent: str,
        *,
        timeout: float = LIST_TIMEOUT_SECONDS,
        retries: int = LIST_MAX_RETRIES,
        base_delay: float = LIST_BACKOFF_BASE_SECONDS,
        cap: float = LIST_BACKOFF_CAP_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> dict[str, RemoteIdentity]:
        """Map decrypted filename -> plaintext identity for the files directly under
        `remote_parent`. Folders and entries with an undecryptable name are skipped.

        This is the single primitive every local-vs-remote comparison should route through
        (verify-after-push, offload-before-delete, cross-client drift): it reads the
        plaintext `claimedSize`/`claimedDigests.sha1`, never the encrypted `totalStorageSize`.

        The remote listing goes through :meth:`list_with_backoff`, so a throttled Drive
        (the degrade-then-timeout signature, #33) is retried with bounded backoff instead
        of hanging on a single unbounded ``filesystem list``. Past the retry budget it
        raises :class:`DriveThrottleError` rather than blocking indefinitely.

        .. versionchanged:: 1.6.0
           Verify listing is now throttle-resilient (was an unbounded ``list`` that could
           hang forever when Drive throttled the verify-after-push step).

        .. versionchanged:: 1.11.0
           Warns once per process when the installed proton-drive supplies no plaintext
           metadata at all, because verification then degrades to name-presence-only (#137).
        """
        identities: dict[str, RemoteIdentity] = {}
        entries = self.list_with_backoff(
            remote_parent,
            timeout=timeout,
            retries=retries,
            base_delay=base_delay,
            cap=cap,
            sleep=sleep,
        )
        for entry in entries:
            if entry.get("type") == "folder":
                continue
            name = decrypted_name(entry)
            if name is None:
                continue
            claimed_size, sha1 = claimed_identity(entry)
            identities[name] = RemoteIdentity(
                claimed_size=claimed_size, sha1=sha1, revision=revision_uid(entry)
            )
        # bound method, NOT called: only resolved if a warning is actually emitted
        _warn_if_verification_degraded(identities, self.drive_version)
        return identities

    def walk(
        self,
        remote_root: str,
        on_directory: Callable[[list[RemoteEntry]], None] | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        frontier: list[tuple[str, str]] | None = None,
        on_progress: Callable[[list[tuple[str, str]]], None] | None = None,
    ) -> list[RemoteEntry]:
        """Breadth-first walk of the remote tree.

        Each directory is listed with throttle backoff (#33). If `on_directory` is given it
        is called with that directory's FILE entries right after the directory is listed, so
        a caller (refresh) can record progress as it goes and keep it when a later directory
        wedges past the retry budget (refresh saves it on that error).

        Resumability (#33 item 2): pass `frontier` to seed the BFS queue from a saved state
        instead of the root, and `on_progress` to be handed the queue's current contents
        after each directory is listed and its children enqueued -- so a caller can persist
        the frontier and resume an interrupted walk from where it stopped.
        """
        root = remote_root.rstrip("/")
        results: list[RemoteEntry] = []
        # queue of (absolute remote path, rel prefix); deque gives O(1) popleft. Resume from
        # a saved frontier when given, else start at the root.
        queue: deque[tuple[str, str]] = (
            deque(frontier) if frontier is not None else deque([(root, "")])
        )
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
                    claimed_size, sha1 = claimed_identity(entry)
                    file_entry = RemoteEntry(
                        rel_path=rel,
                        is_dir=False,
                        size=entry.get("totalStorageSize", 0),
                        claimed_size=claimed_size,
                        sha1=sha1,
                        revision=revision_uid(entry),
                    )
                    results.append(file_entry)
                    dir_files.append(file_entry)
            if on_directory is not None:
                on_directory(dir_files)
            if on_progress is not None:
                # Hand out the remaining queue (this dir popped, its children enqueued) so
                # the caller can persist the frontier for resume.
                on_progress(list(queue))
        return results

    def upload(
        self,
        local_paths: list[Path],
        remote_parent: str,
        file_strategy: str | None = None,
        folder_strategy: str | None = None,
        *,
        skip_thumbnails: bool = False,
        timeout: float = TRANSFER_TIMEOUT_SECONDS,
        retries: int = TRANSFER_MAX_RETRIES,
        base_delay: float = TRANSFER_BACKOFF_BASE_SECONDS,
        cap: float = TRANSFER_BACKOFF_CAP_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> TransferResult:
        """Upload local files/folders into a remote parent, riding out throttling.

        :param local_paths: local files/directories to upload.
        :param remote_parent: the absolute Drive directory to upload into.
        :param file_strategy: proton-drive ``-f`` conflict strategy for files, or ``None``.
        :param folder_strategy: proton-drive ``-d`` strategy for folders, or ``None``.
        :param skip_thumbnails: upload without thumbnails (proton-drive's ``-t``), for
            images this host has no codec to build one for (see
            :func:`is_thumbnail_failure`).
        :param timeout: first-attempt timeout (seconds), raised to fit the files' total
            size by :func:`transfer_timeout` and doubled on each timed-out retry.
        :param retries: max throttle or timeout retries before giving up.
        :param base_delay: base backoff delay (seconds), doubled each retry.
        :param cap: maximum backoff delay (seconds).
        :param sleep: sleep function (injectable for tests).
        :returns: the :class:`TransferResult` summary.
        :raises DriveThrottleError: when throttling outlasts the retry budget.
        :raises DriveTransferTimeoutError: when every attempt timed out.
        :raises DriveClockSkewError: before sending anything, when this host's clock is
            more than :data:`CLOCK_AHEAD_LIMIT_SECONDS` ahead of Proton's (measured once
            per client), or when every file in the call failed on a signature dated in
            the future.
        :raises DriveError: on a non-throttle Drive failure (raised immediately).

        .. versionchanged:: 2.5.0
           The timeout scales with the files' total size, a timeout is retried with a
           doubled timeout and reported as :class:`DriveTransferTimeoutError` rather than
           as throttling (#184). Added ``skip_thumbnails`` (#183).

        .. versionchanged:: 3.3.0
           Checks the clock against Proton's first (:class:`DriveClockSkewError`).

        .. seealso:: :meth:`download` for the reverse direction.
        """
        sendable: list[Path] = []
        literals: list[str] = []
        unnameable: list[dict] = []
        for path in local_paths:
            literal = _glob_literal(str(path))
            if literal is None:
                unnameable.append({"name": path.name, "error": _UNNAMEABLE})
            else:
                sendable.append(path)
                literals.append(literal)
        if not sendable:
            return TransferResult(0, 0, len(unnameable), unnameable)
        self._check_clock()
        args = ["filesystem", "upload"]
        if file_strategy:
            args += ["-f", self._strategy("upload", "-f", file_strategy)]
        if folder_strategy:
            args += ["-d", self._strategy("upload", "-d", folder_strategy)]
        if skip_thumbnails:
            args.append("-t")
        args += literals + [remote_parent]
        result = self._run_transfer_with_backoff(
            args, timeout=timeout, retries=retries, base_delay=base_delay, cap=cap, sleep=sleep,
            payload_bytes=_payload_bytes(sendable),
        )
        if (
            result.failures
            and not result.transferred_items
            and all(_is_signature_time_error(f) for f in result.failures)
        ):
            raise DriveClockSkewError(self._clock_offset)
        if unnameable:
            result.failed_items += len(unnameable)
            result.failures = [*result.failures, *unnameable]
        return result

    def download(
        self,
        remote_paths: list[str],
        local_folder: Path,
        file_strategy: str | None = None,
        folder_strategy: str | None = None,
        *,
        timeout: float = TRANSFER_TIMEOUT_SECONDS,
        retries: int = TRANSFER_MAX_RETRIES,
        base_delay: float = TRANSFER_BACKOFF_BASE_SECONDS,
        cap: float = TRANSFER_BACKOFF_CAP_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        payload_bytes: int | None = None,
    ) -> TransferResult:
        """Download remote files/folders into a local folder, riding out throttling.

        :param remote_paths: absolute Drive paths to download.
        :param local_folder: the local directory to download into.
        :param file_strategy: proton-drive ``-f`` conflict strategy for files, or ``None``.
        :param folder_strategy: proton-drive ``-d`` strategy for folders, or ``None``.
        :param timeout: first-attempt timeout (seconds), raised to fit ``payload_bytes``
            by :func:`transfer_timeout` and doubled on each timed-out retry.
        :param retries: max throttle or timeout retries before giving up.
        :param base_delay: base backoff delay (seconds), doubled each retry.
        :param cap: maximum backoff delay (seconds).
        :param sleep: sleep function (injectable for tests).
        :param payload_bytes: expected total size of the download, or ``None`` if unknown.
        :returns: the :class:`TransferResult` summary.
        :raises DriveThrottleError: when throttling outlasts the retry budget.
        :raises DriveTransferTimeoutError: when every attempt timed out.
        :raises DriveError: on a non-throttle Drive failure (raised immediately).

        .. versionchanged:: 2.5.0
           Added ``payload_bytes``; a timeout is retried with a doubled timeout and
           reported as :class:`DriveTransferTimeoutError` rather than as throttling (#184).

        .. seealso:: :meth:`upload` for the reverse direction.
        """
        folder = _glob_literal(str(local_folder))
        if folder is None:
            raise DriveError(
                f"cannot download into {local_folder}: proton-drive reads that path as a "
                "glob pattern, and one holding a backslash or a {a,b} brace list cannot be "
                "escaped for it; move the store to a path without them"
            )
        args = ["filesystem", "download"]
        if file_strategy:
            args += ["-f", self._strategy("download", "-f", file_strategy)]
        if folder_strategy:
            args += ["-d", self._strategy("download", "-d", folder_strategy)]
        args += remote_paths + [folder]
        return self._run_transfer_with_backoff(
            args, timeout=timeout, retries=retries, base_delay=base_delay, cap=cap, sleep=sleep,
            payload_bytes=payload_bytes,
        )

    def trash(self, remote_paths: list[str]) -> list[dict]:
        """Move remote nodes to the trash (recoverable via :meth:`restore`).

        :param remote_paths: absolute Drive paths to trash.
        :returns: the raw per-node CLI results.
        :raises DriveError: on a Drive failure.

        .. seealso:: :meth:`restore` (undo) and :meth:`delete` (permanent).
        """
        result = self._run_json(["filesystem", "trash", *remote_paths])
        return result if isinstance(result, list) else []

    # Like trash and create-folder, move and rename run without a timeout: killing one
    # part-way would leave it unknown which nodes moved. Callers check the listing after.

    def move(self, remote_paths: list[str], target_parent: str) -> list[dict]:
        """Move remote nodes into the folder ``target_parent``, keeping their names.

        The node itself moves, so its revision history and share links go with it.

        :param remote_paths: absolute Drive paths of the nodes to move.
        :param target_parent: absolute Drive path of the folder to move them into; it
            must exist.
        :returns: the raw per-node CLI results. A node that could not be moved (its name
            is taken in the target, or it is gone) may be reported here with ``ok`` false
            rather than raised, so check a listing to learn what moved.
        :raises DriveError: when the command itself fails.

        .. versionadded:: 3.0.0

        .. seealso:: :meth:`rename` to change a node's name in place.
        """
        result = self._run_json(["filesystem", "move", *remote_paths, target_parent])
        return result if isinstance(result, list) else []

    def rename(self, remote_path: str, new_name: str) -> dict:
        """Rename a remote node in place (it stays in the same folder).

        :param remote_path: absolute Drive path of the node.
        :param new_name: its new name (a name, not a path).
        :returns: the raw CLI result.
        :raises DriveError: when the command fails or reports the node as not renamed
            (e.g. the name is taken).

        .. versionadded:: 3.0.0
        """
        result = self._run_json(["filesystem", "rename", remote_path, new_name])
        items = result if isinstance(result, list) else [result]
        for item in items:
            if isinstance(item, dict) and item.get("ok") is False:
                raise DriveError(f"rename of {remote_path} failed: {json.dumps(item)}")
        return items[0] if items and isinstance(items[0], dict) else {}

    def restore(self, remote_paths: list[str]) -> list[dict]:
        """Restore trashed nodes given their ORIGINAL remote paths.

        proton-drive 0.5.0 removed original-path restore: `filesystem restore` only
        accepts `/trash/<name>` paths, resolved by decrypted name with first-match-
        wins, and node UIDs are NOT accepted under /trash (#56). The original-path
        form is tried first (it is what 0.4.6 accepts); on rejection each path is
        translated to its trash entry, refusing to act when a stale same-named
        entry would be matched instead of the requested one.
        """
        try:
            result = self._run_json(["filesystem", "restore", *remote_paths])
            return result if isinstance(result, list) else []
        except DriveError as exc:
            if "not supported" not in str(exc):
                raise
        return [self._restore_from_trash(path) for path in remote_paths]

    def _node_uid(self, remote_path: str) -> str | None:
        """The node UID of `remote_path`, via `filesystem info`."""
        result = self._run_json(["filesystem", "info", remote_path])
        return result.get("uid") if isinstance(result, dict) else None

    def _restore_from_trash(self, original_path: str) -> dict:
        """Restore one node by translating its original path to a `/trash/<name>`
        path, guarding against proton-drive's first-match-wins name resolution."""
        stripped = original_path.rstrip("/")
        parent, _, name = stripped.rpartition("/")
        same_named = [e for e in self.list("/trash") if decrypted_name(e) == name]
        if not same_named:
            raise DriveError(f"cannot restore {original_path}: no trashed item is named {name!r}")
        parent_uid = self._node_uid(parent or "/")
        candidate = same_named[0]
        if candidate.get("parentUid") != parent_uid:
            # The first same-named trash entry (which is what proton-drive would
            # act on) did not come from this path's parent — restoring would hit
            # the wrong node, and UIDs cannot disambiguate under /trash (#56).
            raise DriveError(
                f"cannot restore {original_path}: {len(same_named)} trashed items "
                f"are named {name!r} and proton-drive >= 0.5.0 restores trash "
                f"entries by name, first match wins; the first match is not the "
                f"one trashed from {parent or '/'}. Run `protonfs trash list` to see "
                f"the duplicates, then restore it via the Drive web UI, or resolve the "
                f"ambiguity with `protonfs trash empty` (irreversible, account-global) "
                f"or by permanently deleting the older same-named trash entries first."
            )
        escaped = name.replace("/", "\\/")
        result = self._run_json(["filesystem", "restore", f"/trash/{escaped}"])
        items = result if isinstance(result, list) else []
        restored = items[0] if items else {}
        if restored.get("uid") != candidate.get("uid") or not restored.get("ok"):
            raise DriveError(
                f"restore of {original_path} failed: "
                f"{json.dumps(restored) if restored else 'no result from proton-drive'}"
            )
        return restored

    def delete(self, remote_paths: list[str]) -> list[dict]:
        """Permanently delete remote nodes (only valid for nodes already in ``/trash``).

        :param remote_paths: absolute ``/trash`` paths to delete permanently.
        :returns: the raw per-node CLI results.
        :raises DriveError: on a Drive failure (including deleting a non-trashed node).

        .. warning:: Irreversible. proton-drive only permits permanent deletion of nodes
            that are already trashed.
        """
        result = self._run_json(["filesystem", "delete", *remote_paths])
        return result if isinstance(result, list) else []

    def parent_name(self, parent_uid: str) -> str | None:
        """Best-effort decrypted name of a trashed node's original parent, given its
        ``parentUid`` from a `/trash` listing entry. proton-drive accepts a bare node
        UID in place of a path segment (per `filesystem info --help`), so this is
        tried directly; returns None on any failure or undecryptable name so callers
        (``protonfs trash list``) can show "(unknown)" rather than fail the listing.
        """
        try:
            result = self._run_json(["filesystem", "info", parent_uid])
        except DriveError:
            return None
        return decrypted_name(result) if isinstance(result, dict) else None

    def empty_trash(self) -> None:
        """Permanently empty /trash for the whole account. Irreversible and NOT
        scoped to this repo's remote_root -- callers must confirm explicitly before
        calling this (see `protonfs trash empty`)."""
        self._run_json(["filesystem", "empty-trash"])

    def create_folder(self, parent_path: str, name: str) -> dict:
        """Create a single folder ``name`` under ``parent_path`` on Drive.

        :param parent_path: the absolute Drive directory to create the folder in.
        :param name: the new folder's name.
        :returns: the raw CLI result dict.
        :raises DriveError: on a Drive failure (an "already exists" error surfaces here;
            callers building nested paths catch and ignore it).
        """
        result = self._run_json(["filesystem", "create-folder", parent_path, name])
        return result if isinstance(result, dict) else {}
