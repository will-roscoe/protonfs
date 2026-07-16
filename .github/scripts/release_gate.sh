#!/usr/bin/env bash
# .github/scripts/release_gate.sh — the full pre-release gate (issue #11).
#
# CI already gates every auto-created version tag on the test matrix + coverage
# floor (auto-release.yml calls ci.yml before tagging). What CI cannot run is the
# LIVE suite: it needs an authenticated proton-drive session and a throwaway
# remote dir, which never belong in CI. This script is the manual complement —
# run it before any milestone/manual tag (and periodically before merging
# release-worthy work):
#
#   PROTONFS_TEST_REMOTE=/my-files/test .github/scripts/release_gate.sh
#
# It fails fast on the first broken gate.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

COV_FLOOR=80

echo "== gate 1/3: lint (strict) =="
ruff check src tests

echo "== gate 2/3: full suite + coverage floor (${COV_FLOOR}%) =="
pytest -q --cov=src/protonfs --cov-report=term --cov-fail-under="${COV_FLOOR}"

echo "== gate 3/3: live suite against a throwaway remote =="
if [ -z "${PROTONFS_TEST_REMOTE:-}" ]; then
    echo "PROTONFS_TEST_REMOTE is not set." >&2
    echo "Set it to a DISPOSABLE Drive dir (e.g. /my-files/test) and re-run:" >&2
    echo "  PROTONFS_TEST_REMOTE=/my-files/test $0" >&2
    exit 1
fi
# -p no:cacheprovider keeps repeated gate runs from reordering by cached failures.
pytest tests/test_live_integration.py -v -p no:cacheprovider

echo "== release gate PASSED =="
