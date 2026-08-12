# #26 — Drive-hosted sync-state manifest: design analysis & decision

Status: **DESIGN — recommend DEFER (do not implement now).** Keep issue open as a
tracked future optimization, downgraded from "feature" to "optimization".

## Question

Should protonfs store global per-file sync state in a manifest hosted in Proton
Drive (e.g. `/my-files/.protonstate/<reponame>`), instead of only in each client's
local `index.json`?

## What changed the premise

The original motivation ("a file uploaded by client1 should be visible to client2
with correct state without a git round-trip") is **already satisfied without a
manifest**. `proton-drive filesystem list --json` exposes, per file, the plaintext
`claimedSize`, `claimedDigests.sha1`, and `claimedModificationTime`. `refresh`
walks the remote live and (after #24) derives authoritative per-file state —
including content identity via sha1 — directly from the remote. So cross-client
state is *possible today* from the live remote alone. The manifest is therefore an
**optimization**, not a correctness prerequisite.

## What a manifest would still buy

1. **Avoid the full recursive walk.** For large trees (tens of thousands of
   entries) `refresh` is expensive and — from a throttled host — slow/flaky (see
   #33). A single small manifest download is far cheaper than walking N
   directories with a `list` per directory.
2. **protonfs-specific metadata proton does not store:** origin device, protonfs's
   own sha256 (proton only keeps sha1), and *intended* operations that have no
   remote representation — notably a queued local-deletion (the `local-deleted`
   state #24 introduces) or a queued offload (#25's `metadata-only` transition).

## Why NOT to implement now

1. **Concurrency is the hard part, recreated one level down.** Two clients
   updating the manifest concurrently need conflict handling *for the manifest
   itself* — the exact problem protonfs exists to solve, now applied to a single
   shared file with no natural per-path isolation. The live remote (source of
   truth) sidesteps this entirely.
2. **Staleness.** If the manifest and the live remote disagree, the live remote
   must win (it is the truth), which demotes the manifest to a cache/hint. A hint
   that can be wrong must always be validated against the remote before any
   destructive action (#25 offload already re-lists the remote per #22), so the
   manifest cannot remove the verify step it was meant to save.
3. **#33 largely closes the performance gap by other means.** Incremental
   per-directory persistence + resumable refresh + throttle backoff make the live
   walk survivable and cheap-on-re-run without a new shared store. The strongest
   argument for the manifest (avoid the expensive walk) is weakened once refresh
   is incremental and resumable.
4. **Scope/layering risk.** Per #21, protonfs already has a shared layer
   (`config.json` + `ignore`, committed to git) and a per-device layer
   (`index.json`, local). A manifest is a *third, shared-state* layer. Introducing
   it ad-hoc before #21's config-layering model lands would create a fourth
   storage location with unclear precedence. If ever pursued, it must slot into
   #21's model, not precede it.

## Recommended decision

- **Defer implementation.** Cross-client correctness is delivered by #24 (rich
  classification from live remote) + #22 (verify-against-remote) + #5 (resolution)
  + #33 (survivable refresh). None of them need the manifest.
- **Keep #26 open** as a future *optimization*, explicitly gated on:
  (a) #33 shipping and proving whether refresh cost is still a real pain, and
  (b) #21's config-layering model landing (so the manifest slots in as the
  shared-state layer with defined precedence: live remote > manifest hint >
  local index).
- **If revisited**, design it as a *hint cache* only: the manifest never
  authorizes a destructive action on its own; every offload/delete still verifies
  against the live remote (#22). Its job is purely to let `status`/`refresh` skip
  the full walk on the common no-change path, falling back to the live walk on any
  mismatch or on a manifest older than a TTL.

## Manifest sketch (only if/when implemented)

- Path: `<remote_root>/.protonstate/<reponame>.json` (sibling of the data, so a
  single client owns write; or per-device manifests `…/<reponame>.<device>.json`
  merged on read to dodge write-concurrency — preferred).
- Contents: `{schema_version, generated_at, device_id, entries: {rel_path:
  {claimed_size, sha1, claimed_mtime, origin_device, intended_op}}}`.
- Read: download all per-device manifests, merge newest-wins per path, treat as a
  hint; validate against live remote before any destructive op.
- Write: each device writes only its own manifest after a successful push/offload.
  No cross-device locking needed because no device writes another's file.
