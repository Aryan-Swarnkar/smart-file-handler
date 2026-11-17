"""Worker error taxonomy shared by tasks and pipelines.

`NonRetryableError` is the terminal-failure signal: the task body maps it to
files.status='failed' with a display-safe reason (surfaced by the API via
files.error). It lives HERE (not in tasks.py) so pipeline modules can raise
it without importing the task module (tasks imports pipelines — an import in
the other direction would cycle).
"""

from __future__ import annotations


class NonRetryableError(Exception):
    """Terminal, deterministic failure — retrying cannot succeed (bad kind,
    corrupt object, unsupported payload). `reason` is display-safe text the
    API surfaces via files.error; never embed SQL or internals in it."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
