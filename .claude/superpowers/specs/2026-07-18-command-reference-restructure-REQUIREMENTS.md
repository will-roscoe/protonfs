# Spec C — Command Reference restructure (REQUIREMENTS CAPTURE, pre-brainstorm)

Date: 2026-07-18
Status: requirements captured; needs its own brainstorm before implementation.
Origin: user message 2026-07-18. Reworks the docs structure built in Spec A (PR #107).

## Problems the user raised

1. **Weak visual separation between commands** in the command reference — hard to see
   where one command's entry ends and the next begins (sphinx-click output runs together).
2. **Missing backreferences that should be automatic.** Prose that mentions a command
   invocation should link to that command's reference entry. Example: `protonfs.argv`
   module docs mention `protonfs --dry-run push`; that should link to the `protonfs push`
   entry. Wanted **across all docs, ideally automatically** (an auto-linker, not hand roles).
3. **Subcommand reference section needs formatting love** — the title and usage sections.
4. **Prose + examples feel lost.** The rich hand-written per-command prose/examples should be
   present in the (restructured) command reference. Pull back useful prose/examples from
   PRIOR versions in git history wherever they exist (they currently live in
   `docs/reference/index.rst`, separate from the autodoc `commands.rst`).

## Desired structure (user-specified)

```
# Command Reference
  ## Configuration
    ### Environment Variables
      #### <one per environment variable>
    ### Configuration file and keys
      #### <one per config key>
  ## Subcommands
    ### <one per subcommand>   (each: title + usage + prose + examples, clearly separated)
```

- The **top level** ("Command Reference") should carry its own **`protonfs`-level usage**
  info, with examples (the root program synopsis + top-level examples).

## Open design questions (for the brainstorm)

- **Auto-backlinking mechanism**: custom Sphinx transform/role that regex-matches
  `protonfs <subcommand>` in any docstring/RST and links it to the command entry? vs a
  `default_role`/interpreted-text convention? vs sphinx extension. Must not create bad
  links inside literal shell examples.
- **Autodoc vs hand-prose reconciliation**: keep sphinx-click for the mechanical
  synopsis/options but interleave the hand-written prose+examples per command in the same
  section (sphinx-click supports before/after content injection?), vs abandon sphinx-click
  for a hand-structured page, vs a hybrid where each `### <subcommand>` embeds the autodoc.
- **Merge the 3 pages** (`commands.rst` autodoc + `config.rst` + `index.rst` narrative) into
  the one hierarchy above, or keep autodoc as an appendix.
- **Visual separation**: CSS (furo) horizontal rules / card styling per command vs RST
  structure (rubric/section per command).
- Keep the strict `-W` build clean and all existing cross-ref targets working.

## Constraint

Must preserve the Spec A/B gains: confval/envvar targets, version directives, `:option:`
cross-refs, and the doc-coverage + strict-build CI gates.
