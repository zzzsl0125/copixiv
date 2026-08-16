"""Demo third-party task plugin (docs/MODULARITY.md §M8 验收件).

Simulates a package installed by the user that contributes one task via
the ``copixiv.tasks`` entry-point group.  The registry never imports this
module directly — discovery loads it, and ``@register`` fires.
"""

from pydantic import BaseModel

from copixiv.domain.models.task_result import TaskResult
from copixiv.tasks.context import TaskContext
from copixiv.tasks.registry import register


class DemoArgs(BaseModel):
    label: str = "demo"


@register("demo_task", description="第三方演示任务：验证插件发现链路", args=DemoArgs)
async def demo_task(args: DemoArgs, ctx: TaskContext) -> TaskResult:
    """Return a summary echoing the arg and the injected task id."""
    return TaskResult(summary=f"demo:{args.label}:task_id={ctx.task_id}")
