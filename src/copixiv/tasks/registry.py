"""Task registry and built-in task discovery."""

from __future__ import annotations

import importlib
import inspect
import types
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel

from copixiv.log import logger

TaskLane = Literal["fetch", "general"]

# Built-in task modules — the single source of truth for the task set.
DEFAULT_TASK_MODULES: tuple[str, ...] = (
    "copixiv.tasks.novels",
    "copixiv.tasks.batch",
    "copixiv.tasks.maintenance",
    "copixiv.tasks.sources",
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
    source: str | None = None
    timeout_sources: Callable[[Any, Any], Sequence[str] | Awaitable[Sequence[str]]] | None = None


_registry: dict[str, TaskSpec] = {}


def register(
    name: str,
    description: str = "",
    args: type[BaseModel] | None = None,
    *,
    lane: TaskLane = "general",
    timeout: float | None = None,
    notify: bool = True,
    source: str | None = None,
    timeout_sources: Callable[[Any, Any], Sequence[str] | Awaitable[Sequence[str]]] | None = None,
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
            source=source,
            timeout_sources=timeout_sources,
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


def _unwrap_annotation(annotation: Any) -> tuple[Any, bool]:
    """Unwrap Annotated / Optional / Union annotations into ``(inner_type, nullable)``."""
    while get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        args = get_args(annotation)
        nullable = type(None) in args
        non_none = [a for a in args if a is not type(None)]
        inner = non_none[0] if non_none else str
        while get_origin(inner) is Annotated:
            inner = get_args(inner)[0]
        return inner, nullable
    return annotation, False


def _map_arg_type(annotation: Any) -> str:
    """Map a Python annotation to the flat task argument type."""
    if annotation is bool:
        return "boolean"
    if annotation is int:
        return "integer"
    if annotation is float:
        return "number"
    origin = get_origin(annotation)
    if annotation is list or origin in (list, tuple, set):
        return "array"
    return "string"


def _describe_args_model(args_model: type[BaseModel]) -> list[dict[str, Any]]:
    """Flatten a Pydantic args model into field descriptors for the UI."""
    fields: list[dict[str, Any]] = []
    for field_name, field_info in args_model.model_fields.items():
        inner, nullable = _unwrap_annotation(field_info.annotation)
        required = field_info.is_required()
        default = None if required else field_info.get_default(call_default_factory=True)
        fields.append(
            {
                "name": field_name,
                "type": _map_arg_type(inner),
                "required": required,
                "nullable": nullable,
                "default": default,
                "description": field_info.description,
            }
        )
    return fields


def describe_tasks() -> list[dict[str, Any]]:
    """Return task descriptors for the ``/api/tasks/methods`` contract."""
    discover_tasks()
    methods = []
    for spec in _registry.values():
        methods.append(
            {
                "name": spec.name,
                "description": spec.description,
                "source": spec.source,
                "arguments": _describe_args_model(spec.args_model) if spec.args_model is not None else [],
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
