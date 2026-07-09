# src/protonfs/drive.py
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

BINARY_ENV_VAR = "PROTONFS_DRIVE_BIN"
DEFAULT_BINARY = "proton-drive"


class DriveError(RuntimeError):
    pass


class DriveAuthError(DriveError):
    pass


@dataclass
class TransferResult:
    transferred_items: int
    skipped_items: int
    failed_items: int
    failures: list[dict]

    @classmethod
    def from_json(cls, data: dict) -> TransferResult:
        return cls(
            transferred_items=data.get("transferredItems", 0),
            skipped_items=data.get("skippedItems", 0),
            failed_items=data.get("failedItems", 0),
            failures=data.get("failures", []),
        )


@dataclass
class RemoteEntry:
    rel_path: str
    is_dir: bool
    size: int


def binary_path() -> str:
    return os.environ.get(BINARY_ENV_VAR, DEFAULT_BINARY)


class DriveClient:
    def __init__(self, binary: str | None = None) -> None:
        self._binary = binary or binary_path()

    def _binary_available(self) -> bool:
        return shutil.which(self._binary) is not None or Path(self._binary).exists()

    def _invoke(self, args: list[str]) -> subprocess.CompletedProcess:
        if not self._binary_available():
            raise DriveError(f"proton-drive binary not found: {self._binary}")
        return subprocess.run([self._binary, *args, "--json"], capture_output=True, text=True)

    def _run_json(self, args: list[str]) -> dict | list:
        result = self._invoke(args)
        stdout = result.stdout.strip()
        try:
            parsed = json.loads(stdout) if stdout else {}
        except json.JSONDecodeError as exc:
            raise DriveError(f"unparseable output from proton-drive: {stdout!r}") from exc
        if result.returncode != 0:
            message = json.dumps(parsed) if parsed else result.stderr.strip()
            if "auth" in message.lower() or "unauthenticated" in message.lower():
                raise DriveAuthError(f"proton-drive auth required: {message}")
            raise DriveError(message)
        return parsed

    def _run_transfer(self, args: list[str]) -> TransferResult:
        result = self._invoke(args)
        stdout = result.stdout.strip()
        try:
            parsed = json.loads(stdout) if stdout else None
        except json.JSONDecodeError:
            parsed = None
        if not isinstance(parsed, dict) or "transferredItems" not in parsed:
            message = result.stderr.strip() or stdout
            if "auth" in message.lower() or "unauthenticated" in message.lower():
                raise DriveAuthError(f"proton-drive auth required: {message}")
            raise DriveError(message)
        return TransferResult.from_json(parsed)

    def version(self) -> str | None:
        if not self._binary_available():
            return None
        result = subprocess.run([self._binary, "version"], capture_output=True, text=True)
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    def is_authenticated(self) -> bool:
        try:
            self._run_json(["filesystem", "list", "/"])
        except DriveError:
            return False
        return True

    def list(self, remote_path: str) -> list[dict]:
        result = self._run_json(["filesystem", "list", remote_path])
        return result if isinstance(result, list) else []

    def walk(self, remote_root: str) -> list[RemoteEntry]:
        root = remote_root.rstrip("/")
        results: list[RemoteEntry] = []
        # queue of (absolute remote path, rel prefix)
        queue: list[tuple[str, str]] = [(root, "")]
        while queue:
            abs_path, prefix = queue.pop(0)
            for entry in self.list(abs_path):
                name = entry.get("name", {})
                if not name.get("ok"):
                    continue
                value = name["value"]
                rel = f"{prefix}{value}" if prefix else value
                child_abs = f"{abs_path}/{value}"
                if entry.get("type") == "folder":
                    results.append(RemoteEntry(rel_path=rel, is_dir=True, size=0))
                    queue.append((child_abs, f"{rel}/"))
                else:
                    results.append(
                        RemoteEntry(
                            rel_path=rel,
                            is_dir=False,
                            size=entry.get("totalStorageSize", 0),
                        )
                    )
        return results

    def upload(
        self,
        local_paths: list[Path],
        remote_parent: str,
        file_strategy: str | None = None,
        folder_strategy: str | None = None,
    ) -> TransferResult:
        args = ["filesystem", "upload"]
        if file_strategy:
            args += ["-f", file_strategy]
        if folder_strategy:
            args += ["-d", folder_strategy]
        args += [str(p) for p in local_paths] + [remote_parent]
        return self._run_transfer(args)

    def download(
        self,
        remote_paths: list[str],
        local_folder: Path,
        file_strategy: str | None = None,
        folder_strategy: str | None = None,
    ) -> TransferResult:
        args = ["filesystem", "download"]
        if file_strategy:
            args += ["-f", file_strategy]
        if folder_strategy:
            args += ["-d", folder_strategy]
        args += remote_paths + [str(local_folder)]
        return self._run_transfer(args)

    def trash(self, remote_paths: list[str]) -> list[dict]:
        result = self._run_json(["filesystem", "trash", *remote_paths])
        return result if isinstance(result, list) else []

    def restore(self, remote_paths: list[str]) -> list[dict]:
        result = self._run_json(["filesystem", "restore", *remote_paths])
        return result if isinstance(result, list) else []

    def delete(self, remote_paths: list[str]) -> list[dict]:
        result = self._run_json(["filesystem", "delete", *remote_paths])
        return result if isinstance(result, list) else []

    def create_folder(self, parent_path: str, name: str) -> dict:
        result = self._run_json(["filesystem", "create-folder", parent_path, name])
        return result if isinstance(result, dict) else {}
