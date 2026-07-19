# Contributing to protonfs

Thanks for your interest in protonfs. This guide covers how to set up a
development environment, the conventions the project follows, and what CI expects
before a change can merge.

By participating you agree to abide by the [Code of Conduct](CODE_OF_CONDUCT.md).

## Licensing

protonfs is released under the **PolyForm Noncommercial License 1.0.0** (see
[LICENSE](LICENSE)). By contributing, you agree that your contributions are
licensed under the same terms.

## Development setup

Requires Python >= 3.9.

```bash
git clone https://github.com/will-roscoe/protonfs
cd protonfs
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,docs]"
```

The test suite runs offline with fakes and needs no Proton account:

```bash
pytest -q
```

## Before you open a pull request

CI runs the checks below and **gates** on all of them. Run them locally first:

| Check | Command |
| --- | --- |
| Lint | `ruff check src tests` |
| Format | `ruff format --check src tests` |
| Tests + coverage floor (80%) | `pytest -q --cov=src/protonfs --cov-fail-under=80` |
| Docstring coverage floor (80%) | `interrogate -c pyproject.toml` |
| README ↔ docs in sync | `python .github/scripts/sync_readme.py --check` |
| Docs build | `sphinx-build -b html docs docs/_build/html` |

Notes:

- **Docstrings** use Sphinx reStructuredText field lists (`:param:`, `:returns:`,
  `:raises:`, `.. seealso::`, `.. note::`), not Google/NumPy style. New public
  functions/classes need one.
- **Version directives** — a new public module/class/function gets a
  `.. versionadded:: <next release>` in its docstring; a behavioral change to
  existing public API gets `.. versionchanged:: <version>`; API on a removal track
  gets `.. deprecated:: <version>`. Config keys and env vars carry the same
  directives in their `docs/reference/index.rst` Configuration section definition, and post-1.0 CLI
  feature changes are marked in `docs/reference/index.rst`. The baseline for API that
  predates the first stable release is `1.0.0`; date anything newer to its release.
- The **project overview** has a single source of truth in
  `docs/_shared/overview.rst`. Do not edit README's overview block by hand — edit
  the fragment and run `python .github/scripts/sync_readme.py --write`.
- Changes to the **CLI surface** (a command, option, exit code, config key, or
  environment variable) must update `docs/stability.rst` and the surface-freeze
  test (`tests/test_cli_surface.py`) in the same change — that surface is a frozen
  1.0 contract.

## Commit messages and versioning

protonfs auto-computes each release from [Conventional Commits](https://www.conventionalcommits.org/):

- `feat:` → minor bump
- `fix:` / `perf:` / `revert:` → patch bump
- a breaking change (`type!:` or a `BREAKING CHANGE:` footer) → major bump
- `chore` / `docs` / `style` / `refactor` / `build` / `ci` / `test` → no release

Use `type(scope): description`, e.g. `fix(drive): retry throttled downloads`.

### Release-override directives

A `+:<spec>` token **alone on its own line** of a commit message overrides the
classification, where `<spec>` is `major` | `minor` | `patch` | `pre` | `prepre`
| `rc`. The pre-release ladder (alpha → beta → rc) and the exact semantics are
documented in `docs/stability.rst`. Only maintainers cutting releases normally
use these.

## Reporting bugs and requesting features

Use the issue templates (they open automatically at
[New issue](https://github.com/will-roscoe/protonfs/issues/new/choose)). For a
bug, the more of the reproduction, `protonfs doctor` output, and versions you can
include, the faster it can be triaged.

## Security

Do **not** open a public issue for a security vulnerability. See
[SECURITY.md](SECURITY.md) for how to report privately.
