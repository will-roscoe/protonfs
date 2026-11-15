"""The packaged page assets: present, d3 pinned, regions in place, wheel contains them."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import zipfile
from importlib import resources
from pathlib import Path

import pytest

D3_SHA256 = "f2094bbf6141b359722c4fe454eb6c4b0f0e42cc10cc7af921fc158fceb86539"
ASSETS = [
    "index.html",
    "app.css",
    "app.js",
    "vendor/d3.v7.9.0.min.js",
    "vendor/LICENSE-d3.txt",
]


def _static(name: str):
    node = resources.files("protonfs.webui") / "static"
    for part in name.split("/"):
        node = node / part
    return node


@pytest.mark.parametrize("name", ASSETS)
def test_static_assets_packaged(name):
    assert _static(name).is_file()


def test_d3_pinned():
    digest = hashlib.sha256(_static("vendor/d3.v7.9.0.min.js").read_bytes()).hexdigest()
    assert digest == D3_SHA256


def test_index_has_regions():
    html = _static("index.html").read_text("utf-8")
    for ident in ("explorer", "splitter", "charts", "goup", "breadcrumb", "sizemode",
                  "settings", "depth", "excludes", "icicle-toggle"):
        assert re.search(rf'id="{ident}"', html), ident
    for tab in ("treemap", "sunburst"):
        assert f'data-tab="{tab}"' in html
    assert '<meta name="pfs-token" content="{{PFS_TOKEN}}">' in html


def test_no_external_urls_outside_vendor():
    root = resources.files("protonfs.webui") / "static"
    for name in ("index.html", "app.css", "app.js"):
        text = (root / name).read_text("utf-8")
        assert not re.search(r"https?://", text), name


@pytest.mark.slow
@pytest.mark.skipif(
    os.environ.get("PROTONFS_TEST_WHEEL") != "1",
    reason="builds a wheel with pip (needs network for the build backend); "
    "set PROTONFS_TEST_WHEEL=1 to run",
)
def test_wheel_contains_static(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps",
         "-w", str(tmp_path), str(repo)],
        capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    wheel = next(tmp_path.glob("protonfs-*.whl"))
    names = zipfile.ZipFile(wheel).namelist()
    for name in ASSETS:
        assert f"protonfs/webui/static/{name}" in names, name
