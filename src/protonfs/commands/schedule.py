"""``protonfs schedule``: install/list/remove cron jobs that run protonfs on a schedule.

Encodes the operational lessons of running protonfs from cron by hand (they are easy to
get wrong and fail silently): cron's PATH is stripped so ``proton-drive`` must be found by
absolute path; concurrent runs must be prevented with ``flock``; the remote-list timeout
must sit well above proton-drive's ~30s+ per-call startup or verify times out into a retry
storm; and everything must be logged. ``schedule`` generates a wrapper script per job that
bakes all of this in, so the user just picks a cadence.

Each job gets a short hex id (e.g. ``a1d3ae``). The job manifest lives in
``.protonfs/schedule.local.json`` (per-device, gitignored -- the schedule lives on THIS
machine). One crontab line per job carries a ``# protonfs-schedule:<id>`` marker so we only
ever touch our own lines. The ``crontab`` runner is injectable for testing.

Commands: ``push``, ``pull``, ``sync`` (pull then push), ``offload`` (push, then offload
the same scope) and ``prune`` (``protonfs prune``, which pushes first itself). Offload
and prune honour a settle window (``min_age``) and prune a per-directory ``keep``.

Locking: every wrapper takes two ``flock`` locks. Its own (non-blocking) keeps a job
from overlapping itself; the repo's shared schedule lock (waited for, up to
:data:`ROOT_LOCK_WAIT_SECONDS`) keeps different jobs on one repo from running at the
same time, so a prune can never overlap a scheduled push. A job that cannot get the
shared lock in time logs that it skipped and exits 0; its next run tries again.

Conflicts: :func:`check_conflicts` refuses (or warns about) a new job that would fight
an existing job over overlapping files -- see its docstring for the rules.

.. versionadded:: 1.8.0

.. versionchanged:: 2.2.0
   Added the ``offload`` and ``prune`` commands, conflict checks when adding a job, and
   the shared per-repo lock (#158).
"""
from __future__ import annotations

import fnmatch
import json
import re
import secrets
import shutil
import stat
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from protonfs.config import CONFIG_DIR_NAME, config_path
from protonfs.drive import binary_path

MANIFEST_FILE_NAME = "schedule.local.json"
MANIFEST_SCHEMA_VERSION = 1
MARKER = "# protonfs-schedule:"

# Tuned defaults, learned from running this from cron over a rate-limited link: a list
# timeout comfortably above proton-drive's per-call startup, a generous transfer timeout,
# and a modest batch so each upload call stays under it.
DEFAULT_LIST_TIMEOUT = 120
DEFAULT_TRANSFER_TIMEOUT = 1200
DEFAULT_BATCH_SIZE = 50

COMMANDS = ("push", "pull", "sync", "offload", "prune")

# How long a job waits for another job on the same repo before skipping this run. Long
# enough to ride out an ordinary push; bounded so a stuck job cannot queue waiters
# forever (each waits at most this long, then logs a skip).
ROOT_LOCK_WAIT_SECONDS = 1800
ROOT_LOCK_NAME = "repo.lock"


class ScheduleError(RuntimeError):
    """A schedule operation could not be completed (bad cadence, no crontab, unknown id)."""


@dataclass
class ScheduledJob:
    """One installed scheduled job (mirrors an entry in the manifest)."""

    id: str
    cron: str
    command: str
    path: str | None
    resolve: str | None
    repo: str
    list_timeout: int
    transfer_timeout: int
    batch_size: int
    label: str
    created_at: str
    log_path: str
    wrapper_path: str = ""
    # #131: passed through to push/pull as --strict. Defaulted (and declared last) so a
    # manifest written before this field existed still loads via ScheduledJob(**data).
    strict: bool = False
    # #158: offload/prune settle window (e.g. "1d") and prune's per-directory keep.
    # Defaulted for the same reason as `strict`; None means the command's own default.
    min_age: str | None = None
    keep: int | None = None


