# Command Reference Restructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unify the three command-reference pages into one hierarchy, replace the duplicated hand synopsis/options with per-command sphinx-click autodoc, and add an automatic cross-page linker for `protonfs <subcommand>` mentions — with the strict `-W` docs build staying green.

**Architecture:** The single page `docs/reference/index.rst` becomes `Command Reference → Configuration (env vars, config keys) → Subcommands`. Each subcommand section embeds a single-command sphinx-click block plus hand prose. A local Sphinx extension (`docs/_ext/command_xref.py`) rewrites cross-page command mentions into `:ref:` cross-references to the `.. _cmd-*:` anchors. The restructure lands before the linker so every anchor the linker targets already exists.

**Tech Stack:** Sphinx (`>=8.1,<9`), sphinx-click, furo, docutils, pytest.

## Global Constraints

- Docs build MUST stay clean under `sphinx-build -W --keep-going -b html docs/ docs/_build/html` (warnings are errors).
- Build docs in a **clean venv**, not the system `sphinx-build` (system version is wrong; see `[[protonfs-docs-ci-gotchas]]`).
- Preserve every cross-ref target: `.. _cmd-*:` labels, `.. confval::` keys, `.. envvar::` names, `:option:` targets. No target renamed; only additions allowed.
- Preserve all Spec B version directives (`.. versionadded/changed/deprecated::`) — move, never drop.
- No CLI/behaviour/dependency change. Extension is local `docs/_ext`, stdlib + Sphinx only.
- Commit style: conventional commits; single trailer `Helped-By: claude-opus-4-8 <anthropic@willroscoe.uk>`.
- Do NOT commit anything under `.claude/` (specs/plans stay local).

---

### Task 1: Spike — single-command sphinx-click rendering

Decide the concrete directive that renders exactly one subcommand cleanly. Everything downstream assumes "a per-command autodoc block exists"; this locks the mechanism.

**Files:**
- Create (throwaway): `/tmp/claude-1000/-win-e-Repositories-protonfs/c99190f4-9833-4598-ba11-5408d6c3b676/scratchpad/spike/one.rst`

**Interfaces:**
- Produces: the confirmed directive form used verbatim by Task 3 (either the `:commands:` filter or a fallback).

- [ ] **Step 1: Write a minimal one-command doc**

Create `.../scratchpad/spike/one.rst`:

```rst
push (spike)
============

.. click:: protonfs.cli:main
   :prog: protonfs
   :commands: push
   :nested: none
```

- [ ] **Step 2: Build it in the clean docs venv and inspect the HTML**

```bash
cd /win/e/Repositories/protonfs
python3 -m venv /tmp/ci-docs-venv && . /tmp/ci-docs-venv/bin/activate
pip install -e ".[docs]" >/dev/null
SP=/tmp/claude-1000/-win-e-Repositories-protonfs/c99190f4-9833-4598-ba11-5408d6c3b676/scratchpad/spike
cp docs/conf.py "$SP/conf.py"
sphinx-build -q -b html "$SP" "$SP/_build" 2>&1 | tail -20
grep -c "dry-run" "$SP/_build/one.html"     # push HAS --dry-run  -> expect >=1
grep -c "resolve" "$SP/_build/one.html"     # push HAS --resolve  -> expect >=1
grep -c "migrate-lfs" "$SP/_build/one.html" # setup-only flag -> expect 0 (no other cmds leaked)
```

Expected: `--dry-run`/`--resolve` present; `migrate-lfs` count `0` (only `push` rendered).

- [ ] **Step 3: Record the decision**

- If Step 2 shows only `push` with no leaked parent-group header/usage: the design directive is used as-is in Task 3. Note "`:commands:` clean" in the commit message body.
- If a redundant group header leaks (a stray `protonfs [OPTIONS] COMMAND [ARGS]` synopsis or other commands): the Task 7 CSS will additionally hide `section[id^="cmd-"] > .click-... ` group headers; record the exact leaked element's HTML (`grep -A2 -B2 "COMMAND \[ARGS\]" "$SP/_build/one.html"`) so Task 7 can target it. Do NOT change the directive form — `:commands:` + CSS suppression is the fallback.

- [ ] **Step 4: Commit (decision only — no repo files changed yet)**

