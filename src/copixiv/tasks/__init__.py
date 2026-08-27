"""Task system — kernel + business tasks (docs/MODULARITY.md §M8).

Kernel (generic, reusable) lives in :mod:`copixiv.tasks.kernel`:
declarative manifests + built-in discovery, the injected dependency
channel (:class:`copixiv.tasks.kernel.TaskContext`), the executor
(:class:`copixiv.tasks.kernel.TaskExecutor`), the history recorder
(:class:`copixiv.tasks.kernel.TaskHistoryRecorder`), the scheduler
(:class:`copixiv.tasks.kernel.CronScheduler`), and the facade
(:class:`copixiv.tasks.kernel.TaskManagerSystem`).

Business tasks (kernel users): ``novels`` / ``batch`` / ``maintenance`` —
self-describing module entries (Pydantic args model +
``(args, ctx)`` function + ``@register``).
"""
