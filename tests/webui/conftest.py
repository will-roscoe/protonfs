"""Fixtures shared by the browser tests."""

from __future__ import annotations

import os

import pytest


def start_chromium(sync_api):
    """Start Playwright and launch Chromium; skip the test, stopping the driver, if it cannot.

    :param sync_api: the ``playwright.sync_api`` module.
    :returns: ``(playwright, browser)``; the caller closes the browser then stops playwright.
    """
    try:
        pw = sync_api.sync_playwright().start()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"playwright cannot start: {exc!r}")
    try:
        if not os.path.exists(pw.chromium.executable_path):
            pytest.skip("playwright chromium is not installed")
        return pw, pw.chromium.launch()
    except pytest.skip.Exception:
        pw.stop()  # skip is not an Exception, so it needs its own clause
        raise
    except Exception as exc:  # noqa: BLE001
        pw.stop()
        pytest.skip(f"chromium cannot launch: {exc!r}")


# module scope: two sync_playwright() instances cannot be alive in one thread at once
@pytest.fixture(scope="module")
def pw_browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    pw, b = start_chromium(sync_api)
    yield b
    b.close()
    pw.stop()
