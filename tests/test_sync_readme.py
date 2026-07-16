"""Tests for the README/docs overview sync (`.github/scripts/sync_readme.py`).

The script is not part of the shipped package (it lives under `.github/scripts/`), so
it is loaded by path via importlib, mirroring the other workflow-script tests.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / ".github" / "scripts" / "sync_readme.py"


def _load():
    spec = importlib.util.spec_from_file_location("sync_readme", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load()
rst_to_markdown = mod.rst_to_markdown


def test_section_headers_by_underline():
    md = rst_to_markdown("Title\n=====\n\nSub\n---\n")
    assert "## Title" in md
    assert "### Sub" in md


def test_inline_literal_and_link_and_bold():
    md = rst_to_markdown("Use ``push`` on `Proton Drive <https://proton.me/drive>`_ **now**.\n")
    assert "`push`" in md
    assert "[Proton Drive](https://proton.me/drive)" in md
    assert "**now**" in md


def test_code_block_becomes_fenced():
    src = ".. code-block:: bash\n\n   pip install protonfs\n   protonfs setup\n"
    md = rst_to_markdown(src)
    assert "```bash" in md
    assert "pip install protonfs" in md
    assert "protonfs setup" in md
    assert md.count("```") == 2


def test_bullet_list_with_wrapped_continuation():
    src = "- first item that\n  wraps onto a second line\n- second item\n"
    md = rst_to_markdown(src)
    assert "- first item that wraps onto a second line" in md
    assert "- second item" in md


def test_comment_lines_are_dropped():
    src = ".. this is an rst comment\n.. spanning two lines\n\nReal paragraph.\n"
    md = rst_to_markdown(src)
    assert "rst comment" not in md
    assert "Real paragraph." in md


def test_paragraph_lines_are_joined():
    src = "This paragraph is\nwrapped across\nthree lines.\n"
    md = rst_to_markdown(src)
    assert "This paragraph is wrapped across three lines." in md


def test_repo_readme_is_in_sync():
    """The committed README block must match the current fragment (guards the CI gate)."""
    readme = mod._README.read_text(encoding="utf-8")
    assert mod._START in readme and mod._END in readme
    assert mod._replace_block(readme, mod.render_block()) == readme, (
        "README overview block is stale; run "
        "`python .github/scripts/sync_readme.py --write`"
    )