No repo changes in this task; the spike output is in scratchpad. Proceed to Task 2. (If you prefer a record, add the finding to the plan file — but that's under `.claude/`, so it stays uncommitted.)

---

### Task 2: Root synopsis + merge Configuration into the single page

Add the top-level `protonfs` synopsis to `index.rst`, move `config.rst`'s confval/envvar blocks into a `Configuration` section, delete `config.rst`, and repoint its referrers. Build stays green.

**Files:**
- Modify: `docs/reference/index.rst` (add root `.. click::` block near top; add `Configuration` H2 with the moved blocks; drop `config` from the hidden toctree)
- Read (source of blocks): `docs/reference/config.rst`
- Delete: `docs/reference/config.rst`
- Modify (repoint): any file with `:doc:\`config\`` / `:doc:\`reference/config\`` — check `docs/reference/index.rst` "See also", `docs/stability.rst`

**Interfaces:**
- Produces: a `Configuration` H2 in `docs/reference/index.rst` containing all `.. confval::` and `.. envvar::` blocks (same target names), and a root `protonfs` synopsis block.

- [ ] **Step 1: Find every referrer of the config page**

```bash
cd /win/e/Repositories/protonfs
grep -rn ":doc:\`config\`\|:doc:\`reference/config\`\|:doc:\`../reference/config\`" docs/
grep -rn "reference/config\|^\s*config$" docs/reference/index.rst
```

Note each hit; every one must be repointed or removed in Step 4/5.

- [ ] **Step 2: Add the root synopsis block**

In `docs/reference/index.rst`, immediately after the intro paragraph (before "Global behavior"), insert:

```rst
Synopsis
--------
.. click:: protonfs.cli:main
   :prog: protonfs
   :nested: none

``protonfs`` is the command group; every operation is a subcommand documented under
:ref:`Subcommands <reference-subcommands>`. The global options above (verbosity, progress
style, event log) may appear before or after the subcommand.

Examples::

    protonfs setup && protonfs push        # first-time setup, then upload
    protonfs refresh && protonfs pull       # first pull on a new machine
    protonfs status; echo "exit=$?"         # drift check for scripts
```

- [ ] **Step 3: Add the Configuration section with the moved blocks**

Read `docs/reference/config.rst` in full. Insert a new H2 into `index.rst` **before** the `Subcommands` content (i.e. before `.. _cmd-setup:`), preserving every `.. confval::`/`.. envvar::` block and its `.. versionadded::` verbatim:

```rst
Configuration
=============

Environment variables
---------------------

<all `.. envvar::` blocks from config.rst, verbatim — including the operational/tuning
 env-var section from PR #110>

Configuration file and keys
--------------------------

<all `.. confval::` blocks from config.rst, verbatim>
```

Copy the blocks exactly; do not rewrite prose or rename targets.

- [ ] **Step 4: Delete config.rst and drop it from the toctree**

```bash
git rm docs/reference/config.rst
```

In `docs/reference/index.rst`, edit the hidden toctree from:

```rst
.. toctree::
   :hidden:

   commands
   config
```

to (leave `commands` for now — Task 3 removes it):

```rst
.. toctree::
   :hidden:

   commands
```

- [ ] **Step 5: Repoint referrers**

For each hit from Step 1: replace `:doc:\`config\`` with `:doc:\`index\`` (same-dir) or the more specific role where the text names a key/var — prefer `:confval:\`remote_root\`` / `:envvar:\`PROTONFS_CONFIG\``, which already resolve to the moved blocks. Update the "See also" bullet in `index.rst` that points at `:doc:\`config\`` to describe the in-page Configuration section instead.

- [ ] **Step 6: Build strict; expect green**

```bash
cd /win/e/Repositories/protonfs && . /tmp/ci-docs-venv/bin/activate
sphinx-build -W --keep-going -b html docs/ docs/_build/html 2>&1 | tail -15
```

Expected: `build succeeded`, zero warnings. Any "unknown document config" or "undefined label" means a Step 5 referrer was missed — fix and rebuild.

- [ ] **Step 7: Commit**

```bash
git add docs/reference/index.rst docs/stability.rst
git rm docs/reference/config.rst 2>/dev/null; git add -A docs/
git commit -m "docs(reference): merge config keys/env vars into single Command Reference page

Fold docs/reference/config.rst's confval/envvar definitions into a Configuration
section of reference/index.rst and add a root protonfs synopsis; repoint referrers.
All target names unchanged.

Helped-By: claude-opus-4-8 <anthropic@willroscoe.uk>"
```

---

### Task 3: Convert subcommands to the autodoc+prose hybrid

Replace each hand-written `**Synopsis:**` + option bullets with a single-command sphinx-click block (from Task 1), keep the prose + examples, add the missing group/leaf anchors, delete `commands.rst`, repoint its referrers.

**Files:**
- Modify: `docs/reference/index.rst` (every `.. _cmd-*:` section)
- Delete: `docs/reference/commands.rst`
- Modify (repoint): referrers of `:doc:\`commands\``

**Interfaces:**
- Consumes: the **verified** directive form from Task 1 (spike result — the plan's original `:commands:` form does NOT work on sphinx-click 6.2.0; use direct import).
- Produces: for every command, a `.. _cmd-<name>:` anchor whose section holds a single-command `.. click:: protonfs.cli:<attr>` block. New anchors this task adds: `.. _cmd-completions:`, `.. _cmd-trash:`, `.. _cmd-config-get:`, `.. _cmd-config-set:`. These anchor names are the exact `:ref:` targets Task 4's map produces (Task 4's map is derived from the live Click app, so EVERY command — including `completions` — must have a matching `cmd-<name>` anchor or the `-W` build fails when a page mentions it).

**VERIFIED directive form (use this, not `:commands:`):** point the directive straight at each command's importable module attribute in `protonfs.cli`, with `:prog:` set to the full command path. No `:commands:`/`:nested:`. The attribute names (confirmed by introspection):

| command | `.. click:: protonfs.cli:<attr>` | `:prog:` |
|---|---|---|
| setup | `setup` | `protonfs setup` |
| deinit | `deinit` | `protonfs deinit` |
| status | `status` | `protonfs status` |
| ls | `ls` | `protonfs ls` |
| push | `push` | `protonfs push` |
| pull | `pull` | `protonfs pull` |
| offload | `offload` | `protonfs offload` |
| rm | `rm` | `protonfs rm` |
| restore | `restore` | `protonfs restore` |
| refresh | `refresh` | `protonfs refresh` |
| install-drive | `install_drive_cmd` | `protonfs install-drive` |
| upgrade | `upgrade` | `protonfs upgrade` |
| doctor | `doctor` | `protonfs doctor` |
| shell-init | `shell_init` | `protonfs shell-init` |
| completions | `completions` | `protonfs completions` |
| auth (group) | `auth` | `protonfs auth` |
| trash (group) | `trash` | `protonfs trash` |
| trash list | `trash_list_cmd` | `protonfs trash list` |
| trash empty | `trash_empty_cmd` | `protonfs trash empty` |
| config (group) | `config` | `protonfs config` |
| config get | `config_get_cmd` | `protonfs config get` |
| config set | `config_set_cmd` | `protonfs config set` |

- [ ] **Step 1: Add the Subcommands heading (anchor already exists)**

Task 2 already added the bare label `.. _reference-subcommands:` in `index.rst` just before `.. _cmd-setup:`. Do NOT re-declare it (a duplicate label fails the `-W` build). Add the H2 heading directly under the existing anchor:

```rst
.. _reference-subcommands:

Subcommands
===========
```

If the anchor is already immediately followed by a heading, leave it; otherwise insert only the `Subcommands\n===========` heading after the existing label line. Then demote each command from H2 `----` to H3 `~~~~` under it, and group leaf commands to H4 `^^^^`.

- [ ] **Step 2: Convert a leaf command (do `push` first as the template)**

Replace the `push` section body. From:

```rst
.. _cmd-push:

``push``
--------
**Synopsis:** ``protonfs push [PATH]... [--resolve ...] [--dry-run]``

.. versionchanged:: 1.1.0
   ...

Uploads local-only ...

- ``--resolve`` — ...
- ``--dry-run`` — ...

Examples::
    ...
```

to:

```rst
.. _cmd-push:

push
~~~~
.. click:: protonfs.cli:push
   :prog: protonfs push

.. versionchanged:: 1.1.0
   Interactive batch progress on stderr; accepts multiple ``PATH`` pathspecs.

Uploads local-only and locally-modified files under ``PATH`` (or the whole repo)
to Drive. <keep the full existing prose paragraph(s) — behaviour, verification,
LFS-stub guard, resume>.

Examples::

    protonfs push                         # everything in scope that is new/changed
    protonfs push subdir/ --resolve replace
    protonfs push --dry-run
```

Rule for every command: **delete** the `**Synopsis:**` line and the bare option bullets that only restate the option help (sphinx-click now emits both). **Keep** prose that adds behavioural nuance beyond the option's one-liner, all examples, and all version directives.

- [ ] **Step 3: Convert the remaining leaf commands**

Apply the same transform to: `setup`, `deinit`, `status`, `ls`, `pull`, `offload`, `rm`, `restore`, `refresh`, `install-drive`, `upgrade`, `doctor`, `shell-init`. Each uses its own `.. click:: protonfs.cli:<attr>` + `:prog:` from the mapping table above (mind the suffixed attrs: `install-drive`→`install_drive_cmd`, `shell-init`→`shell_init`); keep prose/examples/version directives, drop synopsis+bare-bullets.

- [ ] **Step 3b: Add a `completions` entry (new — no prose exists yet)**

`completions` (shipped PR #111) has no section in `index.rst`. Add one under Subcommands, in command order after `shell-init`:

```rst
.. _cmd-completions:

completions
~~~~~~~~~~~
.. click:: protonfs.cli:completions
   :prog: protonfs completions

Prints, installs, or removes shell completion for bash, zsh, or fish. With
``--install`` it writes the generated script and wires it into your shell config
(idempotent, marker-delimited); ``--uninstall`` removes it. Installed completions are
refreshed automatically by :ref:`protonfs upgrade <cmd-upgrade>`. Global flags
complete after a subcommand too, matching the position-independent argv handling.

.. versionadded:: 1.5.0

Examples::

    protonfs completions bash              # print the script to stdout
    protonfs completions zsh --install     # install + wire into ~/.zshrc
    protonfs completions fish --uninstall
```

- [ ] **Step 4: Convert the groups (`auth`, `config`, `trash`) with per-leaf blocks**

Groups get a group-overview block (`.. click:: protonfs.cli:<group>`, which renders the group's own usage plus a short subcommand list) followed by one direct-import block **per leaf**. `auth` keeps `.. _cmd-auth:`. `config`:

```rst
.. _cmd-config:

config
~~~~~~
.. click:: protonfs.cli:config
   :prog: protonfs config

<group prose: layered config, resolved value, precedence — keep existing config prose>

.. _cmd-config-get:

config get
^^^^^^^^^^
.. click:: protonfs.cli:config_get_cmd
   :prog: protonfs config get

<get prose + examples>

.. _cmd-config-set:

config set
^^^^^^^^^^
.. click:: protonfs.cli:config_set_cmd
   :prog: protonfs config set

<set prose: the three layers, mutual exclusion + examples>
```

For `trash`, add a new group anchor + section, then the existing leaf sections demoted to H4 with direct-import blocks:

```rst
.. _cmd-trash:

trash
~~~~~
.. click:: protonfs.cli:trash
   :prog: protonfs trash

<one-line group intro>

.. _cmd-trash-list:

trash list
^^^^^^^^^^
.. click:: protonfs.cli:trash_list_cmd
   :prog: protonfs trash list

<existing trash-list prose + examples>

.. _cmd-trash-empty:

trash empty
^^^^^^^^^^^
.. click:: protonfs.cli:trash_empty_cmd
   :prog: protonfs trash empty

<existing trash-empty prose + examples>
```

`auth` is a group but its login/logout/status are passthrough actions (one `action` argument, not sub-Commands), so `auth` has no leaf blocks — one `.. click:: protonfs.cli:auth` block + the existing auth prose is correct.

> Underline length rule (docutils): the `~~~~`/`^^^^` underline must be at least as long as the title text, else a "Title underline too short" warning fails the `-W` build. `trash empty`/`config get` etc. need `^^^^^^^^^^^` matching their length.

- [ ] **Step 5: Delete commands.rst and repoint referrers**

```bash
cd /win/e/Repositories/protonfs
grep -rn ":doc:\`commands\`\|:doc:\`reference/commands\`\|:doc:\`../reference/commands\`" docs/
git rm docs/reference/commands.rst
```

Remove `commands` from the hidden toctree in `index.rst` (the toctree block is now empty — delete the whole `.. toctree:: :hidden:` block). Repoint each referrer to `:doc:\`index\`` or the specific `:ref:`/`:option:` role.

- [ ] **Step 6: Build strict; expect green**

```bash
. /tmp/ci-docs-venv/bin/activate
sphinx-build -W --keep-going -b html docs/ docs/_build/html 2>&1 | tail -20
```

Expected: `build succeeded`, zero warnings. Watch specifically for "duplicate label cmd-*" (a leftover anchor) or "unknown document commands".

- [ ] **Step 7: Verify anchors + no leaked commands per section**

```bash
grep -c "^\.\. _cmd-" docs/reference/index.rst   # expect one per command + groups (>= 20)
grep -n "^\.\. _cmd-trash:\|^\.\. _cmd-config-get:\|^\.\. _cmd-config-set:" docs/reference/index.rst
```

Expected: the three new anchors present.

- [ ] **Step 8: Commit**

```bash
git add -A docs/
git commit -m "docs(reference): render each subcommand via single-command sphinx-click autodoc

Replace the duplicated hand-written synopsis/option bullets with per-command
.. click:: blocks (one source of truth for the mechanical contract); keep the
behavioural prose, examples, and version directives. Delete the standalone
commands.rst autodoc page. Add cmd-trash/cmd-config-get/cmd-config-set anchors.

Helped-By: claude-opus-4-8 <anthropic@willroscoe.uk>"
```

---

### Task 4: Auto-linker — target map + span matcher (pure functions, TDD)

Build the two pure functions the extension needs: derive the phrase→label map from the live Click app, and find linkable spans in a string. No docutils yet.

**Files:**
- Create: `docs/_ext/command_xref.py`
- Test: `tests/docs/test_command_xref.py`

**Interfaces:**
- Produces:
  - `build_target_map() -> dict[str, str]` — `{"push": "cmd-push", "trash list": "cmd-trash-list", "config get": "cmd-config-get", ...}` derived from `protonfs.cli.main`.
  - `find_command_spans(text: str, target_map: dict[str, str]) -> list[tuple[int, int, str]]` — `(start, end, label)` for each `protonfs [flags] <phrase>` occurrence, longest-phrase-first, non-overlapping.

- [ ] **Step 1: Write failing tests**

Create `tests/docs/test_command_xref.py`:

```python
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
```

- [ ] **Step 2: Run tests; verify they fail**

Run: `python -m pytest tests/docs/test_command_xref.py -q`
Expected: FAIL — `ModuleNotFoundError: command_xref` (file not created yet).

- [ ] **Step 3: Implement the pure functions**

Create `docs/_ext/command_xref.py`:

```python
"""Sphinx extension: auto-link cross-page ``protonfs <subcommand>`` mentions.

Rewrites plain-text and inline-literal mentions of a command (e.g. ``protonfs
--dry-run push``) on pages *other than the command reference itself* into ``:ref:``
cross-references to that command's ``.. _cmd-*:`` anchor. Shell-example blocks
(``literal_block``) are left untouched.
"""
from __future__ import annotations

import re


def build_target_map() -> dict[str, str]:
    """Map subcommand phrases to their ``.. _cmd-*:`` ref labels, from the live app."""
    from protonfs.cli import main

    target_map: dict[str, str] = {}
    for name, cmd in main.commands.items():
        target_map[name] = f"cmd-{name}"
        subcommands = getattr(cmd, "commands", None)
        if subcommands:
            for sub_name in subcommands:
                target_map[f"{name} {sub_name}"] = f"cmd-{name}-{sub_name}"
    return target_map


_FLAGS = r"(?:\s+-{1,2}[A-Za-z][\w-]*)*"


def _compile(target_map: dict[str, str]) -> re.Pattern[str]:
    # Longest phrase first so "trash list" wins over "trash".
    phrases = sorted(target_map, key=len, reverse=True)
    alternation = "|".join(re.escape(p) for p in phrases)
    return re.compile(rf"\bprotonfs{_FLAGS}\s+({alternation})\b")


def find_command_spans(
    text: str, target_map: dict[str, str]
) -> list[tuple[int, int, str]]:
    """Return non-overlapping ``(start, end, label)`` matches in ``text``."""
    pattern = _compile(target_map)
    return [
        (m.start(), m.end(), target_map[m.group(1)]) for m in pattern.finditer(text)
    ]
```

- [ ] **Step 4: Run tests; verify they pass**

Run: `python -m pytest tests/docs/test_command_xref.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add docs/_ext/command_xref.py tests/docs/test_command_xref.py
git commit -m "docs(ext): add command-xref target map + span matcher

Pure functions for the auto-linker: derive the {phrase: cmd-label} map from the
live Click app and find protonfs-subcommand spans in text (flags allowed between
program and subcommand, longest phrase wins).

Helped-By: claude-opus-4-8 <anthropic@willroscoe.uk>"
```

---

### Task 5: Auto-linker — doctree transform + registration

Add the doctree-read handler that rewrites matches into `pending_xref` nodes (skipping example blocks and the reference page itself), and register the extension in `conf.py`.

**Files:**
- Modify: `docs/_ext/command_xref.py` (add node transform + `setup`)
- Modify: `docs/conf.py` (add `_ext` to `sys.path`, add extension)
- Test: `tests/docs/test_command_xref.py` (add a docutils-level transform test)

**Interfaces:**
- Consumes: `build_target_map`, `find_command_spans` (Task 4).
- Produces: `process_command_xrefs(app, doctree) -> None`, `setup(app) -> dict`.

- [ ] **Step 1: Write the failing transform test**

Append to `tests/docs/test_command_xref.py`:

```python
from docutils import nodes  # noqa: E402
from sphinx import addnodes  # noqa: E402


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


def test_transform_links_plain_text_on_other_pages():
    doc = _para(nodes.Text("First run protonfs --dry-run push to preview."))
    cx.process_command_xrefs(_FakeApp("guarantees"), doc)
    xrefs = list(doc.findall(addnodes.pending_xref))
    assert len(xrefs) == 1
    assert xrefs[0]["reftarget"] == "cmd-push"
    assert xrefs[0].astext() == "protonfs --dry-run push"


def test_transform_links_inline_literal():
    doc = _para(nodes.literal("protonfs pull", "protonfs pull"))
    cx.process_command_xrefs(_FakeApp("api/argv"), doc)
    xrefs = list(doc.findall(addnodes.pending_xref))
    assert len(xrefs) == 1 and xrefs[0]["reftarget"] == "cmd-pull"


def test_transform_skips_literal_block():
    block = nodes.literal_block("protonfs push", "protonfs push")
    doc = nodes.document(None, None)
    doc += block
    cx.process_command_xrefs(_FakeApp("guarantees"), doc)
    assert list(doc.findall(addnodes.pending_xref)) == []


def test_transform_skips_reference_page_itself():
    doc = _para(nodes.Text("protonfs push here"))
    cx.process_command_xrefs(_FakeApp("reference/index"), doc)
    assert list(doc.findall(addnodes.pending_xref)) == []
```

- [ ] **Step 2: Run tests; verify the new ones fail**

Run: `python -m pytest tests/docs/test_command_xref.py -q`
Expected: FAIL — `AttributeError: module 'command_xref' has no attribute 'process_command_xrefs'`.

- [ ] **Step 3: Implement the transform + setup**

Append to `docs/_ext/command_xref.py`:

```python
from docutils import nodes
from sphinx import addnodes

# Text inside these nodes is never linkified: shell examples, already-linked spans,
# and (during the plain-text pass) inline literals, which the literal pass handles.
_SKIP_PARENTS = (
    nodes.literal_block,
    nodes.doctest_block,
    nodes.FixedTextElement,
    nodes.reference,
    addnodes.pending_xref,
    nodes.comment,
)

_REFERENCE_DOCNAME = "reference/index"


def _under(node: nodes.Node, types: tuple[type, ...]) -> bool:
    parent = node.parent
    while parent is not None:
        if isinstance(parent, types):
            return True
        parent = parent.parent
    return False


def _make_xref(label: str, text: str, *, literal: bool) -> addnodes.pending_xref:
    ref = addnodes.pending_xref(
        "",
        refdomain="std",
        reftype="ref",
        reftarget=label,
        refexplicit=True,
        refwarn=True,
    )
    ref += nodes.literal(text, text) if literal else nodes.inline(text, text)
    return ref


def process_command_xrefs(app, doctree) -> None:
    """Rewrite cross-page ``protonfs <subcommand>`` mentions into ``:ref:`` xrefs."""
    if app.env.docname == _REFERENCE_DOCNAME:
        return
    target_map = build_target_map()

    # Pass 1: whole inline literals that are exactly a command phrase.
    for literal in list(doctree.findall(nodes.literal)):
        if _under(literal, _SKIP_PARENTS):
            continue
        spans = find_command_spans(literal.astext(), target_map)
        if len(spans) == 1 and spans[0][0] == 0 and spans[0][1] == len(literal.astext()):
            literal.replace_self(_make_xref(spans[0][2], literal.astext(), literal=True))

    # Pass 2: plain text nodes (skip literals — handled above — and skip blocks).
    for text_node in list(doctree.findall(nodes.Text)):
        if _under(text_node, _SKIP_PARENTS + (nodes.literal,)):
            continue
        source = text_node.astext()
        spans = find_command_spans(source, target_map)
        if not spans:
            continue
        new_nodes: list[nodes.Node] = []
        pos = 0
        for start, end, label in spans:
            if start > pos:
                new_nodes.append(nodes.Text(source[pos:start]))
            new_nodes.append(_make_xref(label, source[start:end], literal=False))
            pos = end
        if pos < len(source):
            new_nodes.append(nodes.Text(source[pos:]))
        text_node.parent.replace(text_node, new_nodes)


def setup(app):
    app.connect("doctree-read", process_command_xrefs)
    return {
        "version": "1.0",
        "parallel_read_safe": True,
        "parallel_write_safe": True,
    }
```

- [ ] **Step 4: Run tests; verify all pass**

Run: `python -m pytest tests/docs/test_command_xref.py -q`
Expected: PASS (8 passed).

- [ ] **Step 5: Register the extension in conf.py**

In `docs/conf.py`, extend the path setup (after line 6 `sys.path.insert(0, os.path.abspath("../src"))`):

```python
sys.path.insert(0, os.path.abspath("_ext"))
```

Add `"command_xref"` to the `extensions` list (end of the list, after `"sphinx_tippy"`):

```python
    "sphinx_tippy",
    # Local: auto-link cross-page `protonfs <subcommand>` mentions to their reference entry.
    "command_xref",
```

- [ ] **Step 6: Strict build; verify cross-page links resolve**

```bash
. /tmp/ci-docs-venv/bin/activate
sphinx-build -W --keep-going -b html docs/ docs/_build/html 2>&1 | tail -15
# argv.py's ``protonfs --dry-run push`` must now be a link to the push entry:
grep -o 'href="[^"]*#cmd-push"' docs/_build/html/api/protonfs.argv.html | head
```

Expected: `build succeeded`, zero warnings; the grep prints at least one `#cmd-push` link (confirming the argv page's `protonfs --dry-run push` mention is now linked). If the argv page path differs, locate it: `grep -rl "reorder_argv" docs/_build/html`.

- [ ] **Step 7: Commit**

```bash
git add docs/_ext/command_xref.py tests/docs/test_command_xref.py docs/conf.py
git commit -m "docs(ext): auto-link cross-page protonfs subcommand mentions

Register a doctree-read transform that rewrites 'protonfs <sub>' in prose and
inline literals (not shell-example blocks, not the reference page itself) into
:ref: cross-references to the cmd-<name> anchors.

Helped-By: claude-opus-4-8 <anthropic@willroscoe.uk>"
```

---

### Task 6: Visual separation stylesheet

Add a light per-command separator via a new stylesheet, registered in `conf.py`. If Task 1 found a leaked group header, suppress it here too.

**Files:**
- Create: `docs/_static/custom.css`
- Modify: `docs/conf.py` (add `html_css_files`)

**Interfaces:** none (pure styling).

- [ ] **Step 1: Create the stylesheet**

Create `docs/_static/custom.css`:

```css
/* Command Reference: delimit each subcommand section (`.. _cmd-*:` -> section id). */
section[id^="cmd-"] {
  border-top: 1px solid var(--color-background-border);
  padding-top: 1.25rem;
  margin-top: 1.75rem;
}
/* Nested group leaves (config get / trash empty) get a lighter rule. */
section[id^="cmd-"] section[id^="cmd-"] {
  border-top-style: dashed;
  margin-top: 1.25rem;
}
```

If Task 1 recorded a leaked group header element, append a rule hiding it within `section[id^="cmd-"]` (use the exact selector recorded in Task 1 — e.g. the generated group synopsis `<dl>`/`<p>`).

- [ ] **Step 2: Register the stylesheet**

In `docs/conf.py`, after `html_static_path = ["_static"]`:

```python
html_css_files = ["custom.css"]
```

- [ ] **Step 3: Build and confirm the CSS is emitted and referenced**

```bash
. /tmp/ci-docs-venv/bin/activate
sphinx-build -W --keep-going -b html docs/ docs/_build/html 2>&1 | tail -8
test -f docs/_build/html/_static/custom.css && echo "css copied"
grep -c "custom.css" docs/_build/html/reference/index.html   # expect >=1
```

Expected: build succeeded; `css copied`; the reference page references `custom.css`.

- [ ] **Step 4: Commit**

```bash
git add docs/_static/custom.css docs/conf.py
git commit -m "docs(reference): visual separators between command sections

Add docs/_static/custom.css with a top rule + spacing on section[id^=\"cmd-\"] so
each subcommand entry is clearly delimited; register via html_css_files.

Helped-By: claude-opus-4-8 <anthropic@willroscoe.uk>"
```

---

### Task 7: Full verification — strict build, coverage gate, unit tests, link audit

Final green-light: the whole suite plus the authoritative clean-venv docs build and the doc-coverage gate, and a spot audit that the linker did its job across pages.

**Files:** none (verification only).

- [ ] **Step 1: Run the extension unit tests**

Run: `python -m pytest tests/docs/test_command_xref.py -v`
Expected: PASS (8 passed).

- [ ] **Step 2: Run the full test suite (nothing else regressed)**

Run: `python -m pytest -q`
Expected: PASS — same count as before plus the 8 new tests; the CLI-surface freeze test unaffected.

- [ ] **Step 3: Authoritative strict docs build in the clean venv**

```bash
. /tmp/ci-docs-venv/bin/activate
rm -rf docs/_build
sphinx-build -W --keep-going -b html docs/ docs/_build/html 2>&1 | tail -20
```

Expected: `build succeeded`, zero warnings.

- [ ] **Step 4: Doc-coverage gate**

```bash
sphinx-build -b coverage docs/ docs/_build/coverage 2>&1 | tail -5
cat docs/_build/coverage/python.txt
```

Expected: no undocumented objects (empty/`0` undocumented) — same as before this change.

- [ ] **Step 5: Cross-page link audit**

```bash
# Pages that mention commands should now carry cmd-* links; the reference page must NOT self-link.
grep -rl '#cmd-' docs/_build/html/api/ | head
grep -c 'href="[^"]*#cmd-' docs/_build/html/reference/index.html   # expect 0 (no self-links)
```

Expected: at least one API page links to `#cmd-*`; the reference page has `0` command self-links.

- [ ] **Step 6: Deactivate venv**

Run: `deactivate`

- [ ] **Step 7: No commit** — verification only. If anything failed, fix in the owning task and re-run this task.

---

## Notes for the executor

- The `[docs]` extra and Sphinx pin are already in `pyproject.toml` (PR #109); no dependency edits needed.
- `docs/_ext/` is a new directory; the extension is a bare module (not a package) imported via the `sys.path` entry — mirror that in the test with the `sys.path.insert` shown in Task 4.
- Task 1 (spike, DONE) established that `:commands:` + `:nested: none` does NOT work on sphinx-click 6.2.0 — it renders the root group's own doc and drops the target command. Task 3 uses the verified direct-import form (`.. click:: protonfs.cli:<attr>` + `:prog:`) per the mapping table in Task 3. What Task 4/5 depend on is the set of `cmd-<name>` anchors existing, one per live command (including `completions`).
- Keep the `docs/reference/index.rst` "See also" section, repointed to in-page sections and the still-existing `stability`/`guarantees`/getting-started pages.
```
