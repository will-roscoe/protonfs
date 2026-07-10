# tests/test_install.py
from __future__ import annotations

import hashlib
import stat
from pathlib import Path

import pytest

from protonfs.install import (
    InstallError,
    Platform,
    binary_url,
    detect_platform,
    diagnose,
    download_and_verify,
    has_avx2,
    install_drive,
    pinned_sha512,
    resolve_install_dir,
)


class _FakeResp:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def read(self, n: int = -1) -> bytes:
        if self._pos >= len(self._data):
            return b""
        end = len(self._data) if n is None or n < 0 else self._pos + n
        chunk = self._data[self._pos : end]
        self._pos += len(chunk)
        return chunk

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *a) -> bool:
        return False


def _opener_for(data: bytes):
    def _open(url: str) -> _FakeResp:
        return _FakeResp(data)

    return _open


# --- platform detection ---------------------------------------------------

def test_detect_platform_linux_x64() -> None:
    p = detect_platform(system="Linux", machine="x86_64")
    assert p == Platform(slug="linux-x64", os_name="linux", arch="x64")


def test_detect_platform_darwin_arm64() -> None:
    p = detect_platform(system="Darwin", machine="arm64")
    assert p.slug == "darwin-arm64"


def test_detect_platform_windows_raises() -> None:
    with pytest.raises(InstallError, match="unsupported OS"):
        detect_platform(system="Windows", machine="AMD64")


def test_detect_platform_linux_arm_raises() -> None:
    with pytest.raises(InstallError, match="linux-arm64"):
        detect_platform(system="Linux", machine="aarch64")


# --- url / checksum -------------------------------------------------------

def test_binary_url() -> None:
    assert binary_url("0.4.6", "linux-x64") == (
        "https://proton.me/download/drive/cli/0.4.6/linux-x64/proton-drive"
    )


def test_pinned_sha512_linux_x64_is_pinned() -> None:
    sha = pinned_sha512("0.4.6", "linux-x64")
    assert sha is not None and len(sha) == 128


def test_pinned_sha512_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROTONFS_DRIVE_SHA512", "ABC123")
    assert pinned_sha512("0.4.6", "darwin-x64") == "abc123"


def test_pinned_sha512_unpinned_platform_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROTONFS_DRIVE_SHA512", raising=False)
    assert pinned_sha512("0.4.6", "darwin-arm64") is None


# --- diagnosis ------------------------------------------------------------

def test_has_avx2_true() -> None:
    assert has_avx2("flags\t: fpu vme avx2 sse\nmodel\t: 1\n") is True


def test_has_avx2_false() -> None:
    assert has_avx2("flags\t: fpu vme sse4_2\n") is False


def test_diagnose_warns_on_old_glibc() -> None:
    plat = Platform("linux-x64", "linux", "x64")
    warnings = diagnose(plat, cpuinfo_text="flags : avx2", glibc_raw="2.17")
    assert any("glibc" in w for w in warnings)


def test_diagnose_clean_on_modern_glibc() -> None:
    plat = Platform("linux-x64", "linux", "x64")
    assert diagnose(plat, cpuinfo_text="flags : avx2", glibc_raw="2.35") == []


# --- download + verify ----------------------------------------------------

def test_download_and_verify_ok(tmp_path: Path) -> None:
    data = b"binary-bytes"
    sha = hashlib.sha512(data).hexdigest()
    dest = tmp_path / "proton-drive"
    digest = download_and_verify("http://x/pd", sha, dest, opener=_opener_for(data))
    assert digest == sha
    assert dest.read_bytes() == data


def test_download_and_verify_mismatch_raises_and_leaves_no_file(tmp_path: Path) -> None:
    data = b"tampered"
    wrong = hashlib.sha512(b"expected").hexdigest()
    dest = tmp_path / "proton-drive"
    with pytest.raises(InstallError, match="SHA-512 mismatch"):
        download_and_verify("http://x/pd", wrong, dest, opener=_opener_for(data))
    assert not dest.exists()
    assert not (tmp_path / "proton-drive.part").exists()


# --- install orchestration ------------------------------------------------

def test_install_drive_no_avx2_raises_instructive() -> None:
    plat = Platform("linux-x64", "linux", "x64")
    with pytest.raises(InstallError, match="AVX2"):
        install_drive(plat=plat, cpuinfo_text="flags : sse4_2")


def test_install_drive_unpinned_platform_without_override_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PROTONFS_DRIVE_SHA512", raising=False)
    plat = Platform("darwin-arm64", "darwin", "arm64")
    with pytest.raises(InstallError, match="no pinned SHA-512"):
        install_drive(plat=plat)


def test_install_drive_happy_path_installs_and_sets_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = b"the-real-binary"
    sha = hashlib.sha512(data).hexdigest()
    monkeypatch.setenv("PROTONFS_DRIVE_SHA512", sha)  # override the pin for test data
    plat = Platform("linux-x64", "linux", "x64")

    result = install_drive(
        plat=plat,
        dest_dir=tmp_path,
        cpuinfo_text="flags : avx2",
        downloader=_opener_for(data),
    )

    assert result.path == tmp_path / "proton-drive"
    assert result.path.read_bytes() == data
    assert result.sha512 == sha
    assert result.path.stat().st_mode & stat.S_IXUSR  # executable
    # tmp_path is not on PATH -> a PROTONFS_DRIVE_BIN hint is surfaced
    assert any("PROTONFS_DRIVE_BIN" in w for w in result.warnings)


def test_resolve_install_dir_prefers_local_bin_when_on_path() -> None:
    local_bin = str(Path.home() / ".local" / "bin")
    path_dir, on_path = resolve_install_dir(path_env=f"/usr/bin:{local_bin}")
    assert on_path is True
    assert path_dir == Path.home() / ".local" / "bin"


def test_resolve_install_dir_falls_back_to_managed_when_off_path() -> None:
    path_dir, on_path = resolve_install_dir(path_env="/usr/bin:/bin")
    assert on_path is False
    assert path_dir == Path.home() / ".local" / "share" / "protonfs" / "bin"
