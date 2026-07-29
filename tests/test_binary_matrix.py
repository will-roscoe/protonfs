# tests/test_binary_matrix.py
"""Local-only test matrix across every supported proton-drive **binary version**.

The rest of the suite drives a *fake* proton-drive, which is fast and hermetic but
structurally blind to anything that differs between real binary versions -- an older
GnuPG the keygen must cope with, a CLI that lacks a flag protonfs relies on, a changed
``--json`` shape, a broken pin. This matrix downloads and runs each pinned version to
catch exactly that class of regression (the kind exo2 surfaced: GnuPG 2.0 vs the pass
keygen, and the pass store needing proton-drive >= 0.6.0).

It NEVER runs in CI -- it is gated on ``PROTONFS_TEST_MATRIX`` (and each row only spans
the versions this release ships a pin for on the current platform). Two tiers:

**Offline tier** (needs network to download the binaries once, ~100 MB each)::

    PROTONFS_TEST_MATRIX=1 pytest tests/test_binary_matrix.py -v

**Auth tier** (real session round-trips per binary) additionally needs a throwaway
remote and a usable session on this host (log in first with the highest version)::

    PROTONFS_TEST_MATRIX=1 PROTONFS_TEST_REMOTE=/my-files/test \
        pytest tests/test_binary_matrix.py -v

Running the binaries requires this host to be able to reach a keyring/session store
(``proton-drive version`` loads the session before it answers) -- i.e. a real dev box
or a host where ``protonfs doctor --fix`` succeeds, which is why this is local-only.
"""
from __future__ import annotations

import os

import pytest

from protonfs import install
from protonfs.commands.doctor import pass_store_drive_compat_check
from protonfs.commands.upgrade import _semver_tuple
from protonfs.credstore import PASS_STORE_MIN_DRIVE
from protonfs.drive import DriveClient

MATRIX = os.environ.get("PROTONFS_TEST_MATRIX")
REMOTE = os.environ.get("PROTONFS_TEST_REMOTE")

pytestmark = pytest.mark.skipif(
    not MATRIX, reason="set PROTONFS_TEST_MATRIX=1 to run the per-binary matrix"
)


def _installable_versions() -> list[str]:
    """Supported proton-drive versions this release pins for the current platform.

    A version without a pin for this OS/arch (e.g. 0.4.6, published for linux-x64 only)
    is skipped rather than failed -- the matrix spans what this host can actually verify.
    """
    if not MATRIX:
        return []
    slug = install.detect_platform().slug
    return [v for v in install.SUPPORTED_DRIVE_VERSIONS if install.pinned_sha512(v, slug)]


VERSIONS = _installable_versions()


@pytest.fixture(scope="session")
def binaries(tmp_path_factory) -> dict[str, install.InstallResult]:
    """Download+verify each supported version once, into a per-session cache.

    Each install goes through :func:`install.install_drive`, so the pinned SHA-512 is
    checked before the binary is ever marked executable -- a bad pin fails here.
    """
    root = tmp_path_factory.mktemp("proton-drive-matrix")
    out: dict[str, install.InstallResult] = {}
    for version in VERSIONS:
        out[version] = install.install_drive(version=version, dest_dir=root / version)
    return out


@pytest.mark.parametrize("version", VERSIONS)
def test_binary_installs_and_verifies(binaries, version: str) -> None:
    """The pinned binary downloads and its SHA-512 matches the pin for this platform."""
    slug = install.detect_platform().slug
    result = binaries[version]
    assert result.path.exists()
    assert result.sha512 == install.pinned_sha512(version, slug)


@pytest.mark.parametrize("version", VERSIONS)
def test_binary_reports_expected_version(binaries, version: str) -> None:
    """The real binary runs and ``proton-drive version`` parses to the pinned semver.

    Guards the ``cli-drive@X.Y.Z`` version-parsing regex against every shipped build,
    and confirms the binary actually executes on this host (AVX2/glibc/keyring).
    """
    client = DriveClient(binary=str(binaries[version].path))
    assert client.drive_version() == version


@pytest.mark.parametrize("version", VERSIONS)
def test_pass_compat_gate_matches_installed_version(binaries, version: str) -> None:
    """`doctor`'s pass/proton-drive compat gate agrees with the real installed version.

    Ties :func:`pass_store_drive_compat_check` to actual binaries: it must pass for
    proton-drive >= 0.6.0 (pass-store aware) and fail below it.
    """
    client = DriveClient(binary=str(binaries[version].path))
    check = pass_store_drive_compat_check(client)
    expected_ok = _semver_tuple(version) >= _semver_tuple(PASS_STORE_MIN_DRIVE)
    assert check.ok is expected_ok


# --- auth tier: real session round-trips per binary (needs a throwaway remote) ---------

_auth = pytest.mark.skipif(
    not (MATRIX and REMOTE),
    reason="set PROTONFS_TEST_MATRIX=1 and PROTONFS_TEST_REMOTE to run the auth tier",
)


@_auth
@pytest.mark.parametrize("version", VERSIONS)
def test_binary_lists_remote_root_live(binaries, version: str) -> None:
    """Each binary authenticates and lists the Drive root through the wrapper.

    Proves the whole shell-out path (env, ``--json``, parse) works against every version
    with a real session -- the session established once for this host is reused by all.
    """
    client = DriveClient(binary=str(binaries[version].path))
    if not client.is_authenticated():
        pytest.skip(
            "no usable proton-drive session on this host; run `protonfs auth login` first"
        )
    entries = client.list("/")
    assert isinstance(entries, list)
