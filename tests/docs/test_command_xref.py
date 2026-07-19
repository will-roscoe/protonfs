"""Unit tests for the docs command-xref extension (pure functions, no Sphinx build)."""
from __future__ import annotations

import sys
from pathlib import Path

# The extension lives in docs/_ext (not importable as a package); add it to the path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "docs" / "_ext"))

import command_xref as cx  # noqa: E402


def test_target_map_has_leaf_and_group_phrases():
    m = cx.build_target_map()
    assert m["push"] == "cmd-push"
    assert m["trash list"] == "cmd-trash-list"
    assert m["config get"] == "cmd-config-get"
    # every mapped label follows the cmd-<dashed-phrase> convention
    for phrase, label in m.items():
        assert label == "cmd-" + phrase.replace(" ", "-")


def test_spans_match_flags_between_program_and_subcommand():
    m = cx.build_target_map()
    spans = cx.find_command_spans("run protonfs --dry-run push now", m)
    assert len(spans) == 1
    start, end, label = spans[0]
    assert label == "cmd-push"
    assert "protonfs --dry-run push" == "run protonfs --dry-run push now"[start:end]


def test_spans_prefer_longest_phrase():
    m = cx.build_target_map()
    spans = cx.find_command_spans("use protonfs trash list here", m)
    assert [s[2] for s in spans] == ["cmd-trash-list"]


def test_spans_ignore_bare_program():
    m = cx.build_target_map()
    assert cx.find_command_spans("just protonfs alone", m) == []


import pytest  # noqa: E402

# The transform tests below need docutils + sphinx (the ``docs`` extra); the plain
# ``.[dev]`` CI test environment does not install them, while the pure-function tests
# above need only the package. Guard the import and skip just the transform tests when
# the extra is absent — the transform is additionally exercised end-to-end by the strict
# docs build (docs.yml), which runs this extension over every page.
try:
    from docutils import nodes
    from sphinx import addnodes

    _HAS_SPHINX = True
except ImportError:  # pragma: no cover - depends on the installed extra
    nodes = addnodes = None  # type: ignore[assignment]
    _HAS_SPHINX = False

_needs_sphinx = pytest.mark.skipif(
    not _HAS_SPHINX, reason="requires docutils+sphinx (the docs extra)"
)


class _FakeEnv:
    def __init__(self, docname):
        self.docname = docname


class _FakeApp:
    def __init__(self, docname):
        self.env = _FakeEnv(docname)


def _para(*children):
    p = nodes.paragraph()
    for c in children:
        p += c
    doc = nodes.document(None, None)
    doc += p
    return doc


@_needs_sphinx
def test_transform_links_plain_text_on_other_pages():
    doc = _para(nodes.Text("First run protonfs --dry-run push to preview."))
    cx.process_command_xrefs(_FakeApp("guarantees"), doc)
    xrefs = list(doc.findall(addnodes.pending_xref))
    assert len(xrefs) == 1
    assert xrefs[0]["reftarget"] == "cmd-push"
    assert xrefs[0].astext() == "protonfs --dry-run push"


@_needs_sphinx
def test_transform_links_inline_literal():
    doc = _para(nodes.literal("protonfs pull", "protonfs pull"))
    cx.process_command_xrefs(_FakeApp("api/argv"), doc)
    xrefs = list(doc.findall(addnodes.pending_xref))
    assert len(xrefs) == 1 and xrefs[0]["reftarget"] == "cmd-pull"


@_needs_sphinx
def test_transform_skips_literal_block():
    block = nodes.literal_block("protonfs push", "protonfs push")
    doc = nodes.document(None, None)
    doc += block
    cx.process_command_xrefs(_FakeApp("guarantees"), doc)
    assert list(doc.findall(addnodes.pending_xref)) == []


@_needs_sphinx
def test_transform_skips_reference_page_itself():
    doc = _para(nodes.Text("protonfs push here"))
    cx.process_command_xrefs(_FakeApp("reference/index"), doc)
    assert list(doc.findall(addnodes.pending_xref)) == []
