"""Task registry — maps task names to callables, no reflection scanning.

Each task registers itself via ``@register("task_name")`` at the bottom
of its module file.  The registry is a plain dict populated by imports.
"""

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