def _default_runner(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    """Run a ``crontab`` command, capturing output. Injected in tests."""
    return subprocess.run(
        args, input=stdin, capture_output=True, text=True, check=False
    )


def _schedule_dir(repo_root: Path) -> Path:
    return repo_root / CONFIG_DIR_NAME / "schedule"


def _manifest_path(repo_root: Path) -> Path:
    return repo_root / CONFIG_DIR_NAME / MANIFEST_FILE_NAME


def _load_manifest(repo_root: Path) -> dict[str, dict]:
    path = _manifest_path(repo_root)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (ValueError, OSError):
        return {}
    if not isinstance(raw, dict) or raw.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        return {}
    jobs = raw.get("jobs")
    return jobs if isinstance(jobs, dict) else {}


def _save_manifest(repo_root: Path, jobs: dict[str, dict]) -> None:
    path = _manifest_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {"schema_version": MANIFEST_SCHEMA_VERSION, "jobs": jobs}
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")


def _new_id(existing: dict) -> str:
    """A short hex id, unique within the manifest."""
    while True:
        candidate = secrets.token_hex(3)
        if candidate not in existing:
            return candidate


_CRON_FIELD = r"[\d*/,\-]+"
_CRON_RE = re.compile(rf"^\s*{_CRON_FIELD}(\s+{_CRON_FIELD}){{4}}\s*$")


def cadence_to_cron(every: str | None, cron: str | None, at: str | None) -> str:
    """Resolve a friendly cadence to a 5-field cron expression.

    :param every: ``hourly`` | ``daily`` | ``weekly`` | ``<N>h`` | ``<N>m``.
    :param cron: a raw 5-field cron expression (takes precedence; validated).
    :param at: comma-separated hours (0-23), e.g. ``"1,3,5"`` -- runs daily at those hours,
        and overrides the hour of an ``--every daily``/``weekly``.
    :raises ScheduleError: on an invalid or missing cadence.
    """
    if cron:
        if not _CRON_RE.match(cron):
            raise ScheduleError(f"invalid cron expression (need 5 fields): {cron!r}")
        return cron.strip()
    if at is not None:
        if not re.fullmatch(r"\d{1,2}(,\d{1,2})*", at) or any(
            int(h) > 23 for h in at.split(",")
        ):
            raise ScheduleError(f"invalid --at hours (0-23, comma-separated): {at!r}")
        return f"0 {at} * * *"
    if not every:
        raise ScheduleError("no cadence: pass --every, --cron, or --at")
    every = every.strip().lower()
    presets = {"hourly": "0 * * * *", "daily": "0 0 * * *", "weekly": "0 0 * * 0"}
    if every in presets:
        return presets[every]
    m = re.fullmatch(r"(\d+)([hm])", every)
    if not m:
        raise ScheduleError(
            f"unrecognised --every {every!r} (use hourly|daily|weekly|<N>h|<N>m)"
        )
    n, unit = int(m.group(1)), m.group(2)
    if unit == "h":
        if not 1 <= n <= 23:
            raise ScheduleError("--every <N>h needs 1..23 hours (use daily for 24h)")
        return f"0 */{n} * * *"
    if not 1 <= n <= 59:
        raise ScheduleError("--every <N>m needs 1..59 minutes")
    return f"*/{n} * * * *"


def _resolve_drive_bin() -> str:
    """Absolute path to proton-drive for the wrapper (cron has no useful PATH)."""
    binary = binary_path()
    found = shutil.which(binary)
    return found or binary


def _run_lines(job: ScheduledJob, protonfs_bin: str) -> str:
    """The protonfs invocations a job runs, in order."""
    exe = f'"{protonfs_bin}" -v'
    if job.command == "sync":
        return f"  {exe} pull{_args(job)}\n  {exe} push{_args(job)}\n"
    if job.command == "offload":
        # Push first, as a prune does: offload can only reclaim what is on Drive, and it
        # re-verifies every file against the live listing before deleting anything.
        return (
            f"  {exe} push{_args(job, transfer=True)}\n"
            f"  {exe} offload --yes{_args(job, retention=True)}\n"
        )
    if job.command == "prune":
        return f"  {exe} prune --yes{_args(job, transfer=True, retention=True)}\n"
    return f"  {exe} {job.command}{_args(job)}\n"


def _wrapper_text(job: ScheduledJob, protonfs_bin: str, drive_bin: str, lock_path: str) -> str:
    """Render the per-job wrapper script (the generated equivalent of a hand-rolled
    cron push script): absolute binaries, tuned env, flock, timestamped logging.

    Two locks: the job's own (``flock -n``: a job never overlaps itself) and the repo's
    shared schedule lock (``flock -w``: jobs on one repo take turns, waiting a bounded
    time rather than failing on protonfs's own repo lock).
    """
    py_bin_dir = str(Path(protonfs_bin).parent)
    drive_bin_dir = str(Path(drive_bin).parent)
    root_lock = str(Path(lock_path).parent / ROOT_LOCK_NAME)
    return (
        "#!/bin/bash\n"
        f"# protonfs scheduled job {job.id} -- generated by `protonfs schedule`.\n"
        "# Do not edit; re-create with `protonfs schedule --uninstall <id>` then `--add`.\n"
        f'export PATH="{py_bin_dir}:{drive_bin_dir}:$PATH"\n'
        f'export PROTONFS_DRIVE_BIN="{drive_bin}"\n'
        f"export PROTONFS_LIST_TIMEOUT={job.list_timeout}\n"
        f"export PROTONFS_TRANSFER_TIMEOUT={job.transfer_timeout}\n"
        f"export PROTONFS_BATCH_SIZE={job.batch_size}\n"
        f'cd "{job.repo}" || exit 1\n'
        "(\n"
        f'  flock -n 9 || {{ echo "===== skip $(date -u): job {job.id} running ====="; exit 0; }}\n'
        f"  flock -w {ROOT_LOCK_WAIT_SECONDS} 8 || "
        f'{{ echo "===== skip $(date -u): job {job.id}: another job on this repo held the '
        f'lock for {ROOT_LOCK_WAIT_SECONDS}s ====="; exit 0; }}\n'
        f'  echo "===== job {job.id} start $(date -u) ====="\n'
        "  rc=0\n"
        + "".join(
            f"{line} || rc=$?\n" for line in _run_lines(job, protonfs_bin).splitlines()
        )
        + f'  echo "===== job {job.id} end rc=$rc $(date -u) ====="\n'
        f') 9>"{lock_path}" 8>"{root_lock}" >> "{job.log_path}" 2>&1\n'
    )


def _args(job: ScheduledJob, *, transfer: bool = True, retention: bool = False) -> str:
    """CLI arguments for one invocation: the scope, plus transfer options (push/pull
    flags) and/or retention options (offload/prune flags) as that command accepts."""
    parts = ""
    if job.path:
        # Quoted deliberately: it keeps cron's shell from touching the value, so a glob
        # pattern reaches protonfs INTACT and is expanded by protonfs itself at run time
        # (#131). That is what lets one job cover a family of runs and keep matching as
        # new ones appear -- a shell glob would have been expanded once, at install time.
        parts += f' "{job.path}"'
    if transfer and not retention:
        if job.resolve:
            parts += f" --resolve={job.resolve}"
        if job.strict:
            parts += " --strict"
    if retention:
        if job.min_age:
            parts += f" --min-age={job.min_age}"
        if job.command == "prune" and job.keep is not None:
            parts += f" --keep={job.keep}"
    return parts


# --- conflict checks (#158) --------------------------------------------------------------

# What each scheduled command does to the files in its scope.
_OPERATIONS = {
    "push": {"push"},
    "pull": {"pull"},
    "sync": {"push", "pull"},
    "offload": {"push", "offload"},
    "prune": {"push", "prune"},
}
_REMOVES_LOCAL = ("offload", "prune")
_GLOB_CHARS = re.compile(r"[*?\[]")


@dataclass(frozen=True)
class Conflict:
    """A problem between a proposed job and one already scheduled.

    :ivar level: ``"error"`` (the job is refused) or ``"warning"`` (added, but reported).
    :ivar job_id: the existing job it clashes with.
    :ivar message: what clashes and what to do instead.
    """

    level: str
    job_id: str
    message: str


def _normalise_scope(path: str | None) -> str | None:
    """``None`` for the whole repo; otherwise the path without ``./`` or trailing ``/``."""
    if path is None:
        return None
    cleaned = path.strip()
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    cleaned = cleaned.strip("/")
    return cleaned or None if cleaned != "." else None


def _literal_ends(segment: str) -> tuple[str, str]:
    """The literal text before a glob segment's first wildcard and after its last."""
    positions = [m.start() for m in _GLOB_CHARS.finditer(segment)]
    last = max(segment.rfind("*"), segment.rfind("?"), segment.rfind("]"))
    return segment[: positions[0]], segment[last + 1 :]


def _segments_compatible(a: str, b: str) -> bool:
    """Whether two path segments (literal or glob) could name the same entry.

    Exact for a literal against a literal or a glob. For two globs it compares their
    literal prefixes and suffixes, which settles the usual cases (``mload*`` vs
    ``other*``, ``*.ev`` vs ``*.sink``) and otherwise errs towards "could overlap".
    """
    a_glob, b_glob = bool(_GLOB_CHARS.search(a)), bool(_GLOB_CHARS.search(b))
    if not a_glob and not b_glob:
        return a == b
    if not a_glob:
        return fnmatch.fnmatchcase(a, b)
    if not b_glob:
        return fnmatch.fnmatchcase(b, a)
    (pa, sa), (pb, sb) = _literal_ends(a), _literal_ends(b)
    return (pa.startswith(pb) or pb.startswith(pa)) and (sa.endswith(sb) or sb.endswith(sa))


def scope_relation(a: str | None, b: str | None) -> str:
    """How two job scopes (``--path`` values) relate: ``"identical"``, ``"overlap"`` or
    ``"disjoint"``.

    ``None`` is the whole repo. A scope is a directory, a file or a glob pattern, and a
    directory covers everything beneath it, so ``run1`` overlaps ``run1/*.ev`` but not
    ``run10``. Patterns are compared segment by segment; ``**`` can span segments, so a
    pattern containing it is treated as overlapping anything. Where the comparison
    cannot be sure, it reports an overlap: a false alarm is a message, a missed clash is
    two jobs fighting over the same files.
    """
    na, nb = _normalise_scope(a), _normalise_scope(b)
    if na == nb:
        return "identical"
    if na is None or nb is None or "**" in na or "**" in nb:
        return "overlap"
    for x, y in zip(na.split("/"), nb.split("/")):
        if not _segments_compatible(x, y):
            return "disjoint"
    return "overlap"


def _describe(path: str | None) -> str:
    return f"'{path}'" if _normalise_scope(path) else "the whole repo"


def check_conflicts(
    command: str, path: str | None, existing: list[ScheduledJob]
) -> list[Conflict]:
    """Every problem between a proposed job and the jobs already scheduled (pure).

    Only jobs whose scope overlaps the proposed one are compared. The rules:

    - **Duplicate** -- the same command on the identical scope: error.
    - **push + offload/prune** -- offload and prune jobs already push their scope
      first, so a separate push job on it is redundant and races them: error; schedule
      the offload/prune alone.
    - **push + pull** -- identical scope: error; overlapping scopes: warning. Both
      directions on the same files churn; ``sync`` does both in one job, in order.
      A ``sync`` job alongside a push or pull on the same files is judged the same way.
    - **offload/prune + pull (or sync)** -- one removes local copies the other brings
      back: error.
    - **offload + prune** -- offload would remove the newest files prune is keeping:
      error.
    - **Same command on overlapping (not identical) scopes** -- warning: the two
      repeat each other's work on the shared files.

    :param command: the proposed job's command.
    :param path: its scope (``None`` for the whole repo).
    :param existing: the jobs already scheduled for this repo.
    :returns: the conflicts found, errors and warnings, in ``existing`` order.
    """
    conflicts: list[Conflict] = []
    for job in existing:
        relation = scope_relation(path, job.path)
        if relation == "disjoint":
            continue
        pair = {command, job.command}
        mine, theirs = _OPERATIONS[command], _OPERATIONS[job.command]
        where = _describe(path) if relation == "identical" else (
            f"{_describe(path)} (overlapping job {job.id}'s {_describe(job.path)})"
        )
        if command == job.command and relation == "identical":
            conflicts.append(Conflict("error", job.id, (
                f"job {job.id} already runs `{command}` on {where}"
            )))
        elif pair & set(_REMOVES_LOCAL) and ("pull" in mine or "pull" in theirs):
            remover = next(c for c in (command, job.command) if c in _REMOVES_LOCAL)
            other = job.command if remover == command else command
            conflicts.append(Conflict("error", job.id, (
                f"`{remover}` and `{other}` on {where} fight each other: {remover} removes "
                f"local copies that {other} brings back"
            )))
        elif pair == {"offload", "prune"}:
            conflicts.append(Conflict("error", job.id, (
                f"`offload` and `prune` on {where} overlap: offload would remove the newest "
                "files prune keeps; schedule one of them"
            )))
        elif "push" in pair and pair & set(_REMOVES_LOCAL):
            remover = next(c for c in (command, job.command) if c in _REMOVES_LOCAL)
            conflicts.append(Conflict("error", job.id, (
                f"a `{remover}` job already pushes {where} before it removes anything; "
                f"schedule `{remover}` alone"
            )))
        elif ("push" in mine and "pull" in theirs) or ("pull" in mine and "push" in theirs):
            level = "error" if relation == "identical" else "warning"
            if "sync" in pair:
                advice = "a `sync` job already pulls and pushes; keep just that one"
            else:
                advice = "one `--command sync` job does both, pull then push"
            conflicts.append(Conflict(level, job.id, (
                f"`{command}` and job {job.id}'s `{job.command}` both move {where}, in "
                f"opposite directions; {advice}"
            )))
        elif command == job.command:
            conflicts.append(Conflict("warning", job.id, (
                f"job {job.id} already runs `{command}` on part of {where}; the two will "
                "repeat each other's work there"
            )))
    return conflicts


def is_protonfs_repo(repo_root: Path) -> bool:
    return config_path(repo_root).exists()


def add_job(
    repo_root: Path,
    *,
    every: str | None = None,
    cron: str | None = None,
    at: str | None = None,
    command: str = "push",
    path: str | None = None,
    resolve: str | None = None,
    strict: bool = False,
    min_age: str | None = None,
    keep: int | None = None,
    list_timeout: int = DEFAULT_LIST_TIMEOUT,
    transfer_timeout: int = DEFAULT_TRANSFER_TIMEOUT,
    batch_size: int = DEFAULT_BATCH_SIZE,
    label: str = "",
    protonfs_bin: str | None = None,
    runner=_default_runner,
    now: str | None = None,
    warn=None,
) -> ScheduledJob:
    """Install a scheduled job: write its wrapper, append a tagged crontab line, record it.

    :param path: a subtree, or a glob pattern (``mload*``, ``mload*/*.ev``). A pattern is
        written into the wrapper quoted, so cron's shell leaves it alone and protonfs
        expands it itself on every run -- meaning one job covers a whole family of runs
        and keeps matching as new ones appear (#131).
    :param strict: pass ``--strict`` to push/pull, so a run whose pattern matches nothing
        fails (exit 1) instead of being reported and skipped.
    :param min_age: settle window for ``offload``/``prune`` jobs (e.g. ``"1d"``);
        ``None`` uses the command's default.
    :param keep: newest files per directory a ``prune`` job keeps; ``None`` uses the
        command's default.
    :param warn: called with each warning-level conflict (the job is still added).
    :raises ScheduleError: on a non-repo, unknown command, bad cadence, missing crontab,
        an option the command does not take, or an error-level conflict with a job
        already scheduled (see :func:`check_conflicts`).

    .. versionchanged:: 1.11.0
       Added ``strict``; ``path`` may now be a glob pattern.

    .. versionchanged:: 2.2.0
       Added the ``offload``/``prune`` commands, ``min_age``, ``keep`` and ``warn``;
       conflicting jobs are refused (#158).
    """
    if not is_protonfs_repo(repo_root):
        raise ScheduleError(f"{repo_root} is not a protonfs repo (run `protonfs setup`).")
    if command not in COMMANDS:
        raise ScheduleError(f"unknown --command {command!r} (choose {', '.join(COMMANDS)}).")
    _check_options(command, resolve=resolve, strict=strict, min_age=min_age, keep=keep)
    conflicts = check_conflicts(command, path, list_jobs(repo_root))
    errors = [c.message for c in conflicts if c.level == "error"]
    if errors:
        raise ScheduleError("refusing to add this job:\n  " + "\n  ".join(errors))
    for conflict in conflicts:
        if warn is not None:
            warn(conflict.message)
    if shutil.which("crontab") is None:
        raise ScheduleError("`crontab` not found; cron scheduling is unavailable on this host.")
    cron_expr = cadence_to_cron(every, cron, at)

    jobs = _load_manifest(repo_root)
    job_id = _new_id(jobs)
    sched_dir = _schedule_dir(repo_root)
    job = ScheduledJob(
        id=job_id,
        cron=cron_expr,
        command=command,
        path=path,
        resolve=resolve,
        strict=strict,
        min_age=min_age,
        keep=keep,
        repo=str(repo_root.resolve()),
        list_timeout=list_timeout,
        transfer_timeout=transfer_timeout,
        batch_size=batch_size,
        label=label,
        created_at=now or _utcnow(),
        log_path=str(sched_dir / f"{job_id}.log"),
        wrapper_path=str(sched_dir / f"{job_id}.sh"),
    )

    protonfs_bin = protonfs_bin or shutil.which("protonfs") or "protonfs"
    drive_bin = _resolve_drive_bin()
    lock_path = str(sched_dir / f"{job_id}.lock")

    sched_dir.mkdir(parents=True, exist_ok=True)
    wrapper = Path(job.wrapper_path)
    wrapper.write_text(_wrapper_text(job, protonfs_bin, drive_bin, lock_path))
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    line = f'{cron_expr} "{job.wrapper_path}"  {MARKER}{job_id}'
    _crontab_install_line(line, runner)

    jobs[job_id] = asdict(job)
    _save_manifest(repo_root, jobs)
    return job


def _check_options(
    command: str, *, resolve: str | None, strict: bool, min_age: str | None, keep: int | None
) -> None:
    """Refuse options the scheduled command would silently ignore."""
    from protonfs.retention import RetentionError, parse_duration

    if (resolve or strict) and command == "prune":
        raise ScheduleError("--resolve/--strict apply to push/pull; a prune job does not take them")
    if min_age is not None:
        if command not in _REMOVES_LOCAL:
            raise ScheduleError("--min-age applies to offload and prune jobs only")
        try:
            parse_duration(min_age)
        except RetentionError as exc:
            raise ScheduleError(str(exc)) from exc
    if keep is not None:
        if command != "prune":
            raise ScheduleError("--keep applies to prune jobs only")
        if keep < 0:
            raise ScheduleError("--keep must be 0 or more")


def remove_job(repo_root: Path, id_or_index: str, *, runner=_default_runner) -> ScheduledJob:
    """Uninstall a job by id or 1-based --list index: drop its crontab line, wrapper, entry.

    :raises ScheduleError: when no job matches.
    """
    jobs = _load_manifest(repo_root)
    job_id = _resolve_id(jobs, id_or_index)
    _crontab_remove_marker(f"{MARKER}{job_id}", runner)
    data = jobs.pop(job_id)
    _save_manifest(repo_root, jobs)
    for suffix in (".sh", ".lock"):
        Path(_schedule_dir(repo_root) / f"{job_id}{suffix}").unlink(missing_ok=True)
    return ScheduledJob(**data)


def remove_all(repo_root: Path, *, runner=_default_runner) -> list[str]:
    """Remove every protonfs job for this repo. Returns the removed ids."""
    ids = list(_load_manifest(repo_root))
    for job_id in ids:
        remove_job(repo_root, job_id, runner=runner)
    return ids


def list_jobs(repo_root: Path) -> list[ScheduledJob]:
    """Every installed job for this repo, ordered by creation."""
    jobs = _load_manifest(repo_root)
    ordered = sorted(jobs.values(), key=lambda d: d.get("created_at", ""))
    return [ScheduledJob(**d) for d in ordered]


def _resolve_id(jobs: dict, id_or_index: str) -> str:
    if id_or_index in jobs:
        return id_or_index
    if id_or_index.isdigit():
        ordered = sorted(jobs.values(), key=lambda d: d.get("created_at", ""))
        idx = int(id_or_index)
        if 1 <= idx <= len(ordered):
            return ordered[idx - 1]["id"]
    raise ScheduleError(f"no scheduled job with id or index {id_or_index!r}.")


def _crontab_read(runner) -> str:
    result = runner(["crontab", "-l"])
    # `crontab -l` exits non-zero when there is no crontab yet -- treat as empty.
    if result.returncode != 0:
        return ""
    return result.stdout


def _crontab_write(text: str, runner) -> None:
    result = runner(["crontab", "-"], stdin=text)
    if result.returncode != 0:
        raise ScheduleError(f"`crontab -` failed: {result.stderr.strip() or result.returncode}")


def _crontab_install_line(line: str, runner) -> None:
    current = _crontab_read(runner)
    # Drop any prior line for this exact marker id (idempotent re-install), keep the rest.
    marker = line.split(MARKER, 1)[1]
    kept = [ln for ln in current.splitlines() if f"{MARKER}{marker}" not in ln]
    kept.append(line)
    _crontab_write("\n".join(kept) + "\n", runner)


def _crontab_remove_marker(marker: str, runner) -> None:
    current = _crontab_read(runner)
    kept = [ln for ln in current.splitlines() if marker not in ln]
    body = ("\n".join(kept) + "\n") if kept else ""
    _crontab_write(body, runner)


def _utcnow() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).isoformat()
