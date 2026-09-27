# .github/scripts/repin_proton_drive.py
"""Maintainer tool: re-pin proton-drive checksums from the upstream manifest.

This script IS the upstream-tracking policy (issue #10), made executable:

1. Fetch the official release manifest (version.json) — it lists the current
   release version and a SHA-512 per published platform build.
2. Independently download each platform binary protonfs supports.
3. Hash the downloaded bytes and verify they match the manifest checksum —
   a pin is only ever produced from this double verification, so a compromised
   or corrupted manifest (or CDN object) can never be pinned silently.
4. Print the ready-to-paste ``PINNED_SHA512`` entries and the new
   ``DEFAULT_VERSION`` for src/protonfs/install.py.

Run it whenever upstream releases a new version:

    python .github/scripts/repin_proton_drive.py

Then update DEFAULT_VERSION + PINNED_SHA512 in src/protonfs/install.py with the
printed block (keep older pinned versions so PROTONFS_DRIVE_VERSION downgrades
remain verifiable), run the test suite, and note the bump in the changelog.
"""
from __future__ import annotations

import hashlib
import json
import sys
import urllib.request

MANIFEST_URL = "https://proton.me/download/drive/cli/version.json"
# Platforms protonfs supports (issue #9: native Windows is out of scope for 1.0;
# musl variants are unsupported until a target machine needs one).
SUPPORTED_SLUGS = ("linux-x64", "linux-arm64", "darwin-x64", "darwin-arm64")
TIMEOUT = 300  # seconds; the binaries are ~100 MB each


def main() -> int:
    with urllib.request.urlopen(MANIFEST_URL, timeout=60) as resp:
        manifest = json.load(resp)

    stable = next(
        (r for r in manifest["Releases"] if r.get("CategoryName") == "Stable"), None
    )
    if stable is None:
        print("no Stable release in manifest", file=sys.stderr)
        return 1

    version = stable["Version"]
    print(f"upstream stable: {version} (released {stable.get('ReleaseDate', '?')})\n")

    by_slug: dict[str, dict] = {}
    for f in stable["Files"]:
        # Url ends .../<version>/<slug>/proton-drive[.exe]
        slug = f["Url"].rstrip("/").split("/")[-2]
        by_slug[slug] = f

    entries: list[tuple[str, str]] = []
    failed = False
    for slug in SUPPORTED_SLUGS:
        entry = by_slug.get(slug)
        if entry is None:
            print(f"  {slug}: NOT in manifest — upstream dropped it?", file=sys.stderr)
            failed = True
            continue
        claimed = entry["Sha512CheckSum"].lower()
        print(f"  {slug}: downloading {entry['Url']} ...", flush=True)
        hasher = hashlib.sha512()
        with urllib.request.urlopen(entry["Url"], timeout=TIMEOUT) as resp:
            while chunk := resp.read(1024 * 256):
                hasher.update(chunk)
        actual = hasher.hexdigest()
        if actual != claimed:
            print(
                f"  {slug}: MISMATCH manifest={claimed} downloaded={actual} — "
                f"DO NOT PIN",
                file=sys.stderr,
            )
            failed = True
            continue
        print(f"  {slug}: verified {actual[:16]}…")
        entries.append((slug, actual))

    if failed:
        return 1

    print(f'\n# paste into src/protonfs/install.py\nDEFAULT_VERSION = "{version}"\n')
    for slug, sha in entries:
        print(f'    ("{version}", "{slug}"): (')
        print(f'        "{sha[:64]}"')
        print(f'        "{sha[64:]}"')
        print("    ),")
    return 0


if __name__ == "__main__":
    sys.exit(main())
