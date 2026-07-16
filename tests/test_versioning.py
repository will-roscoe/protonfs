"""Tests for the auto-release version bump logic (`.github/scripts/compute_next_version.py`).

The script is not part of the shipped `protonfs` package (it lives under
`.github/scripts/`), so it is loaded by path via importlib rather than imported.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / ".github" / "scripts" / "compute_next_version.py"


def _load():
    spec = importlib.util.spec_from_file_location("compute_next_version", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load()
compute_next_version = mod.compute_next_version
classify_bump = mod.classify_bump


class TestClassifyBump:
    def test_feat_is_minor(self):
        assert classify_bump(["feat: add offload command"]) == "minor"

    def test_fix_is_patch(self):
        assert classify_bump(["fix(push): verify remote after upload"]) == "patch"

    def test_perf_is_patch(self):
        assert classify_bump(["perf: batch list calls"]) == "patch"

    def test_revert_is_patch(self):
        assert classify_bump(["revert: undo bad change"]) == "patch"

    def test_chore_docs_style_test_ci_do_not_release(self):
        msgs = ["chore: tidy", "docs: update", "ci: fix", "test: add", "style: fmt"]
        assert classify_bump(msgs) is None

    def test_refactor_and_build_do_not_release_by_default(self):
        # Follow semantic-release defaults: only feat/fix/perf/revert (+breaking) release.
        assert classify_bump(["refactor: rename", "build: bump dep"]) is None

    def test_bang_marks_breaking_major(self):
        assert classify_bump(["feat!: drop py38"]) == "major"

    def test_breaking_change_footer_is_major(self):
        msg = "feat: new manifest\n\nBREAKING CHANGE: index schema v2 required"
        assert classify_bump([msg]) == "major"

    def test_scope_with_bang_is_breaking(self):
        assert classify_bump(["refactor(api)!: change signature"]) == "major"

    def test_highest_bump_wins(self):
        assert classify_bump(["fix: a", "feat: b", "chore: c"]) == "minor"
        assert classify_bump(["feat: a", "fix!: b"]) == "major"

    def test_empty_and_noise(self):
        assert classify_bump([]) is None
        assert classify_bump(["Merge pull request #5 from x/y", "random subject"]) is None

    def test_case_insensitive_type(self):
        assert classify_bump(["Feat: something"]) == "minor"


class TestComputeNextVersion:
    def test_feat_bumps_minor(self):
        assert compute_next_version("0.3.0", ["feat: x"]) == "0.4.0"

    def test_fix_bumps_patch(self):
        assert compute_next_version("0.3.0", ["fix: x"]) == "0.3.1"

    def test_no_release_returns_none(self):
        assert compute_next_version("0.3.0", ["chore: x"]) is None

    def test_breaking_pre_1_0_bumps_minor_not_major(self):
        # Pre-1.0 (0.x) policy: a breaking change bumps MINOR, not MAJOR, to avoid
        # an accidental jump to 1.0.0 mid-development. Standard semantic-release 0.x mode.
        assert compute_next_version("0.3.0", ["feat!: x"]) == "0.4.0"

    def test_breaking_post_1_0_bumps_major(self):
        assert compute_next_version("1.4.2", ["feat!: x"]) == "2.0.0"

    def test_never_produces_major_bump_while_pre_1_0(self):
        # v1.0.0 is released manually; automation must never bump the major while at 0.x.
        for current in ("0.1.0", "0.3.0", "0.9.0", "0.99.5"):
            for msgs in (["feat!: x"], ["feat: x"], ["fix!: y"], ["feat!: a", "fix: b"]):
                nxt = compute_next_version(current, msgs)
                assert nxt is not None
                assert nxt.split(".")[0] == "0", f"{current} + {msgs} -> {nxt} left 0.x"

    def test_minor_post_1_0(self):
        assert compute_next_version("1.4.2", ["feat: x"]) == "1.5.0"

    def test_patch_resets_nothing(self):
        assert compute_next_version("1.4.2", ["fix: x"]) == "1.4.3"

    def test_accepts_v_prefix_and_strips_it(self):
        assert compute_next_version("v0.3.0", ["feat: x"]) == "0.4.0"

    def test_no_prior_tag_uses_zero(self):
        assert compute_next_version(None, ["feat: x"]) == "0.1.0"
        assert compute_next_version("", ["fix: x"]) == "0.0.1"

    def test_invalid_current_raises(self):
        with pytest.raises(ValueError):
            compute_next_version("not-a-version", ["feat: x"])


class TestHeaderFirstLineOnly:
    """The Conventional Commit type header is the FIRST line only: squash-merge
    bodies carry whole PR descriptions, where a quoted `feat:`/`refactor!:` at the
    start of a body line must not (mis)classify the commit."""

    def test_body_line_type_does_not_classify(self):
        msg = "docs: update guide\n\nfeat: quoted from the PR body, not a header"
        assert classify_bump([msg]) is None

    def test_body_line_bang_is_not_breaking(self):
        msg = "chore: tidy\n\nrefactor!: quoted example of a breaking header"
        assert classify_bump([msg]) is None

    def test_breaking_footer_still_detected_mid_message(self):
        msg = "feat: new thing\n\nlong body\n\nBREAKING CHANGE: contract changed"
        assert classify_bump([msg]) == "major"


class TestDirectives:
    """`+:<spec>` override directives and the SemVer 2.0.0 pre-release ladder."""

    # -- overrides beat classification ------------------------------------------

    def test_directive_overrides_conventional_classification(self):
        assert compute_next_version("1.2.3", ["feat: x\n\n+:patch"]) == "1.2.4"
        assert compute_next_version("1.2.3", ["chore: x\n\n+:major"]) == "2.0.0"

    def test_directive_not_subject_to_pre_1_0_demotion(self):
        assert compute_next_version("0.24.0", ["chore: x\n\n+:major"]) == "1.0.0"

    def test_highest_impact_directive_wins_across_batch(self):
        msgs = ["fix: a\n\n+:prepre", "chore: b\n\n+:minor"]
        assert compute_next_version("1.0.0", msgs) == "1.1.0"

    # -- entering a pre-release from a final version -----------------------------

    def test_pre_from_final_rebases_onto_next_minor(self):
        assert compute_next_version("1.0.0", ["chore: x\n\n+:pre"]) == "1.1.0-alpha"

    def test_prepre_from_final_starts_numbered_alpha(self):
        assert compute_next_version("1.0.0", ["chore: x\n\n+:prepre"]) == "1.1.0-alpha.0"

    def test_rc_from_final_jumps_to_rc(self):
        assert compute_next_version("1.0.0", ["chore: x\n\n+:rc"]) == "1.1.0-rc"

    # -- the ladder within a pre-release -----------------------------------------

    def test_prepre_increments_bare_channel(self):
        assert compute_next_version("1.1.0-alpha", ["chore: x\n\n+:prepre"]) == "1.1.0-alpha.1"

    def test_prepre_increments_numbered_channel(self):
        assert compute_next_version("1.1.0-alpha.1", ["chore: x\n\n+:prepre"]) == "1.1.0-alpha.2"

    def test_pre_advances_channel(self):
        assert compute_next_version("1.1.0-alpha", ["chore: x\n\n+:pre"]) == "1.1.0-beta"
        assert compute_next_version("1.1.0-beta.2", ["chore: x\n\n+:pre"]) == "1.1.0-rc"

    def test_pre_past_rc_finalizes(self):
        assert compute_next_version("1.1.0-rc", ["chore: x\n\n+:pre"]) == "1.1.0"
        assert compute_next_version("1.1.0-rc.1", ["chore: x\n\n+:pre"]) == "1.1.0"

    def test_rc_jumps_earlier_channels_and_increments_itself(self):
        assert compute_next_version("1.1.0-alpha.3", ["chore: x\n\n+:rc"]) == "1.1.0-rc"
        assert compute_next_version("1.1.0-rc", ["chore: x\n\n+:rc"]) == "1.1.0-rc.1"
        assert compute_next_version("1.1.0-rc.1", ["chore: x\n\n+:rc"]) == "1.1.0-rc.2"

    def test_minor_and_patch_finalize_a_prerelease(self):
        assert compute_next_version("1.1.0-beta", ["chore: x\n\n+:minor"]) == "1.1.0"
        assert compute_next_version("1.1.0-alpha.2", ["chore: x\n\n+:patch"]) == "1.1.0"

    def test_major_from_prerelease(self):
        # Base already an X.0.0 -> finalize it; otherwise bump to the next major.
        assert compute_next_version("2.0.0-rc", ["chore: x\n\n+:major"]) == "2.0.0"
        assert compute_next_version("1.1.0-rc", ["chore: x\n\n+:major"]) == "2.0.0"

    # -- plain commits during a pre-release ---------------------------------------

    def test_conventional_commits_advance_prerelease_number_only(self):
        assert compute_next_version("1.1.0-alpha", ["feat: x"]) == "1.1.0-alpha.1"
        assert compute_next_version("1.1.0-rc.1", ["fix: x"]) == "1.1.0-rc.2"

    def test_breaking_during_prerelease_does_not_move_base(self):
        assert compute_next_version("1.1.0-beta", ["feat!: x"]) == "1.1.0-beta.1"

    def test_non_release_commits_still_no_release_during_prerelease(self):
        assert compute_next_version("1.1.0-alpha", ["docs: x", "chore: y"]) is None

    # -- parsing ------------------------------------------------------------------

    def test_prerelease_current_version_parses_with_v_prefix(self):
        assert compute_next_version("v1.1.0-alpha.1", ["chore: x\n\n+:pre"]) == "1.1.0-beta"

    def test_directive_requires_word_boundary(self):
        # `+:premature` is not a directive... but its `pre` prefix must not match either.
        assert compute_next_version("1.0.0", ["chore: x +:premature"]) is None
