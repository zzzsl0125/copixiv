"""Task registry — maps task names to callables, no reflection scanning.

Each task registers itself via ``@register("task_name")`` at the bottom
of its module file.  The registry is a plain dict populated by imports.
"""

import inspect
from collections.abc import Callable, Awaitable

_registry: dict[str, Callable[..., Awaitable[object]]] = {}


def register(name: str):
    """Decorator to register an async task function under *name*."""
    def decorator(func: Callable[..., Awaitable[object]]):
        _registry[name] = func
        return func
    return decorator


def get_task(name: str) -> Callable[..., Awaitable[object]] | None:
    """Look up a task by name. Returns None if not found."""
    return _registry.get(name)


def list_tasks() -> dict[str, Callable[..., Awaitable[object]]]:
    """Return a copy of the registry."""
    return dict(_registry)


def describe_tasks() -> list[dict]:
    """Return descriptors of registered tasks with their parameter signatures.

    Each descriptor is ``{"name", "description", "arguments"}`` where
    ``arguments`` lists ``{"name", "type", "default", "required"}`` for
    every positional parameter of the task function.
    """
    methods = []
    for name, func in list_tasks().items():
        sig = inspect.signature(func)
        arguments = []
        for param_name, param in sig.parameters.items():
            if param.kind not in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            ):
                continue
            param_type = _annotation_type_name(param.annotation)

            default_val = None
            required = True
            if param.default != inspect.Parameter.empty:
                default_val = param.default
                required = False

            arguments.append({
                "name": param_name,
                "type": param_type,
                "default": default_val,
                "required": required,
            })
        methods.append({
            "name": name,
            "description": (func.__doc__ or "").strip(),
            "arguments": arguments,
        })
    return methods


def _annotation_type_name(annotation) -> str:
    """Map a task-parameter annotation to a display type name.

    Handles ``int``/``bool``/``float``/``str``, ``X | None`` unions (the
    non-None member wins), and falls back to ``"str"`` for anything else
    (the API contract documents task arguments as JSON scalars).
    """
    if annotation is inspect.Parameter.empty:
        return "str"
    origin = getattr(annotation, "__origin__", None)
    if origin is None:
        return {
            int: "int", bool: "bool", float: "float", str: "str",
        }.get(annotation, "str")
    # Union (e.g. ``int | None``) — pick the first non-NoneType member.
    for member in getattr(annotation, "__args__", ()) or ():
        if member is not type(None):
            return _annotation_type_name(member)
    return "str"
