"""Value types the ``du`` engine returns: :class:`Entry`, :class:`Breakdown` and their enums.

:class:`~protonfs.usage.engine.UsageEngine` produces these; the terminal report and the web
UI read them. Every figure carries the Unix time it was measured (``at``).

.. versionadded:: 3.4.0
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from .excludes import Exclusion


class NodeState(enum.Enum):
    """What is known about a node.

    - ``LISTED``: a folder whose own listing is cached (it can be broken down).
    - ``SIZED``: a size figure is known but no listing (always the case for files).
    - ``EXCLUDED``: a folder matched by a freeze rule; its cached figure, if any, is shown
      without being refreshed.
    - ``UNMEASURED``: no figure is known (nothing cached, or the node cannot be sized).
    - ``ERROR``: the Drive call for this node failed; see the ``error`` text.

    .. versionadded:: 3.4.0
    """

    LISTED = "listed"
    SIZED = "sized"
    EXCLUDED = "excluded"
    UNMEASURED = "unmeasured"
    ERROR = "error"


class GapKind(enum.Enum):
    """How the remainder of a folder's size, beyond its listed children, is labelled.

    - ``NONE``: there is no remainder (or no breakdown to compute one from).
    - ``TRASH`` (``"in_trash"``): every input comes from one pass and every counted child
      has a figure, so the remainder is trashed items whose original parent is this folder.
    - ``UNATTRIBUTED``: some child (excluded, failed or unmeasured) has no figure, so the
      remainder includes its bytes.
    - ``UNRECONCILED``: the remainder is negative, or the inputs come from different
      passes, so it cannot be attributed.

    .. versionadded:: 3.4.0
    """

    NONE = "none"
    TRASH = "in_trash"
    UNATTRIBUTED = "unattributed"
    UNRECONCILED = "unreconciled"


@dataclass(frozen=True)
class Entry:
    """One child of a breakdown: a file, a folder or (in the ``/`` view) an account section.

    :ivar name: plain (unescaped) name; the node uid when the name could not be decrypted.
    :ivar path: escaped Drive path of the node.
    :ivar kind: ``"file"``, ``"folder"`` or ``"section"`` (entries of the ``/`` view).
    :ivar uid: Drive node uid, if known.
    :ivar size: bytes (encrypted ``totalStorageSize``, trashed descendants included for
        folders), or ``None`` when unknown.
    :ivar descendants: node count beneath a folder, ``None`` for files or when unknown.
    :ivar at: Unix time the size was measured, or ``None`` when unknown.
    :ivar state: what is known, see :class:`NodeState`.
    :ivar exclusion: the freeze/hide rule that matched, or ``Exclusion.NONE``.
    :ivar mtime: ``modificationTime`` from the listing, if any.
    :ivar decrypted: whether ``name`` is the decrypted name.
    :ivar error: the failure text when ``state`` is ``ERROR``, else ``None``.

    .. versionadded:: 3.4.0
    """

    name: str
    path: str
    kind: str
    uid: str | None
    size: int | None
    descendants: int | None
    at: float | None
    state: NodeState
    exclusion: Exclusion
    mtime: str | None
    decrypted: bool
    error: str | None = None


@dataclass(frozen=True)
class Breakdown:
    """A folder's size split into its listed children plus a labelled remainder.

    ``size == Σ entry sizes + Σ hidden-row sizes + gap`` whenever every counted child has
    a figure. Hidden (``-X``) children are not in ``entries`` but still count toward
    ``gap`` and ``gap_kind``.

    :ivar path: escaped Drive path of the folder (``"/"`` for the account view).
    :ivar size: the folder total in bytes, or ``None`` when unknown.
    :ivar descendants: node count beneath the folder, or ``None`` when unknown.
    :ivar at: Unix time ``size`` was measured (for ``/``, the oldest entry's time).
    :ivar state: ``LISTED`` with entries; ``SIZED`` when only the total is cached;
        ``UNMEASURED`` when nothing is known; ``EXCLUDED`` when the folder itself matches
        an exclusion rule; ``ERROR`` when its ``size`` or ``list`` call failed.
    :ivar entries: the visible children in listing order; empty when no listing is cached
        or the folder's own call failed.
    :ivar gap: ``size`` minus the known child sizes, or ``None`` without a listing.
    :ivar gap_kind: the label of ``gap``, see :class:`GapKind`.
    :ivar oldest_at: the oldest measurement time among all inputs, or ``None``.
    :ivar newest_at: the newest measurement time among all inputs, or ``None``.
    :ivar error: the failure text when ``state`` is ``ERROR``, else ``None``.

    .. versionadded:: 3.4.0
    """

    path: str
    size: int | None
    descendants: int | None
    at: float | None
    state: NodeState
    entries: tuple[Entry, ...]
    gap: int | None
    gap_kind: GapKind
    oldest_at: float | None
    newest_at: float | None
    error: str | None = None
