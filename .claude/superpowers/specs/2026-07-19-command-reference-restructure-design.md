# Spec C — Command Reference restructure (design)

Date: 2026-07-19
Status: approved (design), ready for implementation plan.
Origin: user request 2026-07-18; reworks the docs structure built in Spec A (PR #107).
Supersedes: `.claude/superpowers/specs/2026-07-18-command-reference-restructure-REQUIREMENTS.md`
(requirements capture).

## Goal

Unify the three separate command-reference pages into one page matching the user's
hierarchy, remove the synopsis/options duplication between the autodoc page and the
hand-written narrative, restore per-command prose+examples as first-class content, and add
**automatic** cross-page linking of `protonfs <subcommand>` mentions to their reference
entries — all while keeping the strict `-W` build and the Spec A/B cross-reference targets
intact.

## Global constraints (carry into every task)

- Docs build stays **`sphinx-build -W --keep-going -b html`** clean (warnings are errors).
- Preserve every existing cross-ref target: `.. _cmd-*:` labels, `.. confval::` keys,
  `.. envvar::` names, and the `:option:` targets sphinx-click emits. Every existing
  `:confval:`/`:envvar:`/`:ref:`/`:doc:` reference across the docs must still resolve.
- Preserve the version directives (Spec B) — `.. versionadded/changed/deprecated::` blocks
  move with their prose, none are dropped.
- Keep the doc-coverage CI gate and the `pull_request` docs build trigger (from PR #109)
  working.
- Sphinx pinned `>=8.1,<9`, sphinx-click present (unchanged from PR #107/#109).

## Current state (what we're changing)

- `docs/reference/commands.rst` — pure sphinx-click autodoc dump (`.. click:: protonfs.cli:main`
  `:nested: full`). The mechanical contract for *every* command on one page.
- `docs/reference/index.rst` (603 lines) — hand-written narrative: for **every** command a
  `.. _cmd-<name>:` label, an H2 title, a `**Synopsis:**` line, option bullets, prose, and
  `Examples::`. Also holds the global "Global behavior" + "Diagnostics & verbosity" prose.
- `docs/reference/config.rst` — `.. confval::` per config key and `.. envvar::` per env var
  (with versionadded), plus an operational/tuning env-var section (PR #110).

Problem: `commands.rst` and `index.rst` are **two sources of truth** for each command's
synopsis + options. The autodoc dump and the hand prose live on separate pages, the prose
mentions of commands don't link anywhere, and the per-command sections lack strong visual
separation.

## Target architecture

### Single-page structure

```
Command Reference                         (H1, docs/reference/index.rst)
  <root `protonfs` synopsis>              .. click:: protonfs.cli:main  :nested: none
  <global-behavior prose>                 lock, error boundary
  Diagnostics & verbosity                 (H2)  -v ladder / progress / event-log (unchanged prose)
  Configuration                           (H2)
    Environment variables                 (H3)  .. envvar:: blocks (moved from config.rst)
    Configuration file and keys           (H3)  .. confval:: blocks (moved from config.rst)
  Subcommands                             (H2)
    setup                                 (H3, .. _cmd-setup:)  autodoc + prose + examples
    deinit … doctor … shell-init          (H3 each)
    auth                                  (H3, group: login/logout/status prose)
    config                                (H3, group)  → config get / config set  (H4)
    trash                                 (H3, group)  → trash list / trash empty  (H4)
```

- `docs/reference/commands.rst` — **deleted**; its autodoc is inlined per-command.
- `docs/reference/config.rst` — **deleted**; its `.. confval::`/`.. envvar::` blocks move
  verbatim into the `Configuration` H2. All confval/envvar target names are unchanged.
- `docs/reference/index.rst` — becomes the whole page. Its hidden `.. toctree::` to
  `commands`/`config` is removed.
- `docs/stability.rst` and any other page linking `:doc:\`reference/commands\`` /
  `:doc:\`reference/config\`` are repointed to `:doc:\`reference/index\`` (or the relevant
  `:ref:` / `:confval:` / `:envvar:` role, which is preferable and mostly already used).

### Per-command section template (the "hybrid")

```rst
.. _cmd-push:

push
~~~~
.. click:: protonfs.cli:main
   :prog: protonfs
   :commands: push
   :nested: none

.. versionchanged:: 1.1.0
   Interactive batch progress on stderr; accepts multiple ``PATH`` pathspecs.

<hand-written prose: behaviour, guarantees, resume semantics>

Examples::

    protonfs push --dry-run
```

- sphinx-click emits the **live** synopsis, arguments, options, and env vars — the single
  source of truth for the mechanical contract. The hand-written `**Synopsis:**` line and
  option bullets currently duplicated in `index.rst` are **dropped** (prose that adds
  behavioural nuance beyond an option's one-liner is kept in the prose body).
- The `.. _cmd-<name>:` label is retained (reused as both the section id and the
  auto-linker target).
- `config` and `trash` are group sections: an H3 group intro, then each subcommand as an
  H4 (`config get`, `config set`, `trash list`, `trash empty`) with its own autodoc block +
  prose. Existing labels `.. _cmd-config:`, `.. _cmd-trash-list:`, `.. _cmd-trash-empty:`
  are kept; add `.. _cmd-config-get:` / `.. _cmd-config-set:` for the new H4 anchors.

### Single-command autodoc mechanism (spike required)

Rendering exactly one subcommand as its own block is done with sphinx-click's `:commands:`
option on the group directive:

```rst
.. click:: protonfs.cli:main
   :prog: protonfs
   :commands: push
   :nested: none
```

**Risk:** `:commands:` may still emit the parent group's own header/usage before the named
subcommand. Task 1 is a spike that renders one command and inspects the HTML:

- **If clean** (only the subcommand's synopsis/options render): use as designed.
- **If the group header leaks:** fall back to pointing the directive at the command object
  directly. Command objects are reachable as `protonfs.cli.main.commands["push"]`; if
  sphinx-click's `module:attr` import can't address a dict entry, expose thin module-level
  aliases in a dedicated `docs`-only shim (e.g. `protonfs.cli` already imports them) or add
  `:commands:` + a CSS rule that hides the redundant group `<h2>` within a command section.

The spike picks the concrete mechanism before any bulk rewrite; the rest of the plan is
mechanism-agnostic (it only relies on "a per-command autodoc block exists").

## Auto-linker extension

A local Sphinx extension, `docs/_ext/command_xref.py`, added to `sys.path` and `extensions`
in `conf.py`.

### Behaviour

- **Target map, derived live from the Click app** (no hardcoded command list): introspect
  `protonfs.cli.main` to enumerate command phrases → ref targets:
  - top-level command `push` → `cmd-push`
  - group subcommand `trash list` → `cmd-trash-list`, `config get` → `cmd-config-get`, etc.
  - The map is `{phrase: label}` where `label` is the existing `.. _cmd-*:` name.
  Deriving from the live app means new commands auto-participate; a phrase whose label does
  not exist is skipped (defensive).

- **Match pattern:** `protonfs`, then zero or more global flags/short options, then a known
  subcommand phrase (longest phrase wins, so `trash list` beats `trash`). Example matches:
  `protonfs push`, `protonfs --dry-run push`, `protonfs -vv pull`, `protonfs trash empty`.
  The matched span (the whole `protonfs … <sub>` phrase) becomes the link.

- **Scope — cross-page only:** the handler runs per document and **skips the reference page
  itself** (`docname == "reference/index"`), so the page defining the commands never
  self-links. Every *other* page (module autodoc like `protonfs.argv`, guarantees,
  getting-started, upgrading) gets its mentions linked.

- **Node safety — what gets linked vs skipped:**
  - **Skip** `literal_block`, `doctest_block`, and any `FixedTextElement` (the `::` shell
    example blocks) — a `protonfs push` inside a copy-paste example stays plain text.
  - **Skip** anything already inside a `reference`/`pending_xref` (don't double-link).
  - **Process inline `literal` nodes** (double-backtick ``` ``protonfs push`` ```): if the
    literal's full text matches a command phrase, wrap it in a cross-reference so it renders
    as a *linked* code span. This is the common prose form (`argv.py` docstrings write
    ``` ``protonfs --dry-run push`` ```) and is exactly what must link.
  - **Process ordinary `Text` nodes:** linkify matching substrings, splitting the text node
    into before / xref / after.

- **Link construction:** insert a `sphinx.addnodes.pending_xref`
  (`refdomain="std"`, `reftype="ref"`, `reftarget=<label>`, `refexplicit=True`) wrapping the
  matched text (kept as an inline `literal` when the source was a literal, plain text
  otherwise). Sphinx resolves it in the normal `doctree-resolved` phase, so a bad target
  fails the `-W` build rather than producing a dead link.

### Registration

`doctree-read` event handler (fires per parsed document, before resolution) so inserted
`pending_xref` nodes are resolved by Sphinx's standard machinery. `setup(app)` connects the
handler and returns the standard metadata dict (`parallel_read_safe`).

## Visual separation

Each command H3 already renders with furo spacing; add a light-touch rule to
`docs/_static/custom.css` keyed on the `cmd-` section id (the `.. _cmd-*:` label becomes the
section's HTML id):

```css
section[id^="cmd-"] {
  border-top: 1px solid var(--color-background-border);
  padding-top: 1.25rem;
  margin-top: 1.75rem;
}
```

A subtle top rule + spacing delimits each command; no per-command markup needed. (Verified
in the plan: the explicit label attached before a section title makes `cmd-<name>` the
section id in docutils/furo output.)

## Testing

- **Extension unit tests** (`tests/docs/test_command_xref.py`, pure Python — no Sphinx
  build):
  - target-map builder returns the expected `{phrase: label}` from the live Click app,
    including two-word group phrases and excluding unknown labels.
  - the linkify matcher: `protonfs --dry-run push` → matches, target `cmd-push`;
    `protonfs push` inside a simulated `literal_block` → not linked; inline `literal`
    ``protonfs pull`` → linked; longest-match (`trash list` not `trash`).
- **Docs build smoke** (existing CI `-W --keep-going` build) — the authoritative check that
  the merged page has no broken `:ref:`/`:doc:`/`:confval:`/`:envvar:` and that every
  inserted xref resolves. Run locally in the clean venv (per
  `[[protonfs-docs-ci-gotchas]]`: the system `sphinx-build` is the wrong version).
- **Doc-coverage gate** unchanged — still passes.
- **CLI-surface freeze test** unaffected (no CLI changes in this spec).

## Non-goals (YAGNI)

- No change to any CLI behaviour, command, option, or config key — docs only.
- No linking of a bare `protonfs` with no subcommand (too noisy; no unambiguous target).
- No linking on the reference page itself.
- No new runtime dependency — the extension is local `docs/_ext`, stdlib + Sphinx only.

## Migration / ordering notes

- Reuse existing `.. _cmd-*:` labels as the anchor + link targets; only *add* the two new
  `config get`/`config set` H4 labels.
- Move confval/envvar blocks verbatim; do not rename any target.
- Repoint `:doc:` links away from the deleted `commands`/`config` pages in the same change
  that deletes them, so no interim `-W` failure.
```
