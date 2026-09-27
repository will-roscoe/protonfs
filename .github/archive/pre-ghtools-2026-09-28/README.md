# .github before ghtools

This directory is a copy of `.github/` as it was on 2026-09-28, taken by `ghtools init`
from commit `166fcf3`. Nothing here runs: GitHub only reads workflows placed directly
in `.github/workflows/`.

- Undo the switch to ghtools: `ghtools deinit`
- Restore by hand from the tag, if you created it: `git checkout pre-ghtools -- .github`
- Remove this archive once the ghtools pipeline has had a green run: `ghtools archive prune`
