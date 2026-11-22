"""Use cases: batch delete and batch tag operations on novel scopes."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from copixiv.core.exceptions import NotFoundError, ValidationError
from copixiv.core.search import QuerySpec, parse_search_keyword
from copixiv.core.text import MAX_TAG_LENGTH, validate_tags
from copixiv.db.tx import run_write_transaction_sync
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.novels.repo import SQLAlchemyNovelRepository
from copixiv.features.tags.repo import SQLAlchemyTagRepository
from copixiv.log import logger
from copixiv.storage.file_storage import FileStorage

BATCH_MAX_TAGS = 20

# Internal chunk size for statements/transactions addressing explicit ID lists
# (match-ids intersection, blocked-ids, batch delete/tag chunks).
BATCH_ID_CHUNK_SIZE = 30_000


def resolve_batch_scope(
    novel_repo: SQLAlchemyNovelRepository,
    *,
    mode: str,
    novel_ids: list[int],
    keyword: str | None,
    min_like: int | None,
    min_text: int | None,
    excluded_ids: list[int],
) -> list[int]:
    """Resolve the effective novel ID list for a batch operation.

    Raises:
        ValidationError: Empty ``ids`` selection.
        NotFoundError: Filter-matched scope contains no novels.
    """
    if mode == "ids":
        ids = sorted({int(i) for i in novel_ids})
        if not ids:
            raise ValidationError("请先勾选要操作的小说")
        return ids

    conditions = parse_search_keyword(keyword) if keyword else None
    ids = novel_repo.list_matching_ids(
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


def delete_novel_paths(file_storage: FileStorage, paths: Sequence[str | None]) -> None:
    """Best-effort delete novel files on disk."""
    for path in paths:
        if path:
            try:
                file_storage.delete_novel_files(path)
            except Exception:  # noqa: BLE001
                logger.exception("删除小说文件失败: {}", path)


def batch_delete(
    novel_ids: list[int],
    *,
    uow: SqlUnitOfWork,
    file_storage: FileStorage,
    defer_file_cleanup: Callable[[list[str]], None] | None = None,
) -> int:
    """Delete all novels in a resolved ID list, then clean up their files.

    DB first, files second — the same order as :func:`~copixiv.features.novels.use_cases.delete_novel.delete_novel`:
    a failed DB delete must not leave rows pointing at deleted files.
    File cleanup stays best-effort so a disk hiccup cannot fail the API
    call after the rows are already gone.
    """
    deleted_count = 0
    all_paths: list[str] = []
    for i in range(0, len(novel_ids), BATCH_ID_CHUNK_SIZE):
        chunk = novel_ids[i : i + BATCH_ID_CHUNK_SIZE]

        def _delete_chunk(tx: SqlUnitOfWork, chunk: list[int] = chunk) -> tuple[int, list[str]]:
            return SQLAlchemyNovelRepository(tx.session).delete_many(chunk)

        count, paths = run_write_transaction_sync(uow, _delete_chunk)
        deleted_count += count
        all_paths.extend(paths)

    if defer_file_cleanup is not None and len(all_paths) > 500:
        defer_file_cleanup(all_paths)
    else:
        delete_novel_paths(file_storage, all_paths)
    return deleted_count


def batch_tag(
    operation: str,
    novel_ids: list[int],
    tags: list[str],
    *,
    uow: SqlUnitOfWork,
) -> int:
    """Add or remove tags on all novels in a resolved ID list.

    Tag names are normalized (stripped, deduped) and resolved through the
    alias map so a batch add behaves exactly like the write path.
    """
    if operation not in ("add_tags", "remove_tags"):
        raise ValidationError(f"未知的批量操作: {operation}")
    cleaned_tags = validate_tags(
        list(tags),
        max_count=BATCH_MAX_TAGS,
        max_length=MAX_TAG_LENGTH,
        require_non_empty=True,
    )
    tag_set = set(cleaned_tags)

    affected = 0
    for i in range(0, len(novel_ids), BATCH_ID_CHUNK_SIZE):
        chunk = novel_ids[i : i + BATCH_ID_CHUNK_SIZE]

        def _tag_chunk(tx: SqlUnitOfWork, chunk: list[int] = chunk) -> int:
            novel_repo = SQLAlchemyNovelRepository(tx.session)
            tag_repo = SQLAlchemyTagRepository(tx.session)
            alias_map = tag_repo.get_alias_map()
            resolved = {alias_map.get(t, t) for t in tag_set}
            if operation == "add_tags":
                return novel_repo.add_tags_to_novels(chunk, resolved)
            return novel_repo.remove_tags_from_novels(chunk, resolved)

        affected += run_write_transaction_sync(uow, _tag_chunk)
    return affected
