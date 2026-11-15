"""Environment for the browser tests: where Playwright finds its Chromium."""

from __future__ import annotations

import os

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/win/d/.mint/cache/playwright/browsers")
