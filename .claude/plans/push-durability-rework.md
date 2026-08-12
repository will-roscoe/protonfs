# Design: push durability — per-directory save + streaming/hash-cache/interrupt

Assessment of the two remaining backlog items, grounded in the current code
(push.py, localscan.py, index.py, drive.py as of v1.7.0).

## Current behaviour (facts)

**Scan/hash (localscan.scan):** hashes EVERY to_push file up front, in one pass,
BEFORE any upload (push.py:169). `low_io` reuses a hash only when the **sync index**
already has an entry with matching size+mtime (localscan.py:125-129). Files not yet
synced — including everything on a resumed interrupted run — have no index entry, so
they are re-hashed from scratch. Hashes are never persisted independently of the sync
index.

**Upload/verify/save (push.py):** `group_by_parent(to_push)` → for each parent
directory group: upload in batches → ONE `remote_identities` verify-list for the whole
group → `index.set()` each verified/adopted file (in memory) → `index.save()` once, at
the END of the group (push.py:~300), then a final save after the loop. `index.save()`
rewrites the ENTIRE index atomically (temp + fsync + os.replace, index.py:152-180); the
sim index is ~7.9 MB / 20k+ entries, which is why save is per-group not per-batch.

**Interrupts:** no KeyboardInterrupt/signal handler anywhere. Ctrl+C → Click's bare
`Abort`, no context, in-memory index changes lost if save hadn't run.

**Key constraint:** DriveClient has NO single-file remote stat — only `list`/
`list_with_backoff`/`remote_identities` (whole-directory) and `walk` (recursive).
So any verify is a whole-directory listing, and each such call pays the ~12-32 s
proton-drive SDK startup measured this session. Per-batch verify would re-list the
(growing) directory once per batch → O(batches × dir) list calls → costly.

## Item 1: per-directory all-or-nothing index save

**Problem:** a flat sim dir is one parent group, so `index.save()` runs once at the end.
A process killed mid-group loses ALL in-memory index progress for that group.

**Already mitigated by v1.7.0 adopt.** On resume, a re-push re-uploads the group's files;
proton-drive fast-conflicts the ones already on Drive (~0.25 s each, no byte transfer,
proven: 0 `POST /storage/blocks`), and the single group verify ADOPTS them into the
index. So a mid-group kill no longer means lost data or a conflict-loop — it means the
next run re-does cheap conflict round-trips + one verify. Correctness is fine; the cost
is wasted work on resume.

**True per-batch durable indexing is NOT worth it right now:** it requires a verify per
batch, and verify is a whole-dir list (no single-file stat) → O(N²) list calls at
~12-32 s SDK startup each. For mload002 (~15 batches) that adds ~7 min of pure verify
overhead. Net negative vs. the adopt-on-resume path.

**If we do want finer checkpoints, the right shape is a per-batch UPLOAD JOURNAL, not
per-batch verify:** persist `{rel: "uploaded, pending-verify"}` to `.protonfs/` after
each batch; on resume, skip re-uploading journalled files and go straight to the group
verify. But adopt already gives ~90% of that benefit (fast conflict instead of a
re-upload), so the journal's marginal value is small. **Recommendation: defer;** revisit
only if proton-drive gains a cheap single-file stat (then per-batch verify+save becomes
O(N) and this becomes trivial).

## Item 2: streaming + hash-cache + KeyboardInterrupt (the higher-value work)

Three separable pieces, in priority order:

### 2a. KeyboardInterrupt handler (LOW effort, HIGH value) — do first
Catch `KeyboardInterrupt` at the CLI boundary (cli.main wrapper or per mutating
command). On catch: flush `index.save()` (persist whatever's indexed so far), print the
current phase + "interrupted after N/M files; already-synced files are recorded, re-run
to resume", exit 130. Turns Ctrl+C from "bare Abort, lost work, no context" into a clean,
informative, resumable stop. Composes with the existing per-group save. ~30-50 lines +
tests.

### 2b. Persistent hash-cache (MEDIUM effort, HIGH value) — do second
The biggest wasted-work-on-resume cost is the ~16 min re-hash of a sim, because `low_io`
only reuses hashes for files already in the SYNC index. Add a content-hash cache keyed by
`(rel_path, size, mtime) -> (sha256, sha1)`, persisted at `.protonfs/hashcache.json`
(gitignored), consulted in `scan()` BEFORE hashing, independent of sync state. A stale
entry is self-correcting (size/mtime guard, same as low_io today) and only ever costs a
recompute — never a wrong sync — so it is safe to write eagerly (after hashing each file,
or per batch). Version the cache format; let `doctor`/a flag force a full rehash.
Effect: a resumed or repeated push hashes only genuinely-new/changed files.
NOTE: this cleanly separates the two facts the index currently conflates — "content
hashes to X" (local, self-verifying, cheap to redo, safe to persist immediately) vs.
"present on Drive at remote_path" (needs remote verify, data-loss risk, persist only
after verify). That separation is the core idea.

### 2c. Streaming hash↔upload (HIGH effort, MEDIUM value) — do last / maybe skip
Restructure scan+push from "hash all, then upload all" into a pipeline: cheap
stat-only enumeration first (instant, gives the real progress denominator), then per
batch: hash → upload → (verify at group end as today). Benefits: uploads start after the
first batch (~seconds) instead of after the full ~16 min hash; live progress. But once 2a
+ 2b land, the marginal value drops: the re-hash pain is gone (2b), and the "silent 16 min"
can be fixed far more cheaply by just emitting per-file hashing progress in `scan()` at
`-v` (a few lines) rather than a full pipeline rewrite. Recommend: add hashing-phase
progress narration as a tiny standalone change; only build the full streaming pipeline if
a concrete need remains.

## Recommended plan (phased, each shippable independently)

1. **KeyboardInterrupt handler + flush** (2a) — small, high UX value. Own PR.
2. **Hashing-phase progress at -v** — tiny; kills the "silent 16 min" cheaply.
3. **Persistent hash-cache** (2b) — the real resume-cost fix. Own PR, versioned cache,
   doctor/flag to force rehash.
4. **Defer** per-batch durable save (Item 1) and the full streaming pipeline (2c) —
   adopt + hash-cache cover most of their value; revisit if proton-drive gains
   single-file stat or a concrete need appears.

Also worth filing (separate, larger): the dominant cost is proton-drive's ~12-32 s
per-invocation SDK startup and protonfs's one-process-per-operation model — a persistent/
long-lived proton-drive session (or batched multi-op invocation) would dwarf all of the
above in wall-clock savings.
