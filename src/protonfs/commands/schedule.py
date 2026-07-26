"""``protonfs schedule``: install/list/remove cron jobs that run push/pull on a schedule.

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

.. versionadded:: 1.8.0
"""
from __future__ import annotations

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

COMMANDS = ("push", "pull", "sync")


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


def _wrapper_text(job: ScheduledJob, protonfs_bin: str, drive_bin: str, lock_path: str) -> str:
    """Render the per-job wrapper script (the generated equivalent of a hand-rolled
    cron push script): absolute binaries, tuned env, flock, timestamped logging."""
    py_bin_dir = str(Path(protonfs_bin).parent)
    drive_bin_dir = str(Path(drive_bin).parent)
    if job.command == "sync":
        run_lines = (
            f'  "{protonfs_bin}" -v pull{_args(job)}\n'
            f'  "{protonfs_bin}" -v push{_args(job)}\n'
        )
    else:
        run_lines = f'  "{protonfs_bin}" -v {job.command}{_args(job)}\n'
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
        f'  echo "===== job {job.id} start $(date -u) ====="\n'
        f"{run_lines}"
        f'  echo "===== job {job.id} end rc=$? $(date -u) ====="\n'
        f') 9>"{lock_path}" >> "{job.log_path}" 2>&1\n'
    )


def _args(job: ScheduledJob) -> str:
    parts = ""
    if job.path:
        parts += f' "{job.path}"'
    if job.resolve:
        parts += f" --resolve={job.resolve}"
    return parts


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
    list_timeout: int = DEFAULT_LIST_TIMEOUT,
    transfer_timeout: int = DEFAULT_TRANSFER_TIMEOUT,
    batch_size: int = DEFAULT_BATCH_SIZE,
    label: str = "",
    protonfs_bin: str | None = None,
    runner=_default_runner,
    now: str | None = None,
) -> ScheduledJob:
    """Install a scheduled job: write its wrapper, append a tagged crontab line, record it.

    :raises ScheduleError: on a non-repo, unknown command, bad cadence, or missing crontab.
    """
    if not is_protonfs_repo(repo_root):
        raise ScheduleError(f"{repo_root} is not a protonfs repo (run `protonfs setup`).")
    if command not in COMMANDS:
        raise ScheduleError(f"unknown --command {command!r} (choose {', '.join(COMMANDS)}).")
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
