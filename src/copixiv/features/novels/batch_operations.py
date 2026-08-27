"""Use cases: batch delete and batch tag operations on novel scopes."""

from __future__ import annotations

from copixiv.core.exceptions import NotFoundError, ValidationError
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
from copixiv.features.tags.repo import SQLAlchemyTagRepository
from copixiv.storage.file_storage import FileStorage
from copixiv.core.services import parse_search_keyword
from copixiv.core.services import QuerySpec

# Hard safety cap per SYNCHRONOUS batch operation — one HTTP request must
# never walk the whole library.  The frontend prompts the user to narrow
# the scope when the matched set is large; selections beyond this run as
# background tasks (POST /api/novels/batch-task).
BATCH_MAX_NOVELS = 5000
BATCH_MAX_TAGS = 20

# Internal chunk size for statements that address an explicit ID list
# (match-ids intersection, blocked-ids, sort-ids, task safety fallback).
# 30k sits safely under the LOWEST mainstream SQLite variable limit
# (32766 — SQLite 3.32~3.46 default), so chunked IN-lists work on any
# environment instead of only on 250k-limit builds.  More chunks cost
# a few extra index scans — negligible next to the row work itself.
BATCH_ID_CHUNK_SIZE = 30_000


# Sentinel: "cap not given" → resolve the module-level BATCH_MAX_NOVELS at
# call time (monkeypatch-friendly), while an explicit ``cap=None`` means
# truly uncapped (background-task path).
_DEFAULT_CAP = object()


async def resolve_batch_scope(
    novel_repo: SQLAlchemyNovelRepository,
    *,
    mode: str,
    novel_ids: list[int],
    keyword: str | None,
    min_like: int | None,
    min_text: int | None,
    excluded_ids: list[int],
    cap: int | None | object = _DEFAULT_CAP,
) -> list[int]:
    """Resolve the effective novel ID list for a batch operation.

    The single scope-resolution rule, shared by the synchronous batch
    endpoint (capped at :data:`BATCH_MAX_NOVELS`) and the background-task
    enqueue path (``cap=None`` — any size, the task chunks the work).

    Args:
        cap: Maximum matched/selected size; ``None`` disables the check
            (background tasks accept selections of any size).  Defaults to
            the current :data:`BATCH_MAX_NOVELS`.

    Raises:
        ValidationError: Empty scope, or scope larger than *cap*.
        NotFoundError: Filter-matched scope contains no novels.
    """
    if cap is _DEFAULT_CAP:
        cap = BATCH_MAX_NOVELS
    if mode == "ids":
        ids = sorted({int(i) for i in novel_ids})
        if not ids:
            raise ValidationError("请先勾选要操作的小说")
        if cap is not None and len(ids) > cap:
            raise ValidationError(
                f"已勾选 {len(ids)} 篇，单次批量操作上限为 "
                f"{cap} 篇，请减少勾选数量"
            )
        return ids

    conditions = parse_search_keyword(keyword) if keyword else None
    # Only the capped path (sync endpoint) needs the COUNT — the
    # background-task path (cap=None) skips it and goes straight to the
    # ID list.
    if cap is not None:
        matched = await novel_repo.count_novels(
            QuerySpec(
                conditions=conditions or [],
                min_like=min_like,
                min_text=min_text,
            )
        )
        if matched > cap:
            raise ValidationError(
                f"当前筛选匹配 {matched} 篇，超过单次批量操作上限 "
                f"{cap} 篇，请先缩小筛选范围"
            )

    ids = await novel_repo.list_matching_ids(
        QuerySpec(
            conditions=conditions or [],
            min_like=min_like,
            min_text=min_text,
            exclude_ids=excluded_ids or [],
        )
    )
    if not ids:
        raise NotFoundError("当前范围内没有可操作的小说")
    return ids


class BatchDeleteUseCase:
    """Delete all novels in a resolved ID list, then clean up their files.

    DB first, files second — the same order as :class:`DeleteNovelUseCase`:
    a failed DB delete must not leave rows pointing at deleted files.
    File cleanup stays best-effort so a disk hiccup cannot fail the API
    call after the rows are already gone.
    """

    def __init__(self, novel_repo: SQLAlchemyNovelRepository, file_storage: FileStorage):
        self._repo = novel_repo
        self._file_storage = file_storage

    async def execute(self, novel_ids: list[int]) -> int:
        paths = await self._repo.delete_many(novel_ids)
        for path in paths:
            if path:
                self._file_storage.delete_novel_files(path)
        return len(novel_ids)


class BatchTagUseCase:
    """Add or remove tags on all novels in a resolved ID list.

    Tag names are normalized (stripped, deduped) and resolved through the
    alias map so a batch add behaves exactly like the write path.
    """

    def __init__(self, novel_repo: SQLAlchemyNovelRepository, tag_repo: SQLAlchemyTagRepository):
        self._repo = novel_repo
        self._tag_repo = tag_repo

    async def execute(
        self, operation: str, novel_ids: list[int], tags: list[str],
    ) -> int:
        tag_set = {t.strip() for t in tags if t and t.strip()}
        if not tag_set:
            raise ValidationError("请至少输入一个标签")
        if len(tag_set) > BATCH_MAX_TAGS:
            raise ValidationError(
                f"一次最多操作 {BATCH_MAX_TAGS} 个标签（当前 {len(tag_set)} 个）"
            )

        alias_map = await self._tag_repo.get_alias_map()
        resolved = {alias_map.get(t, t) for t in tag_set}

        if operation == "add_tags":
            return await self._repo.add_tags_to_novels(novel_ids, resolved)
        if operation == "remove_tags":
            return await self._repo.remove_tags_from_novels(novel_ids, resolved)
        raise ValidationError(f"未知的批量操作: {operation}")
