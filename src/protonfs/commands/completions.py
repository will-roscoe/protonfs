# src/protonfs/commands/completions.py
"""`protonfs completions {bash,zsh,fish}` — print, install, or remove shell completion.

Drives Click's native completion engine (Click 8+); no third-party dependency. Install
writes the generated script to a file and references it (Click's recommended pattern, so a
new shell does not run protonfs on startup), managed idempotently with a marker block. Global
flags typed after a subcommand are not offered, because Click completes in canonical order.

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
    :returns: the shell completion script source.
    :raises ValueError: for an unsupported shell.
    """
    if shell not in SUPPORTED_SHELLS:
        raise ValueError(f"unsupported shell: {shell!r} (choose from {', '.join(SUPPORTED_SHELLS)})")
    comp_cls = get_completion_class(shell)
    if comp_cls is None:
        raise ValueError(f"Click has no completion class for shell: {shell!r}")
    from protonfs.cli import main  # lazy: avoid circular import at module load

    return comp_cls(main, {}, _PROG_NAME, _COMPLETE_VAR).source()


def _home(home: Path | None) -> Path:
    return Path(home) if home is not None else Path.home()


def _targets(shell: str, home: Path | None) -> tuple[Path, Path | None, str]:
    """Return ``(script_file, rc_file_or_None, source_line)`` for ``shell``."""
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

    :param shell: one of :data:`SUPPORTED_SHELLS`.
    :param home: home directory to install under (defaults to the real home; injectable
        for tests).
    :returns: the path of the written script file.
    """
    script, rc, source_line = _targets(shell, home)
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(completion_script(shell))
    if rc is not None:
        _set_marker_block(rc, source_line)
    return script


def is_installed(shell: str, home: Path | None = None) -> bool:
    """Whether ``shell`` completion is currently installed."""
    script, rc, _ = _targets(shell, home)
    if not script.exists():
        return False
    if rc is None:
        return True
    return rc.exists() and MARKER_BEGIN in rc.read_text()


def uninstall_completion(shell: str, home: Path | None = None) -> bool:
    """Remove the script file and its marker block.

    :returns: ``True`` if anything was removed, ``False`` if nothing was installed.
    """
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
    """Rewrite the script file for every shell that is currently installed.

    :returns: the shells whose scripts were refreshed.
    """
    refreshed = []
    for shell in SUPPORTED_SHELLS:
        if is_installed(shell, home=home):
            script, _, _ = _targets(shell, home)
            script.write_text(completion_script(shell))
            refreshed.append(shell)
    return refreshed
