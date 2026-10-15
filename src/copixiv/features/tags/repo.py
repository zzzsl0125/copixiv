"""Tag repository — tags, preferences, and aliases.

TagAlias uses integer FKs (source, target) → tag.id.  The API layer
continues to work with tag *names*; this repository handles the
name ↔ id translation internally.
"""

from collections.abc import Sequence
from typing import Any, NamedTuple

from sqlalchemy import select, text as _text, update as _update, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from copixiv.db import models
from copixiv.db.base import BaseRepository

# 相似标签的 LIKE 模式与关键字搜索共用**同一个**转义器（``\`` / ``%`` / ``_``）：
# 直接 import，不在这里抄第二份实现。这条 tags → novels.search 的边指向叶子模块
# （``novels/search.py`` 只 import sqlalchemy / 标准库，不 import 任何 feature
# 模块），运行期不成环；另一条方向（``novels/repo.py`` 等 → 本模块）是既有的。
from copixiv.features.novels.search import escape_like


# =========================================================================
# suggest_aliases — SQL 底座（迁移 0008：tag.search_lower + pg_bigm GIN）
# =========================================================================

# 每个 target 的候选数上限（= 旧实现 `_find_similar_tags` 里的 LIMIT 50）。
_MAX_CANDIDATES = 50

# 单次调用「检视」多少个 target —— 有界性的唯一来源。旧实现是 while 循环
# 「一直取页、直到凑满 limit 个真有候选的 target」，实测最坏 15s / 1,505 条
# page 查询；新实现固定检视 4×limit 个 target（下限 20、上限 200）。API 把
# limit 限制在 ≤50，所以单次调用最多 200 个 target，与 tag 表大小无关。
_SCAN_FACTOR = 4
_SCAN_FLOOR = 20
_SCAN_CAP = 200


class _SuggestTarget(NamedTuple):
    """``_make_suggest_item`` 需要的 target 三元组（与 ORM ``Tag`` 同形）。"""

    id: int
    name: str
    reference_count: int


def _scan_limit(limit: int) -> int:
    """单次调用最多检视多少个 target（4×limit，clamp 到 20–200）。"""
    return min(max(limit * _SCAN_FACTOR, _SCAN_FLOOR), _SCAN_CAP)


def _like_patterns(name: str) -> tuple[str, str]:
    """``(前缀模式, 子串模式)`` —— 两个都经共享的 :func:`escape_like` 转义。

    前缀分支用**首字符**（与原实现 ``name ILIKE 'X%'`` 同义），子串分支用
    完整名字。转义是必须的：库里真有叫 ``%高中生`` 的 tag，不转义时
    ``LIKE '%高中生%'`` 会匹配全表 75,186 行。名字为空时返回 ``("", "")``，
    ``LIKE ''`` 不匹配任何行（旧实现同样对空名字返回空候选）。
    """
    if not name:
        return "", ""
    return f"{escape_like(name[0])}%", f"%{escape_like(name)}%"


def _candidate_dict(row) -> dict:
    """候选行的对外形状 —— 与旧 ``_find_similar_tags`` 的 dict 逐字段一致。"""
    return {
        "id": row.id,
        "name": row.name,
        "reference_count": row.reference_count,
    }


# 通用模式第一步：keyset 分页取 target（代价有界 = LIMIT :scan，走
# ``tag_reference_count_id_idx`` 这条 (reference_count DESC, id DESC) btree）。
# 过滤（``rc > 0`` / 已在别名映射里）全部在 SQL 里、早于候选上限；旧实现是先取
# ``LIMIT 50`` 再在 Python 里丢，于是一页建议可能整页变空。
# ``:cursor`` = 上一次的 ``next_offset``（0 = 从头）；游标行不存在时
# ``cursor_found`` 为 false，调用方据此收尾而不是从头再来（否则重复吐建议）。
_PAGE_SQL = _text(
    """
    WITH cur AS (
        SELECT reference_count AS rc, id AS cid FROM tag WHERE id = :cursor
    )
    SELECT t.id, t.name, t.reference_count,
           (cur.cid IS NOT NULL) AS cursor_found
    FROM tag t
    LEFT JOIN cur ON TRUE
    WHERE t.reference_count > 0
      AND (cur.cid IS NULL OR (t.reference_count, t.id) < (cur.rc, cur.cid))
      AND NOT EXISTS (SELECT 1 FROM tag_alias a WHERE a.source = t.id)
      AND NOT EXISTS (SELECT 1 FROM tag_alias a WHERE a.target = t.id)
    ORDER BY t.reference_count DESC, t.id DESC
    LIMIT :scan
    """
)

