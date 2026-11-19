"""Task registry and built-in task discovery."""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from copixiv.log import logger

TaskLane = Literal["fetch", "general"]

# Built-in task modules — the single source of truth for the task set.
DEFAULT_TASK_MODULES: tuple[str, ...] = (
    "copixiv.tasks.novels",
    "copixiv.tasks.batch",
    "copixiv.tasks.maintenance",
)


@dataclass(frozen=True)
class TaskSpec:
    """A registered task's manifest: metadata + function + argument schema."""

    name: str
    description: str
    func: Callable[..., Awaitable[object]]
    args_model: type[BaseModel] | None = None
    lane: TaskLane = "general"
    timeout: float | None = None
    notify: bool = True


_registry: dict[str, TaskSpec] = {}


def register(
    name: str,
    description: str = "",
    args: type[BaseModel] | None = None,
    *,
    lane: TaskLane = "general",
    timeout: float | None = None,
    notify: bool = True,
) -> Callable:
    """Register an async task function under *name* with a declarative manifest."""

    def decorator(func):
        if not inspect.iscoroutinefunction(func):
            raise TypeError(
                f"Task {name!r} must be an async def function: synchronous "
                "tasks cannot be cancelled on timeout and would block the "
                "event loop. Wrap blocking work in asyncio.to_thread."
            )
        _registry[name] = TaskSpec(
            name=name,
            description=(description or (func.__doc__ or "").strip()),
            func=func,
            args_model=args,
            lane=lane,
            timeout=timeout,
            notify=notify,
        )
        return func

    return decorator


def get_spec(name: str) -> TaskSpec | None:
    """Look up a task manifest by name."""
    return _registry.get(name)


def get_task(name: str) -> Callable[..., Awaitable[object]] | None:
    """Look up a task function by name (convenience for callers)."""
    spec = _registry.get(name)
    return spec.func if spec else None


def task_names_for_lane(lane: TaskLane) -> tuple[str, ...]:
    """Return all registered task names currently assigned to *lane*."""
    return tuple(name for name, spec in _registry.items() if spec.lane == lane)


def describe_tasks() -> list[dict]:
    """Return task descriptors for the ``/api/tasks/methods`` contract."""
    discover_tasks()
    methods = []
    for spec in _registry.values():
        methods.append(
            {
                "name": spec.name,
                "description": spec.description,
                "arguments": (spec.args_model.model_json_schema() if spec.args_model is not None else None),
            }
        )
    return methods


def discover_tasks() -> None:
    """Import every built-in task module (:data:`DEFAULT_TASK_MODULES`)."""
    for module_name in DEFAULT_TASK_MODULES:
        try:
            importlib.import_module(module_name)
        except Exception:
            logger.exception(
                "Failed to import task module '%s' — task(s) not registered.",
                module_name,
            )
