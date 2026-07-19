"""Build a per-source status *fragment* under ``data/`` from CI inputs.

Each subcommand writes one ``data/<source>.json`` fragment consumed by ``render.py``.
Fragments are deliberately richer than the current template needs (run URLs, per-version
test counts, durations, timestamps) so the graphic can be extended later without changing
how the data is produced.

Usage (invoked from workflow steps)::

    python .github/status/collect.py ci        # tests/coverage/ruff/python/builds
    python .github/status/collect.py docs       # docs coverage + build status
    python .github/status/collect.py proton-drive
    python .github/status/collect.py project --version 1.5.0

Inputs come from the environment (so the workflow YAML stays declarative). Every input is
optional; anything absent falls back to a sensible default so a fragment is always
writable. See ``_ci`` etc. for the exact variables each subcommand reads.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _result_to_status(result: str) -> str:
    """Map a GitHub job ``result``/``outcome`` to the ``passing``/``failing`` vocabulary."""
    return "passing" if result.strip().lower() == "success" else "failing"


def _run_meta() -> dict[str, Any]:
    """Common provenance block attached to every fragment (all optional)."""
    server = _env("GITHUB_SERVER_URL", "https://github.com")
    repo = _env("GITHUB_REPOSITORY")
    run_id = _env("GITHUB_RUN_ID")
    url = f"{server}/{repo}/actions/runs/{run_id}" if repo and run_id else ""
    return {
        "run_id": run_id,
        "run_attempt": _env("GITHUB_RUN_ATTEMPT"),
        "run_url": url,
        "commit": _env("GITHUB_SHA"),
        "ref": _env("GITHUB_REF_NAME"),
    }


_VOLATILE_KEYS = ("updated", "run")


def _meaningful(fragment: dict[str, Any]) -> dict[str, Any]:
    """A fragment minus per-run volatile fields, for change detection."""
    return {k: v for k, v in fragment.items() if k not in _VOLATILE_KEYS}


def _write(name: str, fragment: dict[str, Any]) -> None:
    """Write ``data/<name>.json`` — but only if the meaningful content changed.

    Every fragment carries a fresh ``updated`` timestamp (and ``run`` provenance), so a
    naive write would churn a git commit on every run even when nothing substantive
    changed. To keep status commits meaningful, if the existing fragment matches the new
    one ignoring the volatile fields, the file is left exactly as-is (old timestamp kept),
    producing no git diff.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"{name}.json"
    if path.exists():
        try:
            with open(path, encoding="utf-8") as fh:
                existing = json.load(fh)
            if _meaningful(existing) == _meaningful(fragment):
                print(f"unchanged {path.name} (skipped)")
                return
        except (json.JSONDecodeError, OSError):
            pass
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(fragment, fh, indent=2, sort_keys=False)
        fh.write("\n")
    print(f"wrote {path.relative_to(ROOT.parent.parent)}")


# --------------------------------------------------------------------------- parsers


def parse_junit(path: Path) -> dict[str, Any]:
    """Aggregate pass/fail/skip counts and duration from a JUnit XML report."""
    if not path.exists():
        return {"passed": 0, "failed": 0, "skipped": 0, "total": 0, "duration_s": 0.0}
    root = ET.parse(path).getroot()
    suites = root.iter("testsuite")
    total = failures = errors = skipped = 0
    duration = 0.0
    for suite in suites:
        total += int(suite.get("tests", 0))
        failures += int(suite.get("failures", 0))
        errors += int(suite.get("errors", 0))
        skipped += int(suite.get("skipped", 0))
        duration += float(suite.get("time", 0) or 0)
    failed = failures + errors
    passed = max(total - failed - skipped, 0)
    return {
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "total": total,
        "duration_s": round(duration, 2),
    }


def parse_coverage_xml(path: Path) -> float:
    """Line-coverage percentage from a Cobertura ``coverage.xml`` (0 if unreadable)."""
    if not path.exists():
        return 0.0
    root = ET.parse(path).getroot()
    rate = root.get("line-rate")
    if rate is not None:
        return round(float(rate) * 100, 1)
    covered = root.get("lines-covered")
    valid = root.get("lines-valid")
    if covered and valid and int(valid) > 0:
        return round(int(covered) / int(valid) * 100, 1)
    return 0.0


# ---------------------------------------------------------------------- subcommands


def _python_results() -> list[dict[str, Any]]:
    """Assemble the per-version python list.

    Preferred source: a directory of per-version JSON files (one per matrix leg,
    ``PY_RESULTS_DIR``), each ``{"version", "status", "tests"}``. Fallback: a flat
    ``PYTHON_VERSIONS`` list all taking the overall matrix ``PYTHON_RESULT``.
    """
    results_dir = _env("PY_RESULTS_DIR")
    entries: list[dict[str, Any]] = []
    if results_dir and Path(results_dir).is_dir():
        for file in sorted(Path(results_dir).rglob("*.json")):
            try:
                with open(file, encoding="utf-8") as fh:
                    entries.append(json.load(fh))
            except (json.JSONDecodeError, OSError):
                continue
    if entries:
        # Sort by version ascending (numeric-aware: "3.9" before "3.10").
        entries.sort(key=lambda e: [int(p) for p in str(e.get("version", "0")).split(".")])
        return entries
    # Fallback: overall result applied to every declared version.
    versions = _env("PYTHON_VERSIONS").split()
    status = _result_to_status(_env("PYTHON_RESULT", "success"))
    return [{"version": v, "status": status} for v in versions]


