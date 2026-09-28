<!--
Thanks for contributing to protonfs! A few notes before you open this PR:
- Use a Conventional Commit style title, e.g. `fix(drive): retry throttled downloads`.
  It drives the automatic version bump (feat → minor, fix → patch, type! → major).
- See CONTRIBUTING.md for the full checklist and local commands.
-->

## What does this change?

<!-- A clear description of the change and the motivation for it. -->

## Related issues

<!-- e.g. Closes #123. Spell out the effect in plain terms — do not rely on
     labels or plans a reader without this repo cannot see. -->

## Checklist

- [ ] Title follows Conventional Commits (`type(scope): description`).
- [ ] `ruff check src tests` and `ruff format --check src tests` pass.
- [ ] `pytest -q` passes (with the 80% coverage floor).
- [ ] `interrogate -c pyproject.toml` passes (docstring floor); new public API has
      RST docstrings.
- [ ] If the project overview changed: edited `docs/_shared/overview.rst` and ran
      `python .github/scripts/sync_readme.py --write` (not README directly).
- [ ] If the CLI surface changed (command/option/exit code/config key/env var):
      updated `docs/stability.rst` **and** `tests/test_cli_surface.py`.
- [ ] Docs build cleanly (`sphinx-build -b html docs docs/_build/html`).

## Notes for reviewers

<!-- Anything that helps review: trade-offs, follow-ups, areas of uncertainty. -->
