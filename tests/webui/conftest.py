"""Fixtures shared by the browser tests."""

from __future__ import annotations

import os

import pytest


# module scope: two sync_playwright() instances cannot be alive in one thread at once
@pytest.fixture(scope="module")
def pw_browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    try:
        pw = sync_api.sync_playwright().start()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"playwright cannot start: {exc!r}")
    try:
        if not os.path.exists(pw.chromium.executable_path):
            pytest.skip("playwright chromium is not installed")
        b = pw.chromium.launch()
    except Exception as exc:  # noqa: BLE001
        pw.stop()
        pytest.skip(f"chromium cannot launch: {exc!r}")
    yield b
    b.close()
    pw.stop()
