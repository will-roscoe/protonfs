# M5–M7 orchestration plan (issues #65–#74)

Subagent-driven, parallel where dependencies allow. Follows the wave pattern that
delivered the #59–#64 sweep: isolated worktrees, branch off latest `origin/main`
at dispatch time, TDD, full suite + ruff green, self-contained PR, orchestrator
reviews diff → merges sequentially → next wave bases on the result.

## Dependency graph

```
#65 support matrix ──┬──> #66 upgrade cmd ──> #68 upgrade docs
#67 migrations ──────┘        │
                              └──(#67 registry invoked by #66)
#65 + #67 ─────────────> #73 doctor advisor
#69 transfer backoff      (independent)
#70 trash subcommand      (independent)
#71 deinit                (independent)
#72 macOS CI              (independent)
#74 1.0 checklist         (after everything; manual, NOT a subagent)
```

Conflict surfaces to manage: #66/#70/#71 all touch `cli.py` + the surface-freeze
test + `stability.rst` → never in the same wave twice without sequential merges;
orchestrator merges one at a time and later branches rebase. #65/#67 touch
`install.py`/`config.py` respectively — disjoint.

## Waves

### Wave A — 4 agents in parallel
| Issue | Model | Notes |
|-------|-------|-------|
| #65 support matrix | sonnet | small, foundational; `install.py` + stability.rst table |
| #67 migration registry | sonnet | library-level: registry + layout marker + fixture repos; NO cli wiring yet (that's #66) — expose `pending_migrations(root)` / `apply_migrations(root, dry_run)` |
| #69 transfer backoff | sonnet | `drive.py` only; reuse `_is_throttle_error` |
| #72 macOS CI | haiku | ci.yml + badges; must check current macOS runner availability with `gh api` before writing |

Merge order A: #69 → #72 → #65 → #67 (drive.py first — no overlap; ci.yml next;
then the two foundations).

### Wave B — 3 agents in parallel (base: post-A main)
| Issue | Model | Notes |
|-------|-------|-------|
| #66 `protonfs upgrade` | sonnet | consumes #65 API + #67 registry; owns cli.py/freeze-test/stability.rst updates for `upgrade` |
| #70 `protonfs trash` | sonnet | independent; also edits restore's error message (#56 pointer) |
| #71 `protonfs deinit` | sonnet | independent |

All three touch `cli.py` + surface-freeze test + stability.rst → **merge
sequentially** (#66 → #70 → #71), instructing each PR to expect rebases; the
freeze test makes conflicts loud, which is the point.

### Wave C — 2 agents in parallel (base: post-B main)
| Issue | Model | Notes |
|-------|-------|-------|
| #68 upgrade docs | sonnet | docs/upgrading.rst + reference entry + cross-links |
| #73 doctor advisor | sonnet | consumes #65/#67 APIs; doctor.py + tests |

### Wave D — manual (orchestrator + user, NOT delegated)
#74: run the live release gate on a real account, migration dry-run on a
pre-0.14 fixture, docs sweep, then the **manual** `git tag v1.0.0`. Requires
user presence (real-account operations + the 1.0 go/no-go call).

## Standing instructions baked into every agent prompt
- `git fetch origin && git checkout -b <branch> origin/main` FIRST (worktree
  placeholder branch is stale).
- TDD; full `pytest -q` + `ruff check src tests` green; sphinx `-W` green when
  docs are touched.
- Conventional commit, `Closes #N`, exactly one `Helped-By:` trailer
  (model API id) — never Co-Authored-By.
- Self-contained PR body + `> Assisted by <model> <anthropic@willroscoe.uk>`
  footer; never merge; nothing under `.claude/` committed.
- Surface-changing PRs must update the freeze test AND stability.rst together.

## Orchestrator loop per wave
dispatch (Agent tool, worktree isolation) → on each completion: review diff
(`gh pr diff`, focused on the risk area named above) → CI watch → squash-merge
sequentially → prune worktree/branches → next wave bases on merged main.
Auto-release will version each merge; expect ~9 releases across A–C. exo2 is
NOT touched at any point (user instruction).
