# Spec — Shell completions (`protonfs completions`)

Date: 2026-07-18
Status: approved (design). Implement first, before Spec C (command-reference restructure).
Scope: `src/protonfs/` (new command + commands module + upgrade hook), `tests/`, `docs/`.

## Decision

Add a `completions` command that drives **Click's native completion engine** (Click 8.4.2;
no third-party dependency, `click-completion` is dead for Click 8, `auto-click-auto` is
discontinued). Mirrors the existing `shell-init` command pattern.

## Command surface

```
protonfs completions {bash|zsh|fish}              # print the completion script to stdout
protonfs completions {bash|zsh|fish} --install    # install it, idempotently
protonfs completions {bash|zsh|fish} --uninstall  # remove it
```

- Script generation: `click.shell_completion.get_completion_class(shell)(cli, {}, "protonfs",
  "_PROTONFS_COMPLETE").source()` — in-process, no subprocess.
- **Install** writes the generated script to a file and references it (Click-recommended,
  avoids running protonfs on every new shell):
  - bash → `~/.local/share/protonfs/completion.bash`; add a marker-wrapped `source` line to `~/.bashrc`.
  - zsh → `~/.local/share/protonfs/completion.zsh`; marker-wrapped `source` line in `~/.zshrc`.
  - fish → `~/.config/fish/completions/protonfs.fish` (auto-loaded; no rc edit).
- **Idempotent** via a `# >>> protonfs completions >>>` / `# <<< protonfs completions <<<`
  marker block (same pattern as the repo's `.bash_local`/`cd_automation` work). Re-install
  rewrites in place; `--uninstall` removes the block + file.
- Errors cleanly on an unknown/unsupported shell.

## Lifecycle integration

- **`protonfs upgrade`**: if a completion script is installed (marker/file present for a
  shell), regenerate it in place so it tracks new commands. Idempotent; reported like other
  upgrade steps. Never installs completions that weren't already installed.
- **`--uninstall`**: the explicit teardown (removes marker block + generated file).
- **NOT** wired into `deinit` — deinit is contractually `.protonfs/`-only and completions are
  global user-shell files.

## Position-independent completion (timeboxed stretch)

`PositionalFlagGroup` reorders argv at parse time, but Click completes in canonical order, so
a global flag typed *after* a subcommand isn't offered by default. Timeboxed attempt: surface
the group's global options as completion candidates on each subcommand. If it requires
fighting Click's completion resolver, document the limitation instead. Command names and
per-subcommand options complete correctly regardless.

## Frozen surface & docs

- Additive → `feat:` (minor bump). Add `completions` to `tests/test_cli_surface.py` and
  `docs/stability.rst`; it auto-appears in the sphinx-click commands page. `.. versionadded::
  1.5.0` in the command help/narrative.
- Keep the strict `-W` docs build clean.

## Testing

- Each shell prints a non-empty, shell-appropriate script (bash/zsh/fish source markers).
- `--install` creates the script file and adds exactly one marker block to a temp rc
  (monkeypatched `$HOME`); re-running does not duplicate; `--uninstall` removes both.
- Unknown shell → clean usage error, non-zero exit.
- `upgrade` refresh: with an installed marker present, upgrade rewrites the script file;
  with none present, upgrade does nothing.

## Components

- `src/protonfs/commands/completions.py` — generation + install/uninstall/refresh logic
  (pure functions over an injectable `home`/paths for testability).
- `src/protonfs/cli.py` — the `completions` command wiring.
- upgrade hook in `src/protonfs/commands/upgrade.py` (or its migration list).
- `tests/test_completions.py`; surface test + docs updates.
