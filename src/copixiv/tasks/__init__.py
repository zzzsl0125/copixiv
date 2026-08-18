"""Task system — kernel + business tasks (docs/MODULARITY.md §M8).

Kernel (generic, reusable):
- :mod:`copixiv.tasks.registry` — declarative manifests + built-in discovery
- :mod:`copixiv.tasks.context` — the injected dependency channel
- :mod:`copixiv.tasks.executor` — context building + execution + lifecycle
- :mod:`copixiv.tasks.history` — task_history row ownership
- :mod:`copixiv.tasks.scheduler` — APScheduler ownership + cron jobs
- :mod:`copixiv.tasks.manager` — the facade (``TaskManagerSystem``)

Business tasks (kernel users): ``novel_tasks`` / ``batch_tasks`` /
``maintenance`` — self-describing module entries (Pydantic args model +
``(args, ctx)`` function + ``@register``).
"""
