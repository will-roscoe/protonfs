#!/usr/bin/env bash
# Regenerate status.svg from the data fragments and commit it (plus the caller's
# fragment) to main, robustly against concurrent status commits from other workflows.
#
# Usage:  update.sh <fragment-path> [<fragment-path> ...]
#   Each <fragment-path> is a data fragment the calling step has ALREADY written into
#   the workspace (e.g. .github/status/data/ci.json). status.svg is regenerated here.
#
# Why the reset-and-reapply loop: every status-updating workflow regenerates the single
# derived file status.svg, so a naive push races. Because status.svg is derived, we never
# merge it — on a push race we reset to the freshly-fetched origin/main (picking up other
# workflows' fragment commits), re-drop our own fragment, re-render from the union of all
# fragments, and retry. Fragments are per-source disjoint files, so this converges.
set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "usage: update.sh <fragment-path> [<fragment-path> ...]" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRANCH="${STATUS_BRANCH:-main}"

# Run from the repo root so fragment paths and `git add` are unambiguous.
cd "$(git rev-parse --show-toplevel)"

# Stash each caller-written fragment (paths are repo-root-relative, e.g.
# .github/status/data/ci.json) outside the tree so a hard reset can't lose it.
STASH_DIR="$(mktemp -d)"
trap 'rm -rf "$STASH_DIR"' EXIT
declare -a REL_PATHS=()
for rel in "$@"; do
  REL_PATHS+=("$rel")
  mkdir -p "$STASH_DIR/$(dirname "$rel")"
  cp "$rel" "$STASH_DIR/$rel"
done

pip install --quiet jinja2 >/dev/null 2>&1 || pip install jinja2

git config user.name "github-actions[bot]"
git config user.email "github-actions[bot]@users.noreply.github.com"

for attempt in 1 2 3 4 5; do
  # Reset to the freshly-fetched tip via FETCH_HEAD (reliable under a shallow/detached
  # CI checkout, where refs/remotes/origin/$BRANCH may not be maintained).
  git fetch origin "$BRANCH" --quiet
  git reset --hard FETCH_HEAD --quiet

  # Re-drop our fragment(s) on top of the freshly-fetched state.
  for rel in "${REL_PATHS[@]}"; do
    mkdir -p "$(dirname "$rel")"
    cp "$STASH_DIR/$rel" "$rel"
  done

  python "$ROOT/render.py"

  git add "${REL_PATHS[@]}" "$ROOT/status.svg"
  if git diff --cached --quiet; then
    echo "status: no change"
    exit 0
  fi

  git commit -m "chore: update status graphic [skip ci]" --quiet
  if git push origin "HEAD:$BRANCH" --quiet; then
    echo "status: pushed (attempt $attempt)"
    exit 0
  fi
  echo "status: push race, retrying ($attempt/5)"
  sleep "$(( (RANDOM % 5) + 1 ))"
done

echo "status: failed to push after 5 attempts" >&2
exit 1
