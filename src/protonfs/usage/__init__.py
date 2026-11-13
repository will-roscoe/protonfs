"""Drive storage analysis engine behind ``protonfs du`` and its web UI.

The package has no Click or HTTP code. It measures how much space remote paths take on
Proton Drive, caches the figures with their age, and works out how much of a folder's
total is trash.

.. versionadded:: 3.4.0
"""
