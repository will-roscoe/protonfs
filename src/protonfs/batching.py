from __future__ import annotations

from pathlib import Path

DEFAULT_BATCH_SIZE = 200


def group_by_parent(rel_paths: list[str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for rel in rel_paths:
        parent = str(Path(rel).parent)
        groups.setdefault(parent, []).append(rel)
    return groups


def batches(items: list, size: int = DEFAULT_BATCH_SIZE) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]
