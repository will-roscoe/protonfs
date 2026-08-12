# Shell Completions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `protonfs completions {bash|zsh|fish}` command that prints, installs (idempotently), and uninstalls shell completion driven by Click's native engine, refreshed by `protonfs upgrade`.

**Architecture:** A pure-logic module `protonfs.commands.completions` generates the script via `click.shell_completion.get_completion_class` and manages per-shell install files with a marker block; `cli.py` wires the command; `run_upgrade` refreshes installed scripts. All install paths are parameterized on `home` for testability.

**Tech Stack:** Python 3.9+, Click 8.4.2 (native completion), pytest.

## Global Constraints

- No third-party completion dependency — Click native only.
- New command is additive → `feat:` (minor bump). Add to `tests/test_cli_surface.py` and `docs/stability.rst`; `.. versionadded:: 1.5.0`.
- Supported shells: `bash`, `zsh`, `fish`. Completion var `_PROTONFS_COMPLETE`, prog name `protonfs`.
- Marker block: `# >>> protonfs completions >>>` / `# <<< protonfs completions <<<` (idempotent).
- Install targets: bash → `~/.local/share/protonfs/completion.bash` + source line in `~/.bashrc`; zsh → `~/.local/share/protonfs/completion.zsh` + source line in `~/.zshrc`; fish → `~/.config/fish/completions/protonfs.fish` (no rc edit).
- Do NOT wire removal into `deinit` (deinit is `.protonfs/`-only by contract).
- Commit trailer: exactly one `Helped-By: claude-opus-4-8 <anthropic@willroscoe.uk>`.
- Verify docs build stays clean: `sphinx-build -W --keep-going -b html docs/ docs/_build/html` (uses `/tmp/ci-repro-venv` or a Sphinx-8.x venv — the machine's default `sphinx-build` is the wrong Sphinx 7.4.7).

---

### Task 1: Completion-script generation

**Files:**
- Create: `src/protonfs/commands/completions.py`
- Test: `tests/test_completions.py`

**Interfaces:**
- Produces: `SUPPORTED_SHELLS: tuple[str, ...]`; `completion_script(shell: str) -> str`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_completions.py
import pytest
from protonfs.commands import completions as C


@pytest.mark.parametrize("shell,needle", [
    ("bash", "_protonfs_completion"),
    ("zsh", "#compdef protonfs"),
    ("fish", "complete"),
])
def test_completion_script_nonempty_per_shell(shell, needle):
    script = C.completion_script(shell)
    assert script.strip()
    assert "_PROTONFS_COMPLETE" in script
    assert needle in script


def test_completion_script_unknown_shell():
    with pytest.raises(ValueError):
        C.completion_script("tcsh")
```

- [ ] **Step 2: Run it — expect failure**

Run: `pytest tests/test_completions.py -q`
Expected: FAIL (module/function missing).

- [ ] **Step 3: Implement generation**

```python
# src/protonfs/commands/completions.py
# src/protonfs/commands/completions.py
"""`protonfs completions {bash,zsh,fish}` — print, install, or remove shell completion.

Drives Click's native completion engine (Click 8+); no third-party dependency. Install
writes the generated script to a file and references it (Click's recommended pattern, so a
new shell does not run protonfs on startup), managed idempotently with a marker block.

.. versionadded:: 1.5.0
"""
from __future__ import annotations

import re
from pathlib import Path

from click.shell_completion import get_completion_class

SUPPORTED_SHELLS: tuple[str, ...] = ("bash", "zsh", "fish")
_COMPLETE_VAR = "_PROTONFS_COMPLETE"
_PROG_NAME = "protonfs"
MARKER_BEGIN = "# >>> protonfs completions >>>"
MARKER_END = "# <<< protonfs completions <<<"


def completion_script(shell: str) -> str:
    """Return the Click-generated completion script for ``shell``.

    :param shell: one of :data:`SUPPORTED_SHELLS`.
    :raises ValueError: for an unsupported shell.
    """
    if shell not in SUPPORTED_SHELLS:
        raise ValueError(f"unsupported shell: {shell!r} (choose from {', '.join(SUPPORTED_SHELLS)})")
    comp_cls = get_completion_class(shell)
    if comp_cls is None:
        raise ValueError(f"Click has no completion class for shell: {shell!r}")
    from protonfs.cli import main  # lazy: avoid circular import at module load

    return comp_cls(main, {}, _PROG_NAME, _COMPLETE_VAR).source()
```

- [ ] **Step 4: Run tests — expect pass**

Run: `pytest tests/test_completions.py -q`
Expected: PASS (3 cases).

- [ ] **Step 5: Commit**

```bash
git add src/protonfs/commands/completions.py tests/test_completions.py
git commit -m "feat(completions): generate Click-native completion scripts

Helped-By: claude-opus-4-8 <anthropic@willroscoe.uk>"
```

---

### Task 2: Install / uninstall / refresh with a marker block

**Files:**
- Modify: `src/protonfs/commands/completions.py`
- Test: `tests/test_completions.py`

**Interfaces:**
- Consumes: `completion_script`, `SUPPORTED_SHELLS`, `MARKER_BEGIN`, `MARKER_END`.
- Produces: `install_completion(shell, home=None) -> Path`; `uninstall_completion(shell, home=None) -> bool`; `is_installed(shell, home=None) -> bool`; `refresh_installed(home=None) -> list[str]`.

- [ ] **Step 1: Write the failing tests**

```python
def test_install_creates_script_and_marker(tmp_path):
    p = C.install_completion("bash", home=tmp_path)
    assert p == tmp_path / ".local/share/protonfs/completion.bash"
    assert p.read_text().strip()
    rc = (tmp_path / ".bashrc").read_text()
    assert C.MARKER_BEGIN in rc and C.MARKER_END in rc
    assert str(p) in rc
    assert C.is_installed("bash", home=tmp_path)


def test_install_is_idempotent(tmp_path):
    C.install_completion("bash", home=tmp_path)
    C.install_completion("bash", home=tmp_path)
    rc = (tmp_path / ".bashrc").read_text()
    assert rc.count(C.MARKER_BEGIN) == 1  # exactly one block


def test_fish_install_needs_no_rc(tmp_path):
    p = C.install_completion("fish", home=tmp_path)
    assert p == tmp_path / ".config/fish/completions/protonfs.fish"
    assert p.exists()
    assert not (tmp_path / ".config/fish/config.fish").exists()


def test_uninstall_removes_script_and_marker(tmp_path):
    C.install_completion("bash", home=tmp_path)
    assert C.uninstall_completion("bash", home=tmp_path) is True
    assert not (tmp_path / ".local/share/protonfs/completion.bash").exists()
    assert C.MARKER_BEGIN not in (tmp_path / ".bashrc").read_text()
    assert C.uninstall_completion("bash", home=tmp_path) is False  # already gone


def test_refresh_only_touches_installed(tmp_path):
    C.install_completion("zsh", home=tmp_path)
    assert C.refresh_installed(home=tmp_path) == ["zsh"]
    assert C.refresh_installed(home=tmp_path)  # still ["zsh"], stays installed
```

- [ ] **Step 2: Run — expect failure**

Run: `pytest tests/test_completions.py -q`
Expected: FAIL (install/uninstall/etc. undefined).

- [ ] **Step 3: Implement install/uninstall/refresh**

```python
def _home(home: Path | None) -> Path:
    return Path(home) if home is not None else Path.home()


def _targets(shell: str, home: Path | None) -> tuple[Path, Path | None, str]:
    """Return (script_file, rc_file_or_None, source_line)."""
    h = _home(home)
    if shell == "bash":
        script = h / ".local/share/protonfs/completion.bash"
        return script, h / ".bashrc", f'source "{script}"'
    if shell == "zsh":
        script = h / ".local/share/protonfs/completion.zsh"
        return script, h / ".zshrc", f'source "{script}"'
    if shell == "fish":
        return h / ".config/fish/completions/protonfs.fish", None, ""
    raise ValueError(f"unsupported shell: {shell!r}")


def _strip_marker_block(text: str) -> str:
    pattern = re.compile(
        rf"(?ms)^{re.escape(MARKER_BEGIN)}.*?^{re.escape(MARKER_END)}[ \t]*\n?",
    )
    return pattern.sub("", text)


def _set_marker_block(rc: Path, source_line: str) -> None:
    text = rc.read_text() if rc.exists() else ""
    text = _strip_marker_block(text)
    if text and not text.endswith("\n"):
        text += "\n"
    rc.parent.mkdir(parents=True, exist_ok=True)
    rc.write_text(f"{text}{MARKER_BEGIN}\n{source_line}\n{MARKER_END}\n")


def install_completion(shell: str, home: Path | None = None) -> Path:
    """Write the completion script and wire it into the shell (idempotent).

    :returns: the path of the written script file.
    """
    script, rc, source_line = _targets(shell, home)
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(completion_script(shell))
    if rc is not None:
        _set_marker_block(rc, source_line)
    return script


def is_installed(shell: str, home: Path | None = None) -> bool:
    script, rc, _ = _targets(shell, home)
    if not script.exists():
        return False
    if rc is None:
        return True
    return rc.exists() and MARKER_BEGIN in rc.read_text()


def uninstall_completion(shell: str, home: Path | None = None) -> bool:
    """Remove the script file and its marker block. Returns True if anything was removed."""
    script, rc, _ = _targets(shell, home)
    removed = False
    if script.exists():
        script.unlink()
        removed = True
    if rc is not None and rc.exists():
        text = rc.read_text()
        stripped = _strip_marker_block(text)
        if stripped != text:
            rc.write_text(stripped)
            removed = True
    return removed


def refresh_installed(home: Path | None = None) -> list[str]:
    """Rewrite the script file for every shell that is currently installed."""
    refreshed = []
    for shell in SUPPORTED_SHELLS:
        if is_installed(shell, home=home):
            script, _, _ = _targets(shell, home)
            script.write_text(completion_script(shell))
            refreshed.append(shell)
    return refreshed
```

- [ ] **Step 4: Run — expect pass**

Run: `pytest tests/test_completions.py -q`
Expected: PASS (all cases).

- [ ] **Step 5: Commit**

```bash
git add src/protonfs/commands/completions.py tests/test_completions.py
git commit -m "feat(completions): idempotent install/uninstall/refresh with a marker block

Helped-By: claude-opus-4-8 <anthropic@willroscoe.uk>"
```

---

### Task 3: Wire the `completions` CLI command + frozen-surface + docs

**Files:**
- Modify: `src/protonfs/cli.py` (add command near `shell-init`, cli.py:679-690)
- Modify: `tests/test_cli_surface.py` (EXPECTED_TOP_LEVEL_COMMANDS ~line 26; EXPECTED_OPTIONS ~line 54)
- Modify: `docs/stability.rst` (top-level command list/table)
- Test: `tests/test_completions.py`

**Interfaces:**
- Consumes: `completion_script`, `install_completion`, `uninstall_completion`, `SUPPORTED_SHELLS`.

- [ ] **Step 1: Write the failing tests**

```python
from click.testing import CliRunner
from protonfs.cli import main


def test_cli_completions_prints_script():
    r = CliRunner().invoke(main, ["completions", "bash"])
    assert r.exit_code == 0
    assert "_PROTONFS_COMPLETE" in r.output


def test_cli_completions_install(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    r = CliRunner().invoke(main, ["completions", "bash", "--install"])
    assert r.exit_code == 0
    assert (tmp_path / ".local/share/protonfs/completion.bash").exists()


def test_cli_completions_install_and_uninstall_mutually_exclusive():
    r = CliRunner().invoke(main, ["completions", "bash", "--install", "--uninstall"])
    assert r.exit_code != 0


def test_cli_completions_rejects_unknown_shell():
    r = CliRunner().invoke(main, ["completions", "tcsh"])
    assert r.exit_code != 0
```

- [ ] **Step 2: Run — expect failure**

Run: `pytest tests/test_completions.py -q -k cli`
Expected: FAIL (no `completions` command).

- [ ] **Step 3: Add the command in `cli.py`** (place after the `shell-init` command, ~cli.py:690)

```python
@main.command("completions")
@click.argument("shell", type=click.Choice(("bash", "zsh", "fish")))
@click.option("--install", is_flag=True, help="Install the completion script (idempotent).")
@click.option("--uninstall", is_flag=True, help="Remove the installed completion script.")
def completions(shell: str, install: bool, uninstall: bool) -> None:
    """Print or install shell completion (bash|zsh|fish).

    Global flags typed *after* a subcommand are not offered (Click completes in canonical
    order); command names and per-subcommand options complete normally.
    """
    from protonfs.commands.completions import (
        completion_script,
        install_completion,
        uninstall_completion,
    )

    if install and uninstall:
        raise click.UsageError("--install and --uninstall are mutually exclusive.")
    if install:
        path = install_completion(shell)
        click.echo(f"Installed {shell} completion -> {path}")
        click.echo("Start a new shell (or source your rc) to activate it.")
    elif uninstall:
        removed = uninstall_completion(shell)
        click.echo("Removed completion." if removed else "No completion was installed.")
    else:
        click.echo(completion_script(shell))
```

- [ ] **Step 4: Run — expect pass**

Run: `pytest tests/test_completions.py -q`
Expected: PASS.

- [ ] **Step 5: Update the frozen-surface test**

In `tests/test_cli_surface.py`, add `"completions"` to `EXPECTED_TOP_LEVEL_COMMANDS` (the frozenset ~line 26) and add to `EXPECTED_OPTIONS` (~line 54):
```python
    "completions": frozenset({"--install", "--uninstall"}),
```

- [ ] **Step 6: Update `docs/stability.rst`**

Add `completions` to the top-level command enumeration/table with a one-line description:
"`completions {bash,zsh,fish}` — print or install shell completion." (Read the file first to match the existing table format.)

- [ ] **Step 7: Run the surface + full suite**

Run: `pytest tests/test_cli_surface.py tests/test_completions.py -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/protonfs/cli.py tests/test_cli_surface.py tests/test_completions.py docs/stability.rst
git commit -m "feat(completions): add the protonfs completions command

feat: shell completion for bash/zsh/fish via a new completions command with
--install/--uninstall; registered in the frozen CLI surface and stability contract.

Helped-By: claude-opus-4-8 <anthropic@willroscoe.uk>"
```

---

### Task 4: Refresh installed completions on `upgrade`

**Files:**
- Modify: `src/protonfs/commands/upgrade.py` (in `run_upgrade`, after the repo-migrations block ~upgrade.py:165, before the final `check`/`done` handling)
- Test: `tests/test_completions.py`

**Interfaces:**
- Consumes: `refresh_installed`, `is_installed`.

- [ ] **Step 1: Write the failing test**

```python
def test_upgrade_refreshes_installed_completions(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    C.install_completion("bash", home=tmp_path)
    script = tmp_path / ".local/share/protonfs/completion.bash"
    script.write_text("STALE")  # simulate an out-of-date script
    from protonfs.commands.upgrade import refresh_completions_step
    refreshed = refresh_completions_step()
    assert refreshed == ["bash"]
    assert script.read_text() != "STALE"


def test_upgrade_refresh_noop_when_none_installed(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    from protonfs.commands.upgrade import refresh_completions_step
    assert refresh_completions_step() == []
```

- [ ] **Step 2: Run — expect failure**

Run: `pytest tests/test_completions.py -q -k upgrade`
Expected: FAIL (`refresh_completions_step` undefined).

- [ ] **Step 3: Implement the step and call it in `run_upgrade`**

Add to `src/protonfs/commands/upgrade.py`:
```python
def refresh_completions_step() -> list[str]:
    """Rewrite any installed shell-completion scripts so they track new commands."""
    from protonfs.commands.completions import refresh_installed

    return refresh_installed()
```
Then in `run_upgrade`, after the repo-migrations block and before the final `if check:` handling, add (only acts when not a check and not drive-only-scoped restriction on completions — completions are global, so run whenever the binary side runs):
```python
    if not check:
        refreshed = refresh_completions_step()
        if refreshed:
            click.echo(f"shell completion: refreshed {', '.join(refreshed)}.")
```

- [ ] **Step 4: Run — expect pass**

Run: `pytest tests/test_completions.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/protonfs/commands/upgrade.py tests/test_completions.py
git commit -m "feat(upgrade): refresh installed shell completions on upgrade

Helped-By: claude-opus-4-8 <anthropic@willroscoe.uk>"
```

---

### Task 5 (stretch, timeboxed): position-independent global-flag completion

**Files:**
- Modify: `src/protonfs/commands/completions.py` and/or `src/protonfs/cli.py`
- Test: `tests/test_completions.py`

**Interfaces:** no new public names unless feasible.

- [ ] **Step 1: Spike (≤30 min).** Investigate whether Click 8's completion can offer the group's global options (`-v`, `--event-log`, `--progress-inline`) when the cursor is after a subcommand, e.g. by a custom `shell_complete` on the group or overriding the completion resolution. Prototype in a throwaway script using `main.shell_complete(ctx, incomplete)`.

- [ ] **Step 2: Decide.** If a clean, well-contained hook exists, write a failing test asserting a global flag is offered after a subcommand, implement it, and verify. If it requires overriding Click's internal resolver (fragile), STOP: keep the documented limitation in the `completions` help (already present from Task 3) and record the finding.

- [ ] **Step 3: Commit (only if implemented)**

```bash
git add -A
git commit -m "feat(completions): offer global flags in post-subcommand position

Helped-By: claude-opus-4-8 <anthropic@willroscoe.uk>"
```

---

## Self-Review notes

- **Spec coverage:** generation (T1), install/uninstall/refresh + idempotent marker (T2), CLI command + frozen surface + stability + versionadded (T3), upgrade refresh (T4), position-independent stretch (T5), no-deinit-wiring (constraint, honored by omission). All spec items mapped.
- **Type consistency:** `completion_script(shell)->str`, `install_completion(shell,home=None)->Path`, `uninstall_completion(...)->bool`, `is_installed(...)->bool`, `refresh_installed(home=None)->list[str]`, `refresh_completions_step()->list[str]` — used consistently across tasks.
- **Non-placeholder:** all code and test bodies are concrete; shell-specific `needle` markers in T1 may need adjusting to the exact Click 8.4 output (the test asserts `_PROTONFS_COMPLETE` presence as the stable check; adjust the per-shell needle to observed output if Click's wording differs).
- **Docs build:** after T3/T6 doc edits, run the strict build with the Sphinx-8.x venv, not the machine default.
