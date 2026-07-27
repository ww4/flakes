"""Write audit log.

Every write is logged: timestamp, tool, resulting path, byte count. Never note
bodies, never tokens, never the caller's query text — the log ends up in
journald and should be safe to read over someone's shoulder.

The prior art logs nothing on success, which means no forensics after a bad
write. This is the cheapest possible fix for that.
"""

from __future__ import annotations

import logging
import sys

_LOGGER_NAME = "homelab_mcp.audit"

_logger = logging.getLogger(_LOGGER_NAME)


def configure(level: int = logging.INFO) -> None:
    """Log to stdout; systemd routes it to journald."""
    if _logger.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(message)s"))
    _logger.addHandler(handler)
    _logger.setLevel(level)
    _logger.propagate = False


def record_write(tool: str, path: str, byte_count: int) -> None:
    """Record a successful write. Path and size only — never content."""
    _logger.info("write tool=%s path=%s bytes=%d", tool, path, byte_count)


def record_rejection(tool: str, reason: str) -> None:
    """Record a refused operation.

    `reason` must be the sanitised message from PathRejected, which
    deliberately does not echo the offending path.
    """
    _logger.warning("rejected tool=%s reason=%s", tool, reason)
