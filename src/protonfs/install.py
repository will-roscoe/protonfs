# src/protonfs/install.py
"""Self-diagnosing installer for the `proton-drive` prebuilt binary (Tier 3).

`pip install protonfs` gives you the Python package; `protonfs install-drive`
fetches and verifies the official `proton-drive` CLI binary, and `protonfs auth
login` (a thin passthrough) authenticates it. The installer detects the
platform, hard-gates on AVX2 for the linux-x64 Bun-compiled prebuilt, downloads
over HTTPS and verifies the pinned SHA-512 before ever marking the binary
executable — it never installs an unverified binary.

Design notes / accepted deviations from the roadmap decision text:
- The decision described a bash installer checking curl/unzip. This Python
  implementation downloads via urllib and verifies via hashlib, so those external
  tools are not prerequisites; the decision's intent (self-diagnosing,
  resolve-what-it-can, precise instructive errors) is preserved and the installer
  is unit-testable.
- The no-AVX2 path emits precise build-from-source instructions rather than
  automating a Bun-baseline source build. That path is defensive only (no current
  target machine lacks AVX2), so automating it is deferred as YAGNI.
"""
from __future__ import annotations

import hashlib
import os
import platform as _platform
import shutil
import stat
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_VERSION = "0.4.6"
VERSION_ENV = "PROTONFS_DRIVE_VERSION"
SHA512_ENV = "PROTONFS_DRIVE_SHA512"
DOWNLOAD_BASE = "https://proton.me/download/drive/cli"
DOWNLOAD_TIMEOUT = 60  # seconds; avoids a stalled connection hanging the installer

# Pinned SHA-512 of the official prebuilt, keyed by (version, slug). linux-x64 is
# verified against the released 0.4.6 binary. darwin checksums are added when a
# maintainer pins them from the official downloads; until then those platforms
# require an explicit PROTONFS_DRIVE_SHA512 override (we never install unverified).
PINNED_SHA512 = {
    ("0.4.6", "linux-x64"): (
        "d187409932742e6fdc6aae2995998f4c89ea51999283395bc8d0bdc5343a79d3"
        "1bf5a485d5af9adf3b7909fc92f2d2ef0b133edc4939d5faf1d096eb744425bb"
    ),
}

# glibc below this is too old for the Bun-compiled linux-x64 prebuilt. Bun supports
# glibc >= 2.17 (per the roadmap's target-machine survey: exo2 on CentOS 7 / glibc
# 2.17 is a confirmed headless-installable target), so we only warn below that.
MIN_GLIBC = (2, 17)


class InstallError(RuntimeError):
    """Raised with a precise, instructive message when install cannot proceed."""


@dataclass
class Platform:
    slug: str  # e.g. "linux-x64"
    os_name: str  # "linux" | "darwin"
    arch: str  # "x64" | "arm64"


@dataclass
class InstallResult:
    path: Path
    on_path: bool
    sha512: str
    warnings: list[str] = field(default_factory=list)


def detect_platform(system: str | None = None, machine: str | None = None) -> Platform:
    system = (system or _platform.system()).lower()
    machine = (machine or _platform.machine()).lower()
    if machine in ("x86_64", "amd64"):
        arch = "x64"
    elif machine in ("arm64", "aarch64"):
        arch = "arm64"
    else:
        raise InstallError(
            f"unsupported CPU architecture '{machine}'. The proton-drive prebuilt is "
            f"published for x86_64 and arm64 only."
        )
    if system == "linux":
        if arch != "x64":
            raise InstallError(
                f"no official proton-drive prebuilt for linux-{arch}; only linux-x64 is "
                f"published. Build from source or run on an x86_64 host."
            )
        slug = "linux-x64"
        os_name = "linux"
    elif system == "darwin":
        slug = f"darwin-{arch}"
        os_name = "darwin"
    else:
        raise InstallError(
            f"unsupported OS '{system}'. proton-drive prebuilts exist for linux and macOS."
        )
    return Platform(slug=slug, os_name=os_name, arch=arch)


def resolve_version(version: str | None = None) -> str:
    return version or os.environ.get(VERSION_ENV) or DEFAULT_VERSION


def binary_url(version: str, slug: str) -> str:
    return f"{DOWNLOAD_BASE}/{version}/{slug}/proton-drive"


def pinned_sha512(version: str, slug: str) -> str | None:
    """The expected SHA-512, from the env override first, then the pinned table."""
    override = os.environ.get(SHA512_ENV)
    if override:
        return override.strip().lower()
    return PINNED_SHA512.get((version, slug))


def has_avx2(cpuinfo_text: str | None = None) -> bool:
    """Whether the CPU advertises AVX2 (read from /proc/cpuinfo on linux)."""
    if cpuinfo_text is None:
        try:
            cpuinfo_text = Path("/proc/cpuinfo").read_text()
        except OSError:
            return False
    for line in cpuinfo_text.splitlines():
        if line.startswith("flags") and "avx2" in line.split():
            return True
    return False


def _glibc_version(raw: str | None = None) -> tuple[int, int] | None:
    """Parse the running glibc version, e.g. 'glibc 2.35' -> (2, 35). None if unknown."""
    if raw is None:
        libc, _ = _platform.libc_ver()
        raw = _platform.libc_ver()[1] if libc == "glibc" else ""
    if not raw:
        return None
    try:
        major, minor = (int(x) for x in raw.split(".")[:2])
    except (ValueError, IndexError):
        return None
    return (major, minor)