# 通用模式第二步：一条语句给整页 target 各算一份候选（替代 N+1 循环）。
# ``WITH cand AS MATERIALIZED`` 是**有意的计划钉子**：不加它，规划器会在
# 「顺序扫 tag_reference_count_id_idx + Filter」（前缀很热的 target 要扫掉
# 2.8 万行，实测 8.8ms/个）与「GIN bitmap + top-N Sort」之间按估算摇摆。
# MATERIALIZED 先物化每个 target 的匹配集（GIN bitmap），再取最热的 50 个 ——
# 实测 0.24–1.2ms/个，计划形状稳定、无 Seq Scan on tag。
_EXPAND_SQL = _text(
    """
    SELECT p.id AS target_id, c.id, c.name, c.reference_count
    FROM unnest(
             CAST(:ids AS bigint[]), CAST(:pfx AS text[]), CAST(:sub AS text[])
         ) WITH ORDINALITY AS p(id, pfx, sub, ord)
    CROSS JOIN LATERAL (
        WITH cand AS MATERIALIZED (
            SELECT t2.id, t2.name, t2.reference_count
            FROM tag t2
            WHERE t2.id <> p.id
              AND NOT EXISTS (SELECT 1 FROM tag_alias a WHERE a.source = t2.id)
              AND NOT EXISTS (SELECT 1 FROM tag_alias a WHERE a.target = t2.id)
              AND (t2.search_lower LIKE lower(p.pfx) ESCAPE '\\'
                   OR t2.search_lower LIKE lower(p.sub) ESCAPE '\\')
        )
        SELECT cand.id, cand.name, cand.reference_count
        FROM cand
        ORDER BY cand.reference_count DESC, cand.id DESC
        LIMIT :ncand
    ) c
    ORDER BY p.ord, c.reference_count DESC, c.id DESC
    """
)

# ``target_tag=`` 模式：一条语句做「查名字 + 排除已别名 + 各候选」。模式参数由
# Python 侧用共享 escaper 转义（这里的名字来自用户输入）。
_TARGET_SQL = _text(
    """
    WITH tgt AS (
        SELECT t.id, t.name, t.reference_count
        FROM tag t
        WHERE t.name = :name
          AND NOT EXISTS (SELECT 1 FROM tag_alias a WHERE a.source = t.id)
          AND NOT EXISTS (SELECT 1 FROM tag_alias a WHERE a.target = t.id)
    )
    SELECT tgt.id AS target_id, tgt.name AS target_name,
           tgt.reference_count AS target_reference_count,
           c.id, c.name, c.reference_count
    FROM tgt
    CROSS JOIN LATERAL (
        WITH cand AS MATERIALIZED (
            SELECT t2.id, t2.name, t2.reference_count
            FROM tag t2
            WHERE t2.id <> tgt.id
              AND NOT EXISTS (SELECT 1 FROM tag_alias a WHERE a.source = t2.id)
              AND NOT EXISTS (SELECT 1 FROM tag_alias a WHERE a.target = t2.id)
              AND (t2.search_lower LIKE lower(:pfx) ESCAPE '\\'
                   OR t2.search_lower LIKE lower(:sub) ESCAPE '\\')
        )
        SELECT cand.id, cand.name, cand.reference_count
        FROM cand
        ORDER BY cand.reference_count DESC, cand.id DESC
        LIMIT :ncand
    ) c
    """
)


