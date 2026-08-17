"""Notifier-backend registry — same mental model as the task registry.

Each backend module declares a name and a ``build(config)`` factory,
registered via :func:`register_backend` at module import (docs/
MODULARITY.md §M6).  :func:`discover_backends` imports the built-in
backend modules; third-party backends can later join through a
``copixiv.notifiers`` entry-point group, exactly like task plugins.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any

from copixiv.log import logger

# Built-in backend modules — imported by discover_backends() so their
# @register_backend decorators fire.
DEFAULT_BACKEND_MODULES: tuple[str, ...] = (
    "copixiv.infrastructure.notifier.telegram",
    "copixiv.infrastructure.notifier.webhook",
)

# name → build factory (config → NotifierBackendPort instance)
_registry: dict[str, Callable[[Any], Any]] = {}


def register_backend(name: str) -> Callable:
    """Register a backend build factory under *name*."""

    def decorator(build: Callable) -> Callable:
        _registry[name] = build
        return build

    return decorator


def get_backend_builder(name: str) -> Callable[[Any], Any] | None:
    """Look up a backend factory by name; None when unknown."""
    return _registry.get(name)


def list_backends() -> dict[str, Callable[[Any], Any]]:
    """Return a copy of ``{name: build_factory}``."""
    return dict(_registry)


def discover_backends() -> None:
    """Import every built-in backend module (idempotent).

    A module that fails to import is logged loudly (never silent) and
    skipped, so one bad backend cannot take down the whole app.
    """
    for module_name in DEFAULT_BACKEND_MODULES:
        try:
            importlib.import_module(module_name)
        except Exception:
            logger.exception(
                "Failed to import notifier backend module '%s' — "
                "backend(s) not registered.",
                module_name,
            )
