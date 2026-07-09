from __future__ import annotations

from protonfs.batching import batches, group_by_parent


def test_group_by_parent_groups_same_directory_files() -> None:
    groups = group_by_parent(["sim/03pol012/a", "sim/03pol012/b", "sim/03pol013/c"])
    assert groups == {
        "sim/03pol012": ["sim/03pol012/a", "sim/03pol012/b"],
        "sim/03pol013": ["sim/03pol013/c"],
    }


def test_group_by_parent_handles_root_level_files() -> None:
    groups = group_by_parent(["file.txt"])
    assert groups == {".": ["file.txt"]}


def test_batches_splits_into_exact_chunks() -> None:
    result = batches([1, 2, 3, 4], size=2)
    assert result == [[1, 2], [3, 4]]


def test_batches_handles_remainder() -> None:
    result = batches([1, 2, 3], size=2)
    assert result == [[1, 2], [3]]


def test_batches_empty_input() -> None:
    assert batches([], size=2) == []