def diagnose(plat: Platform, cpuinfo_text: str | None = None,
             glibc_raw: str | None = None) -> list[str]:
    """Return a list of warning strings for soft issues (empty == all clear).

    Hard blockers (missing AVX2, unverifiable checksum) are raised by
    install_drive; diagnose covers advisory concerns like an old glibc.
    """
    warnings: list[str] = []
    if plat.os_name == "linux":
        glibc = _glibc_version(glibc_raw)
        if glibc is not None and glibc < MIN_GLIBC:
            warnings.append(
                f"glibc {glibc[0]}.{glibc[1]} detected; the linux-x64 prebuilt targets "
                f">= {MIN_GLIBC[0]}.{MIN_GLIBC[1]} and may fail to start on this host."
            )
    return warnings


def _no_avx2_message() -> str:
    have_bun = shutil.which("bun") is not None
    have_git = shutil.which("git") is not None
    steps = (
        "This CPU lacks AVX2, which the official linux-x64 prebuilt requires. "
        "Build a Bun-baseline binary from source instead:"
    )
    prereqs = []
    if not have_bun:
        prereqs.append("install Bun (https://bun.sh)")
    if not have_git:
        prereqs.append("install git")
    if prereqs:
        return (
            f"{steps} first {', and '.join(prereqs)}, then clone "
            f"github.com/ProtonDriveApps/sdk and build the CLI with a baseline target, "
            f"and point PROTONFS_DRIVE_BIN at the result."
        )
    return (
        f"{steps} clone github.com/ProtonDriveApps/sdk, build the CLI with "
        f"`bun build --compile --target=bun-linux-x64-baseline`, and point "
        f"PROTONFS_DRIVE_BIN at the result."
    )


def resolve_install_dir(path_env: str | None = None) -> tuple[Path, bool]:
    """Return (install_dir, on_path). Prefer ~/.local/bin when it is on PATH; else a
    managed dir the user surfaces via PROTONFS_DRIVE_BIN."""
    local_bin = Path.home() / ".local" / "bin"
    path_value = os.environ.get("PATH", "") if path_env is None else path_env
    on_path = str(local_bin) in path_value.split(os.pathsep)
    if on_path:
        return local_bin, True
    managed = Path.home() / ".local" / "share" / "protonfs" / "bin"
    return managed, False


def _default_opener(url: str):
    return urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT)


def download_and_verify(url: str, expected_sha512: str, dest: Path, opener=None) -> str:
    """Download `url`, verify its SHA-512 equals `expected_sha512`, and write it to
    `dest` (only after verification). Returns the verified digest. Raises InstallError
    on any network/HTTP error or checksum mismatch, always leaving no partial file
    behind."""
    opener = opener or _default_opener
    hasher = hashlib.sha512()
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    try:
        with opener(url) as resp, open(tmp, "wb") as out:
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                hasher.update(chunk)
                out.write(chunk)
    except OSError as exc:
        # urllib.error.URLError/HTTPError subclass OSError, as do socket timeouts.
        tmp.unlink(missing_ok=True)
        raise InstallError(
            f"failed to download {url}: {exc}. Check your connection, or verify "
            f"{VERSION_ENV} points at a real release."
        ) from exc
    digest = hasher.hexdigest()
    if digest.lower() != expected_sha512.lower():
        tmp.unlink(missing_ok=True)
        raise InstallError(
            f"SHA-512 mismatch for {url}: expected {expected_sha512}, got {digest}. "
            f"Refusing to install an unverified binary."
        )
    tmp.replace(dest)
    return digest


def install_drive(
    version: str | None = None,
    plat: Platform | None = None,
    dest_dir: Path | None = None,
    cpuinfo_text: str | None = None,
    downloader=None,
) -> InstallResult:
    """Detect, diagnose, download+verify and install the proton-drive binary."""
    version = resolve_version(version)
    plat = plat or detect_platform()

    if plat.os_name == "linux" and not has_avx2(cpuinfo_text):
        raise InstallError(_no_avx2_message())

    expected = pinned_sha512(version, plat.slug)
    if expected is None:
        raise InstallError(
            f"no pinned SHA-512 for proton-drive {version} on {plat.slug}. Set "
            f"{SHA512_ENV} to the official checksum to install, or install manually. "
            f"Refusing to install an unverified binary."
        )

    if dest_dir is None:
        dest_dir, on_path = resolve_install_dir()
    else:
        on_path = str(dest_dir) in os.environ.get("PATH", "").split(os.pathsep)

    warnings = diagnose(plat, cpuinfo_text)
    override = os.environ.get(SHA512_ENV)
    base_pin = PINNED_SHA512.get((version, plat.slug))
    if override and base_pin and override.strip().lower() != base_pin.lower():
        warnings.append(
            f"{SHA512_ENV} overrides the pinned checksum for {plat.slug} {version}; "
            f"installing against the override, not the audited pin."
        )
    url = binary_url(version, plat.slug)
    dest = dest_dir / "proton-drive"
    digest = download_and_verify(url, expected, dest, opener=downloader)

    mode = dest.stat().st_mode
    dest.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    if not on_path:
        warnings.append(
            f"{dest_dir} is not on PATH; export PROTONFS_DRIVE_BIN={dest} (or add the "
            f"directory to PATH) so protonfs can find the binary."
        )
    return InstallResult(path=dest, on_path=on_path, sha512=digest, warnings=warnings)
