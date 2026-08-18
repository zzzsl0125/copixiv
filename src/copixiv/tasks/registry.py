"""Task registry — declarative, self-describing task registration.

A task is a module-level function decorated with :func:`register`,
carrying its own manifest: name, description, and a Pydantic argument
schema.  This is the copixiv equivalent of a DSH plugin manifest — the
registry stores :class:`TaskSpec` records, and argument descriptions come
from the Pydantic ``args`` model instead of ``inspect.signature``
reflection (docs/MODULARITY.md §M8).

Discovery (:func:`discover_tasks`) imports the built-in task modules
(:data:`DEFAULT_TASK_MODULES`).  There is no third-party plugin
ecosystem — docs/MODULARITY.md §6.
"""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from copixiv.log import logger

# Built-in task modules — the single source of truth for the task set.
DEFAULT_TASK_MODULES: tuple[str, ...] = (
    "copixiv.tasks.novel_tasks",
    "copixiv.tasks.batch_tasks",
    "copixiv.tasks.maintenance",
)


@dataclass(frozen=True)
class TaskSpec:
    """A registered task's manifest: metadata + function + argument schema."""

    name: str
    description: str
    func: Callable[..., Awaitable[object]]
    args_model: type[BaseModel] | None = None


_registry: dict[str, TaskSpec] = {}


def register(
    name: str,
    description: str = "",
    args: type[BaseModel] | None = None,
) -> Callable:
    """Register a task function under *name* with a declarative manifest.

    Args:
        name: Task name (stored in ``scheduled_tasks.task`` / history).
        description: Human-readable description; defaults to the
            function's docstring.
        args: Pydantic model for the task's JSON arguments — validated and
            converted by the executor before the function runs.  ``None``
            for parameter-less tasks.
    """

    def decorator(func):
        _registry[name] = TaskSpec(
            name=name,
            description=(description or (func.__doc__ or "").strip()),
            func=func,
            args_model=args,
        )
        return func

    return decorator


def unregister(name: str) -> bool:
    """Remove a task from the registry (plugin unload / tests).

    Returns True when a task was actually removed.
    """
    return _registry.pop(name, None) is not None


def get_spec(name: str) -> TaskSpec | None:
    """Look up a task manifest by name."""
    return _registry.get(name)


def get_task(name: str) -> Callable[..., Awaitable[object]] | None:
    """Look up a task function by name (convenience for callers)."""
    spec = _registry.get(name)
    return spec.func if spec else None


def list_tasks() -> dict[str, Callable[..., Awaitable[object]]]:
    """Return a copy of ``{name: function}``."""
    return {name: spec.func for name, spec in _registry.items()}


def describe_tasks() -> list[dict]:
    """Return task descriptors for the ``/api/tasks/methods`` contract.

    Each descriptor is ``{"name", "description", "arguments"}`` where
    ``arguments`` lists ``{"name", "type", "default", "required"}`` —
    derived purely from the task's Pydantic args model (no signature
    reflection).
    """
    discover_tasks()
    methods = []
    for spec in _registry.values():
        methods.append({
            "name": spec.name,
            "description": spec.description,
            "arguments": _describe_arguments(spec),
        })
    return methods


def _describe_arguments(spec: TaskSpec) -> list[dict]:
    if spec.args_model is None:
        return []
    arguments = []
    for field_name, info in spec.args_model.model_fields.items():
        arguments.append({
            "name": field_name,
            "type": _annotation_type_name(info.annotation),
            "default": None if info.is_required() else info.default,
            "required": info.is_required(),
        })
    return arguments


def _annotation_type_name(annotation: Any) -> str:
    """Map a Pydantic field annotation to a display type name.

    Handles ``int``/``bool``/``float``/``str``/``list[...]``, ``X | None``
    unions (the non-None member wins), and falls back to ``"str"`` for
    anything else (the API contract documents task arguments as JSON
    scalars; lists render as text inputs in the task editor).
    """
    if annotation is None:
        return "str"
    origin = getattr(annotation, "__origin__", None)
    if origin is None:
        return {
            int: "int", bool: "bool", float: "float", str: "str",
        }.get(annotation, "str")
    if origin is list:
        return "list"
    # Union (e.g. ``int | None``) — pick the first non-NoneType member.
    for member in getattr(annotation, "__args__", ()) or ():
        if member is not type(None):
            return _annotation_type_name(member)
    return "str"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_tasks() -> None:
    """Import every built-in task module (:data:`DEFAULT_TASK_MODULES`).

    Idempotent — imports are cheap no-ops on repeat calls.  A module that
    fails to import is logged loudly (never silent) so the failure is
    visible instead of silently dropping its tasks.
    """
    for module_name in DEFAULT_TASK_MODULES:
        try:
            importlib.import_module(module_name)
        except Exception:
            logger.exception(
                "Failed to import task module '%s' — task(s) not registered.",
                module_name,
            )
