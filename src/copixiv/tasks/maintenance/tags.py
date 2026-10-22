"""``rebuild_tag_counts`` — recompute the denormalized tag reference counts.

Split verbatim out of ``copixiv.tasks.maintenance`` (see that package's
docstring).
"""

from copixiv.core.models import TaskResult
from copixiv.db.write_lock import run_write_transaction

from ..kernel import TaskContext
from ..kernel import register


@register("rebuild_tag_counts", description="重算每个标签的引用计数")
async def rebuild_tag_counts(ctx: TaskContext) -> TaskResult:
    """Recompute every tag's ``reference_count`` from ``novel.tags``.

    The denormalized counter drifts over time: v1 legacy data, deletes
    that predated the decrement-on-delete fix, and alias retroactive
    moves all leave stale values.  Measured on the production 232k DB,
    ~10 % of tags (7500/73037) had a count that didn't match the actual
    distinct-novel count, with errors up to ±14000.

    This task recalculates ``reference_count`` from the array column that
    is now the single source of truth
    (``SELECT count(*) FROM novel WHERE tags @> ARRAY[tag.name]``) in a
    single correlated UPDATE — fast, exact, and independent of the
    trigger's incremental bookkeeping.
    """
    from sqlalchemy import text, func, select
    from copixiv.db import models

    uow = ctx.uow

    async def _run(uow):
        uow.session.execute(text(
            "UPDATE tag SET reference_count = ("
            "  SELECT count(*) FROM novel WHERE tags @> ARRAY[tag.name]"
            ")"
        ))
        # SQLAlchemy doesn't report rowcount reliably for correlated
        # UPDATEs, so count tags explicitly.
        total = uow.session.execute(
            select(func.count()).select_from(models.Tag)
        ).scalar() or 0
        drifted = uow.session.execute(text(
            "SELECT COUNT(*) FROM tag WHERE reference_count != ("
            "  SELECT count(*) FROM novel WHERE tags @> ARRAY[tag.name])"
        )).scalar() or 0
        return total, drifted

    total, drifted = await run_write_transaction(uow, _run)

    # The commit above (uow.begin() exit) bumps the data epoch, so the
    # count cache invalidates itself — no manual invalidate needed.

    return TaskResult(
        summary=f"标签引用计数重建: {total} 个标签, 修正 {drifted} 个偏差"
    )
