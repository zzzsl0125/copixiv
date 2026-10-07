"""本地书 id 换成时间轴坐标：``id = -(上传时刻的 pixiv 作品号坐标)``

Revision ID: 0006_local_id_time_axis
Revises: 0005_novel_source
Create Date: 2026-09-21 00:00:00.000000

``docs/LIBRARY_DESIGN.md`` §3.1。0005 的发号式是 ``id = -(10**9) + n`` —— 负数
区间让本地书永不与 pixiv 作品号撞主键（这一点是对的，保留），但 **10^9 是与时间
无关的锚点**：`abs(id) ≈ 10^9 ≫ pixiv 的 3×10^7`，所以一旦按「时间」排序就两条
路都错 —— 直接排 ``id`` 本地书恒在数轴负端（DESC 永远最后），排 ``abs(id)`` 又
恒在最前，而且本地书内部顺序反转（n 越大越新 → 模越大）。

本迁移把「模」换成**时间坐标**：坐标 = 上传时刻在 pixiv 作品号轴上的位置，
``id = -(坐标)``。负号仍是来源标记（本地行独占负数区间，永不撞号），而模变成
时钟 —— 排序统一走 ``abs(id)``（``features/novels/repo.py::time_axis_col``）。

三件事：

1. **存量本地行重编号**：按 ``create_time`` 在 id 轴上插值（``create_time <= t``
   的最大作品号），再取单调保底（严格递增）。旧的 ``-(10**9)+n`` 必须改 —— 它的
   模没有时间含义，不改就永远挂在榜首。id 长在文件路径里
   （``download/local/{标题}_{id}.txt``，§3.2），所以**重编号连文件一起改名**；
   文件找不到（用户删了、或 CWD 不在项目根）时保留原 path 不动 —— path 是读路径
   的真源，把指向不存在文件的路径改掉才是真损坏。改名先全量校验（目标不得被占），
   全部成功前不写库（写库在前、改名在后，改名失败则回滚已改的名字）。
2. **``novel_local_id_seq`` → ``novel_local_coord_seq``**：序列不再发 id，而是承载
   **坐标游标**（`id = -nextval()`）。序列不回退 —— 删掉最新那本再上传，新书拿的
   坐标更大，不会把旧引用（DSL ``id:`` / 导出 ``{id}``）指到另一本书。
3. **表达式索引 ``ix_novel_abs_id (abs(id), id)``**：``abs(id)`` 不是主键，没有它
   每次列表排序都要全表 top-N Sort（线上实测 1.3ms → 35ms）。

坐标数学与空轴种子在 ``features/library/local_axis.py`` 有一份运行时实现。本迁移
**故意内联同一套算式**：迁移一旦写完就不该跟着应用代码漂移（alembic 惯例）。
改公式请两边一起改，并核对 ``tests/features/test_library_axis_migration.py``。
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text as sa_text

revision: str = "0006_local_id_time_axis"
down_revision: Union[str, Sequence[str], None] = "0005_novel_source"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 0005 的发号式：id = -OLD_ANCHOR + n（n 从 1 起）
OLD_ANCHOR = 10 ** 9
LEGACY_SEQUENCE = "novel_local_id_seq"
COORD_SEQUENCE = "novel_local_coord_seq"

# 空轴种子（= 「库里一行 pixiv 都没有」时的兜底，与 local_axis.py 同源）：
# 线上校准点 29,155,910 @ 2026-09-17T15:00Z，近 90 天增速 8,509 号/天。
SEED_ID = 29_155_910
SEED_AT = datetime(2026, 9, 17, 15, 0, tzinfo=timezone.utc)
SEED_IDS_PER_DAY = 8509.0

# 两段式改号的中间偏移：先把本地行整体挪进一个与任何 id 都不相交的区间，
# 免得「A 的新号 = B 的旧号」在 UPDATE 途中撞主键。
_TEMP_OFFSET = 10 ** 12


def _seed_coord(when: datetime | None) -> int:
    """空轴兜底：把时刻线性外推成作品号坐标（恒 ≥ 1）。"""
    when = when or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(1, int(SEED_ID + (when - SEED_AT).total_seconds() / 86400.0
                      * SEED_IDS_PER_DAY))


def _coord_for_time(conn, when: datetime | None) -> int:
    """时刻 → id 轴坐标：``create_time <= when`` 的最大 pixiv 作品号。

    没有参照（空库 / when 为 NULL / early 于全库最早作品）时返回 0，由调用方兜底。
    """
    if when is None:
        return 0
    value = conn.execute(
        sa_text(
            "SELECT max(id) FROM novel "
            "WHERE source = 'pixiv' AND create_time <= :t"
        ),
        {"t": when},
    ).scalar()
    return int(value) if value is not None else 0


def _path_with_id(path: str | None, old_id: int, new_id: int) -> str | None:
    """把 ``..._{old_id}.txt`` 换成 ``..._{new_id}.txt``（不符约定则原样返回）。"""
    suffix = f"_{old_id}.txt"
    if path and path.endswith(suffix):
        return path[: -len(suffix)] + f"_{new_id}.txt"
    return path


def _renumber(conn, rows, new_id_for) -> list[tuple[int, int]]:
    """本地行重编号 + 文件改名 + path 同步。

    *rows*：``[(old_id, path, coord)]``，*new_id_for*：``coord -> new_id``。
    返回 ``[(old_id, new_id)]``。

    写库分两段（中间偏移），改名放最后且可回滚 —— 见模块 docstring。
    """
    updates: list[tuple[int, int, str | None]] = []
    renames: list[tuple[Path, Path]] = []

    for old_id, path, coord in rows:
        old_id, new_id = int(old_id), int(new_id_for(coord))
        final_path = path
        new_path = _path_with_id(path, old_id, new_id)
        if new_path != path:
            src, dst = Path(path), Path(new_path)
            if src.exists():
                if dst.exists():
                    raise RuntimeError(
                        f"本地书重编号中止：目标文件已存在 {dst}"
                    )
                renames.append((src, dst))
                final_path = new_path
            # else: 文件不在 → 保留原 path（它可能仍是对的，只是 CWD 不同）
        updates.append((old_id, new_id, final_path))

    if not updates:
        return []

    # 第一段：整体挪进不相交区间（避开「新号 = 别人的旧号」）
    conn.execute(
        sa_text("UPDATE novel SET id = id - :off WHERE source = 'local'"),
        {"off": _TEMP_OFFSET},
    )
    # 第二段：落到最终号 + 路径
    for old_id, new_id, final_path in updates:
        conn.execute(
            sa_text(
                "UPDATE novel SET id = :new_id, path = :path WHERE id = :temp_id"
            ),
            {
                "new_id": new_id,
                "path": final_path,
                "temp_id": old_id - _TEMP_OFFSET,
            },
        )

    # 改名放最后：DB 事务还没提交，失败就把已改的名字改回去再抛
    done: list[tuple[Path, Path]] = []
    try:
        for src, dst in renames:
            src.rename(dst)
            done.append((src, dst))
    except OSError as exc:  # pragma: no cover - 预校验后只剩磁盘级意外
        for src, dst in reversed(done):
            try:
                dst.rename(src)
            except OSError:
                pass
        raise RuntimeError(f"本地书重编号中止：文件改名失败 {exc}") from exc

    return [(old_id, new_id) for old_id, new_id, _ in updates]


def _local_rows(conn, order: str):
    return conn.execute(
        sa_text(f"SELECT id, path, create_time FROM novel WHERE source='local' {order}")
    ).fetchall()


def upgrade() -> None:
    conn = op.get_bind()

    # 1) 重编号：按上传时间插值 + 单调保底（严格递增）
    plan = []
    prev = 0
    for old_id, path, create_time in _local_rows(conn, "ORDER BY create_time NULLS FIRST, id"):
        coord = _coord_for_time(conn, create_time) or _seed_coord(create_time)
        coord = max(coord, prev + 1)
        prev = coord
        plan.append((int(old_id), path, coord))
    _renumber(conn, plan, new_id_for=lambda coord: -coord)

    # 2) 序列改名为坐标游标：拨到「已有最大坐标」，再收窄下界
    #    （顺序不能反：setval 到低于 MINVALUE 的值会被拒绝；``START`` 也得一起改，
    #     PostgreSQL 会拿它跟新 MINVALUE 对账 —— 「START value (-999999999)
    #     cannot be less than MINVALUE (1)」。START 只影响将来的 RESTART，
    #     不动当前值。）
    op.execute(f"ALTER SEQUENCE {LEGACY_SEQUENCE} RENAME TO {COORD_SEQUENCE}")
    op.execute(
        f"""
        SELECT setval(
            '{COORD_SEQUENCE}',
            COALESCE((SELECT max(-id) FROM novel WHERE source = 'local'), 1),
            (SELECT count(*) > 0 FROM novel WHERE source = 'local')
        )
        """
    )
    op.execute(f"ALTER SEQUENCE {COORD_SEQUENCE} START WITH 1 MINVALUE 1")

    # 3) 时间轴排序键的表达式索引
    op.execute("CREATE INDEX ix_novel_abs_id ON novel (abs(id), id)")


def downgrade() -> None:
    """回到 0005 的锚点发号：坐标升序 = 上传顺序 → id = -(10**9) + 1..N。"""
    conn = op.get_bind()
    op.execute("DROP INDEX IF EXISTS ix_novel_abs_id")

    rows = conn.execute(
        sa_text(
            "SELECT id, path FROM novel WHERE source = 'local' ORDER BY abs(id)"
        )
    ).fetchall()
    plan = [
        (int(old_id), path, n)
        for n, (old_id, path) in enumerate(rows, start=1)
    ]
    _renumber(conn, plan, new_id_for=lambda n: -OLD_ANCHOR + n)

    op.execute(f"ALTER SEQUENCE {COORD_SEQUENCE} RENAME TO {LEGACY_SEQUENCE}")
    op.execute(
        f"ALTER SEQUENCE {LEGACY_SEQUENCE} MINVALUE -999999999 "
        f"START WITH -999999999"
    )
    op.execute(
        f"""
        SELECT setval('{LEGACY_SEQUENCE}', COALESCE(v, -999999999), v IS NOT NULL)
        FROM (SELECT max(id) AS v FROM novel WHERE id < 0) AS anchor
        """
    )
