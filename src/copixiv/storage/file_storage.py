"""File storage — manages novel text and EPUB files on disk."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

from copixiv.core import naming as formats
from copixiv.core.models import SOURCE_LOCAL, SOURCE_PIXIV
from copixiv.log import logger
from copixiv.storage import assets
from copixiv.storage.atomic import resolve_within_root, write_text_atomic

_FOUR_DIGIT_SHARD_RE = re.compile(r"^\d{4}$")


class FileStorage:
    """Manages novel text and EPUB files on the local filesystem."""

    def __init__(self, download_dir: str = "download"):
        self.download_dir = download_dir
        self._root = Path(download_dir).resolve()

    def novel_text_path(
        self,
        novel_id: int,
        title: str,
        source: str | None = None,
        *,
        series_id: int | None = None,
        series_name: str | None = None,
        series_key: str | None = None,
        series_index: int | None = None,
        series_dir: str | Path | None = None,
    ) -> Path:
        return Path(
            formats.build_path(
                novel_id,
                title,
                self.download_dir,
                source=source,
                series_id=series_id,
                series_name=series_name,
                series_key=series_key,
                series_index=series_index,
                series_dir=series_dir,
            )
        )

    def find_existing_series_dir(
        self,
        sibling_paths: Iterable[str | None],
        source: str,
    ) -> Path | None:
        """Return an existing series directory under ``{root}/{source}/`` among sibling paths."""
        source_dir_name = formats.source_subdir(source)
        source_root = (self._root / source_dir_name).resolve()
        fallback: Path | None = None
        for raw_path in sibling_paths:
            if not raw_path:
                continue
            resolved = resolve_within_root(Path(raw_path), self._root)
            if resolved is None:
                continue
            parent = resolved.parent
            if parent.parent != source_root:
                continue
            if source_dir_name == SOURCE_PIXIV and _FOUR_DIGIT_SHARD_RE.fullmatch(parent.name):
                continue
            candidate = Path(self.download_dir) / parent.relative_to(self._root)
            if parent.is_dir():
                return candidate
            if fallback is None:
                fallback = candidate
        return fallback

    def adopt_anchor(self, anchor: str | None) -> str | None:
        """Return *anchor* when it is a usable write target, else ``None``.

        ``novel.path`` comes from the database, so it is trusted only after
        this containment check — the same rule :meth:`delete_novel_files`
        applies before unlinking.  A row whose stored path escapes the
        download root is a polluted row, not a reason to fail the download:
        returning ``None`` makes the caller fall back to a freshly computed
        path, so the novel stays fetchable while the bad value is ignored
        (and logged).

        A stored path whose basename (or sibling ``.epub``) exceeds
        ``NAME_MAX`` is also refused so the caller recomputes a fitting path.
        """
        if not anchor:
            return None
        resolved = resolve_within_root(Path(anchor), self._root)
        if resolved is None:
            logger.warning(
                "忽略 download 根之外的 path，按标题重新计算：{}",
                anchor,
            )
            return None
        if not self._fits_name_max(resolved.name):
            logger.warning(
                "stored path 超过 NAME_MAX，按标题重新计算：{}",
                anchor,
            )
            return None
        return anchor

    @staticmethod
    def _fits_name_max(basename: str, limit: int = 255) -> bool:
        """Whether ``basename`` and its sibling ``.epub`` fit *limit* bytes.

        ``NAME_MAX`` counts **bytes**, not characters — a Chinese title is
        three bytes per glyph, so a 90-character name is already ~270 bytes.
        ``atomic_write`` uses a short random temp filename, so only the final
        ``.txt`` / ``.epub`` names need to fit within *limit*.
        """
        epub_name = Path(basename).with_suffix(formats.suffix_for(formats.EPUB)).name
        worst = max(len(basename.encode("utf-8")), len(epub_name.encode("utf-8")))
        return worst <= limit

    def local_novel_path(
        self,
        novel_id: int,
        title: str,
        kind: str = formats.TEXT,
    ) -> Path:
        """``{download_dir}/local/{安全标题}_{id}{后缀}``（§3.2）。

        委托 :func:`formats.build_path` 统一处理 ``local/`` 路由与 ``NAME_MAX`` 截断。
        """
        return Path(formats.build_path(novel_id, title, self.download_dir, kind=kind, source=SOURCE_LOCAL))

    def save_novel_text(
        self,
        novel_id: int,
        title: str,
        content: str,
        anchor: str | Path | None = None,
        source: str | None = None,
        *,
        series_id: int | None = None,
        series_name: str | None = None,
        series_key: str | None = None,
        series_index: int | None = None,
        series_dir: str | Path | None = None,
    ) -> Path:
        """Write novel text to disk. Returns the file path.

        *anchor* is the file path to write to.  Callers that already know the
        row's stored ``path`` must pass it: ``novel.path`` is written once and
        then only filled when NULL, while ``build_path`` recomputes from the
        current title.  A source-side rename would otherwise store the new
        body under a new filename while every reader keeps resolving the old
        one (stale text in downloads / exports, and "did the body change?"
        comparing against a file that does not exist).  Omitted → derive from
        ``(novel_id, title)``, which is correct only for first-time writes.
        """
        path = (
            Path(anchor)
            if anchor
            else self.novel_text_path(
                novel_id,
                title,
                source=source,
                series_id=series_id,
                series_name=series_name,
                series_key=series_key,
                series_index=series_index,
                series_dir=series_dir,
            )
        )
        write_text_atomic(path, content, root=self._root)
        logger.info(
            f'下载: #{novel_id} "{title}" → {path} ({len(content)} 字符)',
        )
        return path

    def read_novel_text(
        self,
        novel_id: int,
        title: str,
        anchor: str | Path | None = None,
        source: str | None = None,
        *,
        series_id: int | None = None,
        series_name: str | None = None,
        series_key: str | None = None,
        series_index: int | None = None,
        series_dir: str | Path | None = None,
    ) -> str | None:
        """Read the stored text — ``None`` when there is no usable local copy.

        Used by the refresh path's content-change detection
        (``features/novels/download_novel.py``): missing file, unreadable
        file, or empty content all mean "nothing to compare against", and the
        caller then behaves exactly as before this check existed.

        *anchor* must match what :meth:`save_novel_text` was given, or the
        comparison reads a different file than the one being written.
        """
        path = (
            Path(anchor)
            if anchor
            else self.novel_text_path(
                novel_id,
                title,
                source=source,
                series_id=series_id,
                series_name=series_name,
                series_key=series_key,
                series_index=series_index,
                series_dir=series_dir,
            )
        )
        try:
            if not path.is_file() or path.stat().st_size == 0:
                return None
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            logger.warning(f"读取本地正文失败，按无本地副本处理: {path}")
            return None
        return content or None

    def delete_novel_files(self, novel_path: str, *, novel_id: int | None = None) -> None:
        """Delete a novel's text, EPUB, and downloaded images.

        Refuses to touch paths outside the download root — ``novel_path``
        comes from the database and is trusted only after a containment
        check (a polluted row must not delete arbitrary files).

        The **images** matter as much as the text: they are named after the
        novel id (``{id}_u_*`` / ``{id}_p_*`` / ``{id}_c_cover.*``), so a novel
        that is deleted while its pictures stay on disk leaves them behind for
        the weekly orphan sweep to reclaim.  Deleting the book should reclaim
        them now — that is the difference between "I deleted this" and "I
        deleted this, and 12 images are still sitting in the tree".
        """
        resolved = resolve_within_root(Path(novel_path), self._root)
        if resolved is None:
            logger.warning(
                "Refusing to delete files outside download dir: {}",
                novel_path,
            )
            return

        for suffix in formats.ALL_SUFFIXES:
            _unlink(resolved.with_suffix(suffix))

        nid_str = str(novel_id) if novel_id is not None else resolved.stem.rsplit("_", 1)[-1]
        if assets.is_novel_id_str(nid_str):
            for asset in assets.iter_novel_asset_files(resolved.parent, nid_str):
                _unlink(asset)

        # Remove the parent directory when this was the last novel in it.
        # Assets are gone by now, so an "empty directory" really means empty.
        self._rmdir_if_empty(resolved.parent)

    def delete_novels_files(self, novel_paths: Iterable[str | None]) -> None:
        """Batch-delete novel files, grouping asset scans per parent directory."""
        by_parent: dict[Path, list[str]] = defaultdict(list)
        for novel_path in novel_paths:
            if not novel_path:
                continue
            resolved = resolve_within_root(Path(novel_path), self._root)
            if resolved is None:
                logger.warning(
                    "Refusing to delete files outside download dir: {}",
                    novel_path,
                )
                continue
            for suffix in formats.ALL_SUFFIXES:
                _unlink(resolved.with_suffix(suffix))
            nid_str = resolved.stem.rsplit("_", 1)[-1]
            if assets.is_novel_id_str(nid_str):
                by_parent[resolved.parent].append(nid_str)
            else:
                by_parent.setdefault(resolved.parent, [])

        for parent, novel_ids in by_parent.items():
            if novel_ids:
                for asset in assets.iter_batch_novel_asset_files(parent, novel_ids):
                    _unlink(asset)
            self._rmdir_if_empty(parent)

    def _rmdir_if_empty(self, parent: Path) -> None:
        current = parent.resolve()
        while current != self._root and resolve_within_root(current, self._root) is not None:
            try:
                if any(current.iterdir()):
                    break
                current.rmdir()
            except OSError:
                break
            current = current.parent


def _unlink(path: Path) -> None:
    """Best-effort delete: a cleanup failure must not abort the others."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning(f"删除文件失败（继续清理其余文件）: {path}")