class SQLAlchemyTagRepository(BaseRepository):
    """Repository for tag CRUD, preferences, and aliases."""

    def __init__(self, session: Session):
        super().__init__(session)

    # -- tags ---------------------------------------------------------------

    async def get_all(self) -> Sequence[Any]:
        return list(self.session.execute(
            select(models.Tag).order_by(models.Tag.name)
        ).scalars().all())

    def _get_or_create_tag_id(self, name: str) -> int:
        """Return the tag.id for *name*, creating the tag if it doesn't exist."""
        tag = self.session.execute(
            select(models.Tag).where(models.Tag.name == name)
        ).scalar_one_or_none()
        if tag:
            return tag.id
        self.session.execute(
            pg_insert(models.Tag).values(name=name, reference_count=0)
            .on_conflict_do_nothing(index_elements=["name"])
        )
        self.session.flush()
        tag = self.session.execute(
            select(models.Tag).where(models.Tag.name == name)
        ).scalar_one()
        return tag.id

    # -- preferences --------------------------------------------------------

    async def get_preferences(self) -> Sequence[Any]:
        stmt = select(models.TagPreference).order_by(models.TagPreference.sort_index)
        return list(self.session.execute(stmt).scalars().all())

    async def create_preference(self, pref_data: dict) -> Any:
        pref = models.TagPreference(**pref_data)
        self.session.add(pref)
        self.session.flush()
        return pref

    async def update_preference(self, pref_id: int, pref_data: dict) -> Any | None:
        return self._update_attrs(models.TagPreference, pref_id, pref_data)

    async def delete_preference(self, pref_id: int) -> bool:
        return self._delete_by_id(models.TagPreference, pref_id)

    async def reorder_preferences(self, ids: list[int]) -> bool:
        return self._reorder(models.TagPreference, "sort_index", ids)

    # -- aliases ------------------------------------------------------------
    # source/target are stored as integer FKs → tag.id but exposed as tag
    # names for API compatibility.

    async def get_aliases(self) -> Sequence[dict]:
        """Return all tag aliases with source/target as tag names."""
        rows = self.session.execute(
            select(
                models.TagAlias.id,
                models.TagAlias.source,
                models.TagAlias.target,
            ).order_by(models.TagAlias.id)
        ).all()

        id_to_name = self._load_tag_names(
            {row[1] for row in rows} | {row[2] for row in rows}
        )
        return [
            {
                "id": row[0],
                "source": id_to_name.get(row[1], f"<unknown:{row[1]}>"),
                "target": id_to_name.get(row[2], f"<unknown:{row[2]}>"),
            }
            for row in rows
        ]

    def _load_tag_names(self, tag_ids: set[int]) -> dict[int, str]:
        """Return ``{tag.id: tag.name}`` for the given ids (missing → absent)."""
        if not tag_ids:
            return {}
        rows = self.session.execute(
            select(models.Tag.id, models.Tag.name).where(
                models.Tag.id.in_(tag_ids)
            )
        ).all()
        return {t[0]: t[1] for t in rows}

    def get_alias_map_sync(self) -> dict[str, str]:
        """Return {source_tag_name: target_tag_name} for all aliases (sync)."""
        rows = self.session.execute(
            select(
                models.TagAlias.source,
                models.TagAlias.target,
            )
        ).all()

        id_to_name = self._load_tag_names(
            {src for src, tgt in rows} | {tgt for src, tgt in rows}
        )
        return {
            id_to_name.get(src, str(src)): id_to_name.get(tgt, str(tgt))
            for src, tgt in rows
        }

    async def get_alias_map(self) -> dict[str, str]:
        return self.get_alias_map_sync()

    async def create_alias(self, alias_data: dict) -> dict:
        """Create a tag alias.  *alias_data* should have 'source' and 'target'
        as tag *names* — they are resolved to tag IDs internally.

        Raises:
            ValidationError: If the source tag is already aliased
                (unique constraint) — mapped to 400 instead of a raw
                IntegrityError 500.
        """
        from sqlalchemy.exc import IntegrityError

        from copixiv.core.exceptions import ValidationError

        source_name = alias_data["source"]
        target_name = alias_data["target"]

        source_id = self._get_or_create_tag_id(source_name)
        target_id = self._get_or_create_tag_id(target_name)

        alias = models.TagAlias(source=source_id, target=target_id)
        self.session.add(alias)
        try:
            # SAVEPOINT — a duplicate alias fails only this statement,
            # leaving the surrounding transaction intact.
            with self.session.begin_nested():
                self.session.flush()
        except IntegrityError as exc:
            raise ValidationError(
                f"Tag '{source_name}' is already aliased"
            ) from exc

        return {
            "id": alias.id,
            "source": source_name,
            "target": target_name,
        }

    async def delete_alias(self, alias_id: int) -> bool:
        alias = self.session.get(models.TagAlias, alias_id)
        if alias is None:
            return False
        self.session.delete(alias)
        return True

    # ------------------------------------------------------------------
    # suggest_aliases — 候选探测（查询底座：迁移 0008）
    # ------------------------------------------------------------------
    #
    # 旧实现是 N+1：先按 ``reference_count`` 分页取一批 target，再给**每个**
    # target 单独发一条
    #     name ILIKE 'X%' OR name ILIKE '%name%' ORDER BY reference_count DESC LIMIT 50
    # 实测 limit=5 → 7 条语句 / 99–125ms，limit=50 → 51 条 / 927ms，每条都是
    # ``tag`` 全表 Seq Scan + top-N Sort（``ILIKE`` 用不上 ``tag_name_key``
    # 这条 btree）。现在换成：
    #
    # * ``search_lower LIKE lower(:p) ESCAPE '\'`` —— 迁移 0008 的 STORED 生成列
    #   ``tag.search_lower = lower(name)`` + pg_bigm GIN 索引 ``tag_search_bigm``；
    #   模式一律经 :func:`escape_like` 转义（``%高中生`` 因此只匹配它自己，
    #   ``BanG_Dream!`` 不会命中 ``BangGdream!``）。两侧的大小写都由数据库的
    #   ``lower()`` 折叠（与 novels 搜索同一套理由，见 ``novels/search.py``）。
    # * 排序键固定为 ``(reference_count DESC, id DESC)`` —— 只按 reference_count
    #   排序在 39,400 个 ``rc=1`` 的并列值上不是全序，分页必然重叠/漏行。
    # * 候选按 target 用一条 ``CROSS JOIN LATERAL`` 集合语句算完（不再 N+1），
    #   且单条语句的输入 target 数有硬上限（见 :func:`_scan_limit`）。

    async def suggest_aliases(
        self, limit: int = 5, offset: int = 0, target_tag: str | None = None
    ) -> dict:
        """Suggest alias mappings by finding tags with similar names.

        排序键是 ``(reference_count DESC, id DESC)``；``id`` 这个决胜键让顺序
        成为全序（只按 ``reference_count`` 有 447 个并列值 / 其中 39,400 个
        tag 挤在 ``rc=1``）。一个 tag 的候选 = 首字符相同（忽略大小写）**或**
        名字里包含它的完整名字；两个模式都经 :func:`escape_like` 转义，由
        生成列 ``tag.search_lower`` 上的 pg_bigm GIN 索引回答（迁移 0008）。

        ``offset`` 是**游标**而不是行偏移：把上一次返回的 ``next_offset`` 原样
        传回来即可续扫。游标携带「本页最后一个被检视的 target」的 ``tag.id``
        （凑满 limit 时 = 产出第 limit 条 item 的那个 target；否则 = 扫描窗口里
        最后一个 target），下一页从 ``(reference_count, id) < 该行`` 继续；
        ``0`` 表示从头开始，游标行已不存在（被删 / ``rc`` 归零）时按「已扫完」
        收尾 —— 不从头再来，否则会重复吐出已经给过的建议。

        单次调用的工作量上限 = 检视 ``4×limit`` 个 target（下限 20、上限 200；
        API 把 limit 限制在 ≤50，所以实际不超过 200），与 ``tag`` 表大小无关。
        已在别名映射里的 tag 在 SQL 里先过滤，早于候选上限 —— 旧实现是先取
        ``LIMIT 50`` 再在 Python 里丢，会让一整页建议变空。

        ``target_tag=`` 模式只指名一个 target，没有「页」可续，所以**有意忽略
        ``offset``**，最多返回一条 item，``next_offset`` 恒为 0。
        """
        if target_tag:
            patterns = _like_patterns(target_tag)
            rows = self.session.execute(
                _TARGET_SQL,
                {
                    "name": target_tag,
                    "pfx": patterns[0],
                    "sub": patterns[1],
                    "ncand": _MAX_CANDIDATES,
                },
            ).all()
            if not rows:
                # 标签不存在 / 已在别名映射里 / 没有候选 —— 与原实现的三个空分支
                # 同形（都返回空 items + next_offset 0）。
                return {"items": [], "next_offset": 0}
            row = rows[0]
            target = _SuggestTarget(
                row.target_id, row.target_name, row.target_reference_count
            )
            candidates = [_candidate_dict(r) for r in rows]
            return {
                "items": [self._make_suggest_item(target, candidates)],
                "next_offset": 0,
            }

        if limit <= 0:
            return {"items": [], "next_offset": offset}

        cursor = int(offset or 0)
        scan = _scan_limit(limit)
        page = self.session.execute(
            _PAGE_SQL, {"cursor": cursor, "scan": scan}
        ).all()
        if not page:
            return {"items": [], "next_offset": 0}
        if cursor and not page[0].cursor_found:
            # 游标行已不在 tag 里（行被删）：没有诚实的续扫点。
            return {"items": [], "next_offset": 0}

        patterns = [_like_patterns(row.name) for row in page]
        rows = self.session.execute(
            _EXPAND_SQL,
            {
                "ids": [row.id for row in page],
                "pfx": [pfx for pfx, _ in patterns],
                "sub": [sub for _, sub in patterns],
                "ncand": _MAX_CANDIDATES,
            },
        ).all()
        candidates: dict[int, list[dict]] = {}
        for row in rows:
            candidates.setdefault(row.target_id, []).append(_candidate_dict(row))

        items: list[dict] = []
        for row in page:
            row_candidates = candidates.get(row.id)
            if not row_candidates:
                continue
            items.append(self._make_suggest_item(row, row_candidates))
            if len(items) >= limit:
                # 停在产出第 limit 条 item 的 target 上：它之后的 target 还没被
                # 消费，正好是下一页的续扫点（不重不漏）。
                return {"items": items, "next_offset": row.id}

        # 窗口内没凑满 limit：窗口满（= 还有更多行）就把游标推到窗口末尾，
        # 窗口没满说明表已扫完（0 = 结束）。
        return {
            "items": items,
            "next_offset": page[-1].id if len(page) >= scan else 0,
        }

    @staticmethod
    def _make_suggest_item(tag, candidates) -> dict:
        return {
            "target": {
                "id": tag.id,
                "name": tag.name,
                "reference_count": tag.reference_count,
            },
            "candidates": candidates,
        }

    async def apply_alias_retroactively(self, source: str, target: str) -> int:
        """Replace all occurrences of *source* tag with *target* tag on novels.

        Uses a single array operation: ``UPDATE novel SET tags =
        array_replace(tags, :source, :target) WHERE :source = ANY(tags)``.
        The statement-level ``sync_tag_refs`` trigger fires on the tags
        UPDATE and adjusts ``reference_count`` for both tags automatically
        (one aggregated delta set per statement).  Returns the number
        of novels affected.
        """
        src_tag = self.session.execute(
            select(models.Tag).where(models.Tag.name == source)
        ).scalar_one_or_none()
        if not src_tag:
            return 0
        self._get_or_create_tag_id(target)

        result = self.session.execute(
            _update(models.Novel)
            .where(models.Novel.tags.contains([source]))
            .values(tags=func.array_replace(models.Novel.tags, source, target))
        )
        return result.rowcount or 0
