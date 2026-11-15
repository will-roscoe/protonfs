"""Local browser UI for ``protonfs du --gui``.

A stdlib HTTP server (:mod:`protonfs.webui.server`) serves a static page that draws the
:mod:`protonfs.usage` engine's figures as an explorer table, a treemap and a
sunburst/icicle view. The routes map onto a plain-Python :class:`~protonfs.webui.api.Api`,
which contains no HTTP code.

.. versionadded:: 3.4.0
"""