def _ci(_: argparse.Namespace) -> None:
    junit = parse_junit(Path(_env("JUNIT", "junit.xml")))
    line = parse_coverage_xml(Path(_env("COVERAGE_XML", "coverage.xml")))
    fragment = {
        "source": "ci",
        "updated": _now(),
        "run": _run_meta(),
        "tests": junit,
        "coverage": {"line": line, "target": int(_env("COVERAGE_TARGET", "80") or 80)},
        "ruff": {
            "status": _env("RUFF_STATUS", "unknown") or "unknown",
            "violations": int(_env("RUFF_VIOLATIONS", "0") or 0),
        },
        "python": _python_results(),
        "builds": {
            "Linux": {
                "x86_64": _result_to_status(_env("BUILD_LINUX_X64", "success")),
                "arm64": _result_to_status(_env("BUILD_LINUX_ARM64", "success")),
            },
            "macOS": {
                "x86_64": _result_to_status(_env("BUILD_DARWIN_X64", "success")),
                "arm64": _result_to_status(_env("BUILD_DARWIN_ARM64", "success")),
            },
        },
    }
    _write("ci", fragment)


def _py_result(args: argparse.Namespace) -> None:
    """Write ONE matrix leg's result to ``--out`` (uploaded as an artifact, not committed).

    The ``badges`` job later collects every leg's file via ``PY_RESULTS_DIR`` to build the
    per-version python list. Reads ``PY_VERSION`` and ``PY_OUTCOME`` (the test step's
    ``success``/``failure`` outcome) from the environment, plus the leg's junit report.
    """
    entry = {
        "version": _env("PY_VERSION"),
        "status": _result_to_status(_env("PY_OUTCOME", "success")),
        "tests": parse_junit(Path(_env("JUNIT", "junit.xml"))),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(entry, fh, indent=2)
        fh.write("\n")
    print(f"wrote {out}")


def _docs(_: argparse.Namespace) -> None:
    fragment = {
        "source": "docs",
        "updated": _now(),
        "run": _run_meta(),
        "coverage": float(_env("DOCS_COVERAGE", "0") or 0),
        "build": _result_to_status(_env("DOCS_RESULT", "success")),
        "warnings": int(_env("DOCS_WARNINGS", "0") or 0),
    }
    _write("docs", fragment)


def _proton_drive(_: argparse.Namespace) -> None:
    pinned = _env("PIN")
    latest = _env("LATEST")
    fragment = {
        "source": "proton-drive",
        "updated": _now(),
        "run": _run_meta(),
        "pinned": pinned,
        "latest": latest,
        "in_sync": bool(pinned) and pinned == latest,
    }
    _write("proton_drive", fragment)


def _repo_root() -> Path:
    """Repo root (two levels up from .github/status/), for locating pyproject.toml."""
    return ROOT.parent.parent


def _load_pyproject() -> dict[str, Any]:
    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:  # pragma: no cover - older runners
        import tomli as tomllib  # type: ignore[no-redef]
    with open(_repo_root() / "pyproject.toml", "rb") as fh:
        return tomllib.load(fh)


def _git_latest_version() -> str:
    """Newest ``v*`` tag by version order, minus the ``v`` (empty if none)."""
    import subprocess

    try:
        out = subprocess.run(
            ["git", "tag", "--list", "v[0-9]*", "--sort=-version:refname"],
            capture_output=True,
            text=True,
            check=True,
            cwd=_repo_root(),
        ).stdout
    except (subprocess.CalledProcessError, OSError):
        return ""
    first = out.splitlines()[0] if out.strip() else ""
    return first.lstrip("v")


def _project(args: argparse.Namespace) -> None:
    """Generate the project fragment from pyproject.toml + the current release tag.

    Nothing here is hand-maintained: name/description/links come from ``[project]`` in
    ``pyproject.toml`` (the single source of truth), and the version from ``--version``
    (a release tag passed by the workflow) or, failing that, the newest ``v*`` git tag.
    """
    proj = _load_pyproject().get("project", {})
    name = proj.get("name", "")
    urls = {k.lower(): v for k, v in (proj.get("urls") or {}).items()}
    version = (args.version or _git_latest_version()).lstrip("v")
    fragment = {
        "source": "project",
        "updated": _now(),
        "name": name,
        "description": proj.get("description", ""),
        "version": version,
        "links": {
            "pypi": f"https://pypi.org/project/{name}/" if name else "",
            "docs": (urls.get("documentation") or "").rstrip("/"),
            "github": urls.get("homepage") or urls.get("repository") or "",
        },
    }
    _write("project", fragment)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a status data fragment.")
    sub = parser.add_subparsers(dest="source", required=True)
    sub.add_parser("ci").set_defaults(func=_ci)
    sub.add_parser("docs").set_defaults(func=_docs)
    sub.add_parser("proton-drive").set_defaults(func=_proton_drive)
    p_project = sub.add_parser("project")
    p_project.add_argument("--version", default="")
    p_project.set_defaults(func=_project)
    p_py = sub.add_parser("py-result")
    p_py.add_argument("--out", default="py-result.json")
    p_py.set_defaults(func=_py_result)
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
