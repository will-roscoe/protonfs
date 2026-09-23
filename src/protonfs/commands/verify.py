"""``protonfs verify``: reconcile the remote manifest with a full listing of the remote.

The manifest (:mod:`protonfs.manifest`) is a cache of what protonfs verified on Drive.
``verify`` is its escape hatch: it walks the whole remote root and reports every entry
the manifest promises that Drive contradicts, and every remote file the manifest does
not know about. ``--repair`` rewrites the manifest to match the listing -- and is the
only way one is ever created, so a manifest always starts complete (#146).

.. versionadded:: 2.1.0
"""
from __future__ import annotations

from dataclasses import dataclass, field

from protonfs import manifest
from protonfs.context import RepoContext


@dataclass
class VerifyOutcome:
    """What ``verify`` found (and, with ``repair``, did).

    :ivar report: the comparison of the manifest read with the listing.
    :ivar index_state: ``{"generation", "revision"}`` this index was last reconciled
        with, or ``None``.
    :ivar repaired_generation: the generation written by ``--repair``, else ``None``.
    :ivar repaired_entries: number of entries in the repaired manifest.
    :ivar skipped_unsized: files ``--repair`` left out because the listing carries no
        plaintext size for them.
    """

    report: manifest.VerifyReport
    index_state: dict | None
    repaired_generation: int | None = None
    repaired_entries: int = 0
    skipped_unsized: list[str] = field(default_factory=list)


def verify(ctx: RepoContext, repair: bool = False, reporter=None) -> VerifyOutcome:
    """Compare the remote manifest with a full remote walk; optionally rebuild it.

    :param ctx: the loaded repo context.
    :param repair: rewrite the manifest to match the walk (creating it if absent).
    :param reporter: :class:`~protonfs.reporting.Reporter` to narrate progress through;
        defaults to the process reporter (:func:`~protonfs.reporting.get_reporter`).
    :returns: a :class:`VerifyOutcome`.
    :raises protonfs.manifest.ManifestError: when the manifest cannot be read, or
        ``repair`` cannot write it.
    :raises protonfs.drive.DriveError: on a Drive failure (a failed or throttled walk is
        an error: a partial listing cannot verify anything).
    """
    from protonfs.reporting import get_reporter

    reporter = reporter or get_reporter()
    reporter.phase("reading manifest")
    handle = manifest.RemoteManifest.load(ctx)
    reporter.phase("walking remote", root=ctx.config.remote_root)
    remote = manifest.remote_listing(ctx)
    outcome = VerifyOutcome(
        report=manifest.compare(handle.manifest if handle else None, remote),
        index_state=ctx.index.manifest_state,
    )
    if repair:
        reporter.phase("rewriting manifest")
        generation, entries, skipped = manifest.rebuild(ctx, remote, handle)
        outcome.repaired_generation = generation
        outcome.repaired_entries = entries
        outcome.skipped_unsized = skipped
    return outcome
