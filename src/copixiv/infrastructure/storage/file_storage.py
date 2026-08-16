"""File storage — manages novel text and EPUB files on disk."""

import os
from pathlib import Path

from copixiv.domain.services.filename import build_path
from copixiv.log import logger


class FileStorage:
    """Manages novel text and EPUB files on the local filesystem."""

    def __init__(self, download_dir: str = "download"):
        self.download_dir = download_dir

    def novel_text_path(self, novel_id: int, title: str) -> Path:
        return Path(build_path(novel_id, title, self.download_dir))

    def novel_epub_path(self, novel_id: int, title: str) -> Path:
        return self.novel_text_path(novel_id, title).with_suffix(".epub")

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
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp_path.write_text(content, encoding="utf-8")
            os.replace(tmp_path, path)
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        logger.info(
            f"下载: #{novel_id} \"{title}\" → {path} ({len(content)} 字符)",
        )
        return path

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

        for suffix in (".txt", ".epub"):
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
