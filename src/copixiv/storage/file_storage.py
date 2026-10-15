"""File storage — manages novel text and EPUB files on disk."""

from pathlib import Path

from copixiv.core import formats
from copixiv.core.services import build_path, safe_filename
from copixiv.log import logger
from copixiv.storage.atomic import write_text_atomic


class FileStorage:
    """Manages novel text and EPUB files on the local filesystem."""

    #: 本地书（「我的上传」）所在的子目录（docs/LIBRARY_DESIGN.md §3.2）。
    LOCAL_SUBDIR = "local"

    def __init__(self, download_dir: str = "download"):
        self.download_dir = download_dir

    def novel_text_path(self, novel_id: int, title: str) -> Path:
        return Path(build_path(novel_id, title, self.download_dir))

    def novel_epub_path(self, novel_id: int, title: str) -> Path:
        return formats.sibling_path(self.novel_text_path(novel_id, title), formats.EPUB)

    def local_novel_path(
        self, novel_id: int, title: str, kind: str = formats.TEXT,
    ) -> Path:
        """``{download_dir}/local/{安全标题}_{id}{后缀}``（§3.2）。

        本地书不走 :func:`build_path`：它按 id 分片，负数会被塞进 ``0000/``，
        而契约要的是 ``local/``。这里是那条第二约定的**唯一**所有者 ——
        上传管线只调用它，不再自己拼路径。

        标题按 ``NAME_MAX`` 截断，并把最长的临时后缀
        （``_{id}.epub.tmp``）算进余量，所以正文与 EPUB 原件两份都在限额内。
        """
        reserved = len(f"_{novel_id}{formats.SUFFIXES[formats.EPUB]}.tmp") + 1
        filename = (
            f"{safe_filename(title, max_length=254 - reserved)}"
            f"_{novel_id}{formats.suffix_for(kind)}"
        )
        return Path(self.download_dir) / self.LOCAL_SUBDIR / filename

    def save_novel_text(
        self,
        novel_id: int,
        title: str,
        content: str,
        force: bool = False,
    ) -> Path:
        """Write novel text to disk. Returns the file path."""
        path = self.novel_text_path(novel_id, title)
        if path.exists() and not force and path.stat().st_size > 0:
            logger.debug(f"下载: #{novel_id} \"{title}\" 已存在，跳过")
            return path
        write_text_atomic(path, content, root=Path(self.download_dir))
        logger.info(
            f"下载: #{novel_id} \"{title}\" → {path} ({len(content)} 字符)",
        )
        return path

    def read_novel_text(self, novel_id: int, title: str) -> str | None:
        """Read the stored text — ``None`` when there is no usable local copy.

        Used by the refresh path's content-change detection
        (``features/novels/download_novel.py``): missing file, unreadable
        file, or empty content all mean "nothing to compare against", and the
        caller then behaves exactly as before this check existed.
        """
        path = self.novel_text_path(novel_id, title)
        try:
            if not path.is_file() or path.stat().st_size == 0:
                return None
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            logger.warning(f"读取本地正文失败，按无本地副本处理: {path}")
            return None
        return content or None

    def delete_novel_files(self, novel_path: str) -> None:
        """Delete a novel's text and EPUB files, and clean up empty directories.

        Refuses to touch paths outside the download root — ``novel_path``
        comes from the database and is trusted only after a containment
        check (a polluted row must not delete arbitrary files).
        """
        root = Path(self.download_dir).resolve()
        path = Path(novel_path)
        try:
            resolved = path.resolve()
        except OSError:
            return
        if not resolved.is_relative_to(root):
            logger.warning(
                "Refusing to delete files outside download dir: {}", novel_path,
            )
            return

        for suffix in formats.ALL_SUFFIXES:
            file_path = resolved.with_suffix(suffix)
            if file_path.is_file():
                try:
                    file_path.unlink()
                except OSError:
                    pass

        # Remove parent directory if empty
        parent = resolved.parent
        try:
            remaining = list(parent.iterdir())
            if not remaining:
                parent.rmdir()
        except OSError:
            pass
