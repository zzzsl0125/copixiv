"""Deprecated compatibility shim — re-exports the platform logging module.

The real implementation lives in :mod:`copixiv.log` (a platform module
any layer may import, see ``docs/MODULARITY.md`` §M0).  Internal code
must import from ``copixiv.log`` directly; this module only remains for
external scripts and old references.
"""

from copixiv.log import (  # noqa: F401
    InterceptHandler,
    capture_logs,
    logger,
    setup_logging,
)

__all__ = ["InterceptHandler", "capture_logs", "logger", "setup_logging"]
