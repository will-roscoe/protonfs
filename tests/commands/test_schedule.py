from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from protonfs.commands import schedule as sched
from protonfs.config import init_config


class FakeCrontab:
    """Stateful stand-in for the `crontab` binary: holds the crontab text in memory."""

    def __init__(self) -> None:
        self.text = ""

    def __call__(self, args, stdin=None):
        if args == ["crontab", "-l"]:
            # real crontab exits non-zero when the user has no crontab yet
            rc = 0 if self.text else 1
            return subprocess.CompletedProcess(args, rc, stdout=self.text, stderr="")
        if args == ["crontab", "-"]:
            self.text = stdin or ""
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="unexpected")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    init_config(tmp_path, "/my-files/test")
    # crontab/protonfs/proton-drive resolve to fake absolute paths in tests
    monkeypatch.setattr(
        "protonfs.commands.schedule.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )
    return tmp_path


# --- cadence parsing -------------------------------------------------------------------


@pytest.mark.parametrize(
    "every,cron,at,expected",
    [
        ("hourly", None, None, "0 * * * *"),
        ("daily", None, None, "0 0 * * *"),
        ("weekly", None, None, "0 0 * * 0"),
        ("6h", None, None, "0 */6 * * *"),
        ("30m", None, None, "*/30 * * * *"),
        (None, "15 2 * * 1", None, "15 2 * * 1"),
        (None, None, "1,3,5", "0 1,3,5 * * *"),
    ],
)
def test_cadence_to_cron(every, cron, at, expected) -> None:
    assert sched.cadence_to_cron(every, cron, at) == expected


@pytest.mark.parametrize("bad", ["nightly", "0h", "99h", "0m", "5x"])
def test_cadence_rejects_bad_every(bad) -> None:
    with pytest.raises(sched.ScheduleError):
        sched.cadence_to_cron(bad, None, None)


def test_cadence_rejects_bad_cron_and_at() -> None:
    with pytest.raises(sched.ScheduleError):
        sched.cadence_to_cron(None, "only three fields", None)
    with pytest.raises(sched.ScheduleError):
        sched.cadence_to_cron(None, None, "25")  # hour out of range
    with pytest.raises(sched.ScheduleError):
        sched.cadence_to_cron(None, None, None)  # no cadence at all


# --- add / list / remove ---------------------------------------------------------------


def test_add_job_writes_wrapper_crontab_and_manifest(repo: Path) -> None:
    cron = FakeCrontab()
    job = sched.add_job(
        repo, every="daily", command="push", runner=cron, now="2026-01-01T00:00:00Z"
    )

    # executable wrapper with the key hardening baked in
    wrapper = Path(job.wrapper_path)
    assert wrapper.exists() and wrapper.stat().st_mode & 0o100
    text = wrapper.read_text()
    assert "PROTONFS_DRIVE_BIN=" in text
    assert "flock -n 9" in text
    assert "-v push" in text
    assert f'cd "{repo.resolve()}"' in text

    # exactly one tagged crontab line
    lines = [ln for ln in cron.text.splitlines() if sched.MARKER in ln]
    assert len(lines) == 1
    assert lines[0].startswith("0 0 * * *") and f"{sched.MARKER}{job.id}" in lines[0]

    # recorded in the manifest / listable
    listed = sched.list_jobs(repo)
    assert [j.id for j in listed] == [job.id]


def test_add_job_rejects_non_repo(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("protonfs.commands.schedule.shutil.which", lambda n: f"/usr/bin/{n}")
    with pytest.raises(sched.ScheduleError, match="not a protonfs repo"):
        sched.add_job(tmp_path, every="daily", runner=FakeCrontab())


def test_add_sync_job_runs_pull_then_push(repo: Path) -> None:
    job = sched.add_job(repo, every="hourly", command="sync", runner=FakeCrontab())
    text = Path(job.wrapper_path).read_text()
    assert text.index("-v pull") < text.index("-v push")


def test_add_job_with_path_and_resolve(repo: Path) -> None:
    job = sched.add_job(
        repo, cron="0 2 * * *", command="push", path="mload002",
        resolve="replace", runner=FakeCrontab(),
    )
    text = Path(job.wrapper_path).read_text()
    assert '"mload002"' in text and "--resolve=replace" in text


def test_add_job_keeps_a_glob_pattern_quoted_for_protonfs_to_expand(repo: Path) -> None:
    """#131: the wrapper must pass a pattern through to protonfs *unexpanded*.

    The quoting was previously the bug (cron's shell could not expand it, so a job could
    only ever target one literal subtree). Now that protonfs expands patterns itself, the
    same quoting is exactly what makes a scheduled pattern work: it survives cron's shell
    and reaches protonfs intact, so it re-matches as new runs appear.
    """
    job = sched.add_job(
        repo, cron="0 2 * * *", command="pull", path="mload*/*.ev", runner=FakeCrontab(),
    )
    assert job.path == "mload*/*.ev"
    assert '"mload*/*.ev"' in Path(job.wrapper_path).read_text()


def test_add_job_with_strict_passes_the_flag_to_the_wrapper(repo: Path) -> None:
    job = sched.add_job(
        repo, cron="0 2 * * *", command="pull", path="mload*", strict=True,
        runner=FakeCrontab(),
    )
    assert job.strict is True
    assert "--strict" in Path(job.wrapper_path).read_text()


def test_add_job_without_strict_omits_the_flag(repo: Path) -> None:
    job = sched.add_job(repo, cron="0 2 * * *", command="pull", path="mload*", runner=FakeCrontab())
    assert job.strict is False
    assert "--strict" not in Path(job.wrapper_path).read_text()


def test_job_from_a_manifest_predating_strict_still_loads(repo: Path) -> None:
    """A job installed before --strict existed has no `strict` key in the manifest;
    loading it must not blow up (the field defaults to False)."""
    import json

    job = sched.add_job(repo, every="daily", runner=FakeCrontab())
    manifest_path = repo / ".protonfs" / sched.MANIFEST_FILE_NAME
    document = json.loads(manifest_path.read_text())
    del document["jobs"][job.id]["strict"]  # simulate a pre-1.11.0 manifest
    manifest_path.write_text(json.dumps(document))

    loaded = sched.list_jobs(repo)

    assert len(loaded) == 1
    assert loaded[0].strict is False


def test_reinstall_same_id_is_idempotent(repo: Path, monkeypatch) -> None:
    cron = FakeCrontab()
    # force a deterministic id so we can re-install it
    monkeypatch.setattr("protonfs.commands.schedule._new_id", lambda existing: "aaa111")
    sched.add_job(repo, every="daily", runner=cron)
    # same id -> replaces the line (a different scope, so it is not a refused duplicate)
    sched.add_job(repo, every="hourly", path="run1", runner=cron)
    lines = [ln for ln in cron.text.splitlines() if "aaa111" in ln]
    assert len(lines) == 1
    assert lines[0].startswith("0 * * * *")  # the hourly re-install won


def test_remove_job_by_id_cleans_up(repo: Path) -> None:
    cron = FakeCrontab()
    job = sched.add_job(repo, every="daily", runner=cron)
    assert sched.MARKER in cron.text

    sched.remove_job(repo, job.id, runner=cron)

    assert sched.MARKER not in cron.text
    assert not Path(job.wrapper_path).exists()
    assert sched.list_jobs(repo) == []


def test_remove_job_by_index(repo: Path) -> None:
    cron = FakeCrontab()
    j1 = sched.add_job(repo, every="daily", runner=cron, now="2026-01-01T00:00:00Z")
    sched.add_job(repo, every="hourly", path="run2", runner=cron, now="2026-01-02T00:00:00Z")

    removed = sched.remove_job(repo, "1", runner=cron)  # 1-based index -> first job

    assert removed.id == j1.id
    assert [j.id for j in sched.list_jobs(repo)] != [] and j1.id not in [
        j.id for j in sched.list_jobs(repo)
    ]


def test_remove_unknown_id_errors(repo: Path) -> None:
    with pytest.raises(sched.ScheduleError, match="no scheduled job"):
        sched.remove_job(repo, "nope", runner=FakeCrontab())


def test_remove_all(repo: Path) -> None:
    cron = FakeCrontab()
    sched.add_job(repo, every="daily", runner=cron, now="2026-01-01T00:00:00Z")
    sched.add_job(repo, every="hourly", path="run2", runner=cron, now="2026-01-02T00:00:00Z")

    removed = sched.remove_all(repo, runner=cron)

    assert len(removed) == 2
    assert sched.list_jobs(repo) == []
    assert sched.MARKER not in cron.text


# --- module edge cases -----------------------------------------------------------------


def test_add_job_rejects_unknown_command(repo: Path) -> None:
    with pytest.raises(sched.ScheduleError, match="unknown --command"):
        sched.add_job(repo, every="daily", command="bogus", runner=FakeCrontab())


def test_add_job_errors_when_crontab_missing(tmp_path: Path, monkeypatch) -> None:
    from protonfs.config import init_config

    init_config(tmp_path, "/my-files/test")
    monkeypatch.setattr(
        "protonfs.commands.schedule.shutil.which",
        lambda name: None if name == "crontab" else f"/usr/bin/{name}",
    )
    with pytest.raises(sched.ScheduleError, match="crontab.*unavailable"):
        sched.add_job(tmp_path, every="daily", runner=FakeCrontab())


def test_crontab_write_failure_is_surfaced(repo: Path) -> None:
    import subprocess as _sp

    def failing(args, stdin=None):
        if args == ["crontab", "-l"]:
            return _sp.CompletedProcess(args, 1, stdout="", stderr="")
        return _sp.CompletedProcess(args, 1, stdout="", stderr="permission denied")

    with pytest.raises(sched.ScheduleError, match="crontab.*failed"):
        sched.add_job(repo, every="daily", runner=failing)


def test_corrupt_manifest_is_treated_as_empty(repo: Path) -> None:
    (repo / ".protonfs" / sched.MANIFEST_FILE_NAME).write_text("{ not json")
    assert sched.list_jobs(repo) == []


# --- CLI wiring ------------------------------------------------------------------------


class _FakeRun:
    """Fake `subprocess.run` for the crontab binary, so the CLI path needs no real cron."""

    def __init__(self) -> None:
        self.text = ""

    def __call__(self, args, input=None, capture_output=None, text=None, check=None):
        import subprocess as _sp

        if args == ["crontab", "-l"]:
            return _sp.CompletedProcess(args, 0 if self.text else 1, stdout=self.text, stderr="")
        if args == ["crontab", "-"]:
            self.text = input or ""
            return _sp.CompletedProcess(args, 0, stdout="", stderr="")
        return _sp.CompletedProcess(args, 1, stdout="", stderr="unexpected")


@pytest.fixture
def cli_repo(tmp_path: Path, monkeypatch):
    from protonfs.config import init_config
    from protonfs.context import load_context

    init_config(tmp_path, "/my-files/test")
    ctx = load_context(tmp_path)
    monkeypatch.setattr("protonfs.context.load_context", lambda *a, **k: ctx)
    monkeypatch.setattr(
        "protonfs.commands.schedule.shutil.which", lambda name: f"/usr/bin/{name}"
    )
    monkeypatch.setattr("protonfs.commands.schedule.subprocess.run", _FakeRun())
    return tmp_path


def test_cli_schedule_add_list_uninstall(cli_repo: Path) -> None:
    from click.testing import CliRunner

    from protonfs.cli import main

    runner = CliRunner()
    add = runner.invoke(main, ["schedule", "--add", "--every", "daily", "--command", "push"])
    assert add.exit_code == 0, add.output
    assert "scheduled job" in add.output
    job_id = add.output.split("scheduled job", 1)[1].split(":", 1)[0].strip()

    listed = runner.invoke(main, ["schedule", "--list"])
    assert listed.exit_code == 0 and job_id in listed.output

    bare = runner.invoke(main, ["schedule"])  # bare == list
    assert bare.exit_code == 0 and job_id in bare.output

    rm = runner.invoke(main, ["schedule", "-U", job_id])
    assert rm.exit_code == 0 and "removed" in rm.output
    assert job_id not in runner.invoke(main, ["schedule", "--list"]).output


def test_cli_schedule_add_without_cadence_is_usage_error(cli_repo: Path) -> None:
    from click.testing import CliRunner

    from protonfs.cli import main

    result = CliRunner().invoke(main, ["schedule", "--add"])
    assert result.exit_code == 2  # no --every/--cron/--at


def test_cli_schedule_mode_flags_are_mutually_exclusive(cli_repo: Path) -> None:
    from click.testing import CliRunner

    from protonfs.cli import main

    result = CliRunner().invoke(main, ["schedule", "--add", "--list"])
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_cli_schedule_list_json_empty(cli_repo: Path) -> None:
    from click.testing import CliRunner

    from protonfs.cli import main

    result = CliRunner().invoke(main, ["schedule", "--list", "--json"])
    assert result.exit_code == 0 and result.output.strip() == "[]"


def test_cli_schedule_uninstall_all(cli_repo: Path) -> None:
    from click.testing import CliRunner

    from protonfs.cli import main

    runner = CliRunner()
    runner.invoke(main, ["schedule", "--add", "--every", "hourly"])
    runner.invoke(main, ["schedule", "--add", "--cron", "0 2 * * *", "--command", "pull",
                         "--path", "other"])
    out = runner.invoke(main, ["schedule", "--all"])
    assert out.exit_code == 0 and "removed 2" in out.output


# --- #158: offload and prune jobs --------------------------------------------------------


def test_offload_job_pushes_its_scope_then_offloads_it(repo: Path) -> None:
    job = sched.add_job(
        repo, every="daily", command="offload", path="mload*", min_age="12h",
        resolve="local", runner=FakeCrontab(),
    )
    text = Path(job.wrapper_path).read_text()

    push_line = next(ln for ln in text.splitlines() if "-v push" in ln)
    offload_line = next(ln for ln in text.splitlines() if "-v offload" in ln)
    assert text.index(push_line) < text.index(offload_line)
    assert '"mload*"' in push_line and "--resolve=local" in push_line
    assert "--min-age" not in push_line  # push takes no retention options
    assert '"mload*"' in offload_line and "--yes" in offload_line
    assert "--min-age=12h" in offload_line and "--resolve" not in offload_line


def test_prune_job_runs_prune_with_its_retention_options(repo: Path) -> None:
    job = sched.add_job(
        repo, every="daily", command="prune", path="sim", min_age="2d", keep=5,
        runner=FakeCrontab(),
    )
    text = Path(job.wrapper_path).read_text()

    assert '-v prune --yes "sim" --min-age=2d --keep=5' in text
    assert "-v push" not in text  # prune pushes first itself
    assert (job.min_age, job.keep) == ("2d", 5)


def test_prune_job_without_options_uses_the_commands_defaults(repo: Path) -> None:
    job = sched.add_job(repo, every="daily", command="prune", runner=FakeCrontab())
    text = Path(job.wrapper_path).read_text()

    assert "-v prune --yes || rc=$?" in text
    assert "--keep" not in text and "--min-age" not in text


@pytest.mark.parametrize(
    "command,kwargs,message",
    [
        ("push", {"min_age": "1d"}, "--min-age applies to offload and prune"),
        ("offload", {"keep": 3}, "--keep applies to prune jobs only"),
        ("prune", {"min_age": "12"}, "invalid duration"),
        ("prune", {"keep": -1}, "--keep must be 0 or more"),
        ("prune", {"resolve": "local"}, "a prune job does not take them"),
        ("prune", {"strict": True}, "a prune job does not take them"),
    ],
)
def test_add_job_refuses_options_the_command_would_ignore(
    repo: Path, command: str, kwargs: dict, message: str
) -> None:
    with pytest.raises(sched.ScheduleError, match=message):
        sched.add_job(repo, every="daily", command=command, runner=FakeCrontab(), **kwargs)
    assert sched.list_jobs(repo) == []


def test_job_from_a_manifest_predating_retention_options_still_loads(repo: Path) -> None:
    import json

    job = sched.add_job(repo, every="daily", runner=FakeCrontab())
    manifest_path = repo / ".protonfs" / sched.MANIFEST_FILE_NAME
    document = json.loads(manifest_path.read_text())
    del document["jobs"][job.id]["min_age"]
    del document["jobs"][job.id]["keep"]
    manifest_path.write_text(json.dumps(document))

    loaded = sched.list_jobs(repo)[0]
    assert (loaded.min_age, loaded.keep) == (None, None)


# --- #158: one job at a time per repo ------------------------------------------------------


def test_every_wrapper_shares_the_repo_lock_and_keeps_its_own(repo: Path) -> None:
    cron = FakeCrontab()
    a = sched.add_job(repo, every="daily", command="push", path="a", runner=cron)
    b = sched.add_job(repo, every="daily", command="prune", path="b", runner=cron)
    shared = str(repo / ".protonfs" / "schedule" / sched.ROOT_LOCK_NAME)

    for job in (a, b):
        text = Path(job.wrapper_path).read_text()
        assert "flock -n 9" in text  # never overlaps itself
        assert f"flock -w {sched.ROOT_LOCK_WAIT_SECONDS} 8" in text  # takes turns
        assert f'8>"{shared}"' in text
        assert f'9>"{repo / ".protonfs" / "schedule" / job.id}.lock"' in text


def _fake_protonfs(tmp_path: Path) -> Path:
    """A stand-in `protonfs` that logs when each invocation starts and ends."""
    events = tmp_path / "events.log"
    script = tmp_path / "bin" / "protonfs"
    script.parent.mkdir()
    script.write_text(
        "#!/bin/bash\n"
        f'echo "start $$ $*" >> "{events}"\n'
        "sleep 0.5\n"
        f'echo "end $$ $*" >> "{events}"\n'
    )
    script.chmod(0o755)
    return script


@pytest.mark.skipif(
    __import__("shutil").which("flock") is None, reason="needs util-linux flock"
)
def test_two_jobs_on_one_repo_never_run_at_the_same_time(repo: Path, tmp_path: Path) -> None:
    # Real wrappers, run concurrently: the second waits for the first instead of both
    # running (and one failing on protonfs's own repo lock).
    fake = _fake_protonfs(tmp_path)
    cron = FakeCrontab()
    first = sched.add_job(
        repo, every="daily", command="push", path="a", protonfs_bin=str(fake), runner=cron
    )
    second = sched.add_job(
        repo, every="daily", command="prune", path="b", protonfs_bin=str(fake), runner=cron
    )

    procs = [subprocess.Popen(["bash", j.wrapper_path]) for j in (first, second)]
    for proc in procs:
        assert proc.wait(timeout=30) == 0

    events = (tmp_path / "events.log").read_text().split("\n")
    kinds = [line.split(" ", 1)[0] for line in events if line]
    assert kinds == ["start", "end", "start", "end"]  # strictly one after the other


@pytest.mark.skipif(
    __import__("shutil").which("flock") is None, reason="needs util-linux flock"
)
def test_a_wrapper_propagates_a_failing_command_into_its_log(
    repo: Path, tmp_path: Path
) -> None:
    failing = tmp_path / "protonfs-fail"
    failing.write_text("#!/bin/bash\nexit 3\n")
    failing.chmod(0o755)
    job = sched.add_job(
        repo, every="daily", command="offload", protonfs_bin=str(failing), runner=FakeCrontab()
    )

    subprocess.run(["bash", job.wrapper_path], check=False, timeout=30)

    assert "end rc=3" in Path(job.log_path).read_text()


# --- #158: conflict checks (pure) ------------------------------------------------------------


def _job(command: str, path: str | None = None, job_id: str = "old001") -> sched.ScheduledJob:
    return sched.ScheduledJob(
        id=job_id, cron="0 0 * * *", command=command, path=path, resolve=None, repo="/r",
        list_timeout=1, transfer_timeout=1, batch_size=1, label="", created_at="t",
        log_path="/l",
    )


@pytest.mark.parametrize(
    "a,b,expected",
    [
        (None, None, "identical"),
        (None, "run1", "overlap"),
        ("./run1/", "run1", "identical"),
        ("run1", "run1/x.ev", "overlap"),
        ("run1", "run10", "disjoint"),
        ("mload*", "mload004", "overlap"),
        ("mload*", "other*", "disjoint"),
        ("mload*/*.ev", "mload*/*.sink", "disjoint"),
        ("mload*/*.ev", "mload004/run.ev", "overlap"),
        ("mload*", "mload00*", "overlap"),
        ("a/**", "b", "overlap"),  # ** can span segments: treated as overlapping
        ("*.ev", "run*", "overlap"),
    ],
)
def test_scope_relation(a, b, expected: str) -> None:
    assert sched.scope_relation(a, b) == expected
    assert sched.scope_relation(b, a) == expected


@pytest.mark.parametrize(
    "new,new_path,old,old_path,level,needle",
    [
        # duplicates
        ("push", None, "push", None, "error", "already runs `push`"),
        ("prune", "sim", "prune", "sim/", "error", "already runs `prune`"),
        # push alongside offload/prune: those push first already
        ("push", "sim", "offload", "sim", "error", "schedule `offload` alone"),
        ("prune", None, "push", "sim", "error", "schedule `prune` alone"),
        # push + pull: identical is an error, overlap a warning, both point at sync
        ("pull", "sim", "push", "sim", "error", "--command sync"),
        ("pull", "sim/run1", "push", "sim", "warning", "--command sync"),
        ("sync", "sim", "push", "sim", "error", "keep just that one"),
        # offload/prune + pull/sync: one removes what the other restores
        ("pull", "sim", "offload", "sim/*", "error", "fight each other"),
        ("prune", "sim", "sync", None, "error", "fight each other"),
        # offload + prune
        ("offload", "sim", "prune", "sim", "error", "offload would remove the newest"),
        # same command, overlapping scopes
        ("push", "sim/run1", "push", "sim", "warning", "repeat each other's work"),
    ],
)
def test_check_conflicts_rules(new, new_path, old, old_path, level, needle) -> None:
    conflicts = sched.check_conflicts(new, new_path, [_job(old, old_path)])

    assert [c.level for c in conflicts] == [level]
    assert needle in conflicts[0].message
    assert conflicts[0].job_id == "old001"


@pytest.mark.parametrize(
    "new,new_path,old,old_path",
    [
        ("push", "a", "pull", "b"),  # disjoint scopes never conflict
        ("prune", "mload*", "pull", "other*"),
        ("pull", "a", "pull", "b"),
    ],
)
def test_check_conflicts_ignores_disjoint_scopes(new, new_path, old, old_path) -> None:
    assert sched.check_conflicts(new, new_path, [_job(old, old_path)]) == []


def test_check_conflicts_reports_each_clashing_job() -> None:
    existing = [_job("push", "sim", "aaa"), _job("pull", "other", "bbb"),
                _job("pull", "sim/x", "ccc")]

    conflicts = sched.check_conflicts("offload", "sim", existing)

    assert [(c.job_id, c.level) for c in conflicts] == [("aaa", "error"), ("ccc", "error")]


def test_add_job_refuses_an_error_conflict_and_installs_nothing(repo: Path) -> None:
    cron = FakeCrontab()
    sched.add_job(repo, every="daily", command="pull", path="sim", runner=cron)
    before = cron.text

    with pytest.raises(sched.ScheduleError, match="refusing to add this job"):
        sched.add_job(repo, every="daily", command="prune", path="sim", runner=cron)

    assert cron.text == before
    assert len(sched.list_jobs(repo)) == 1


def test_add_job_reports_a_warning_and_still_installs(repo: Path) -> None:
    warnings: list[str] = []
    sched.add_job(repo, every="daily", command="push", path="sim", runner=FakeCrontab())

    sched.add_job(
        repo, every="daily", command="pull", path="sim/run1", runner=FakeCrontab(),
        warn=warnings.append,
    )

    assert len(sched.list_jobs(repo)) == 2
    assert len(warnings) == 1 and "sync" in warnings[0]


def test_cli_schedule_conflict_is_a_usage_error_and_a_warning_goes_to_stderr(
    cli_repo: Path,
) -> None:
    from click.testing import CliRunner

    from protonfs.cli import main

    runner = CliRunner()
    base = ["schedule", "--add", "--every", "daily"]
    assert runner.invoke(main, [*base, "--command", "push", "--path", "sim"]).exit_code == 0

    refused = runner.invoke(main, [*base, "--command", "offload", "--path", "sim"])
    warned = runner.invoke(main, [*base, "--command", "pull", "--path", "sim/run1"])
    prune = runner.invoke(
        main, [*base, "--command", "prune", "--path", "other", "--keep", "3", "--min-age", "6h"]
    )

    assert refused.exit_code == 2 and "schedule `offload` alone" in refused.output
    assert warned.exit_code == 0 and "warning:" in warned.output
    assert prune.exit_code == 0, prune.output
    listed = runner.invoke(main, ["schedule", "--list", "--json"]).output
    assert '"keep": 3' in listed and '"min_age": "6h"' in listed
