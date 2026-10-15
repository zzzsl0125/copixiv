"""原子写 —— "写 ``.tmp`` → ``os.replace``" 的唯一实现处。

收编了原先散在四处的同一套动作：``storage/file_storage.py``、
``storage/epub/builder.py``、``storage/image_downloader.py``、
``features/library/ingest.py``。它们的差别只在"字节从哪来"（直接给、
ebooklib 写、流式下载），"怎么安全落盘"应该逐字一致 ——
``tests/regression/test_m4_m5_atomic.py`` 钉的就是这条纪律：崩溃可以，但
绝不能把截断的文件留在最终路径上被当成完整文件。

给 ``root`` 时拒绝写到 root 之外：``path`` 可能来自数据库（``novel.path``），
一个被污染的字段不该让写路径变成任意文件写入口。
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def _tmp_path(path: Path) -> Path:
    """临时文件路径 —— 后缀追加 ``.tmp``。

    命名跟着最终文件名走，所以各调用方对 ``NAME_MAX`` 的预算（把 ``.tmp``
    算进去）继续成立，不必再各算一遍。
    """
    return path.with_suffix(path.suffix + ".tmp")


@contextmanager
def atomic_write(path: Path, *, root: Path | None = None) -> Iterator[Path]:
    """在 ``path`` 同级开一个 ``.tmp`` 文件交给调用方写，正常退出即替换。

    用法（流式、库写、直接写字节都适用）::

        with atomic_write(target) as tmp:
            tmp.write_bytes(data)

    异常退出（含 ``KeyboardInterrupt``）会删掉临时文件后**原样重抛** ——
    调用方该走自己的错误分支就走，不必再操心清理。``root`` 是
    download 目录的越界防线，见模块 docstring。
    """
    parent = path.parent
    if root is not None and not parent.resolve().is_relative_to(root.resolve()):
        raise OSError(f"路径落在 download 根之外：{path}")
    parent.mkdir(parents=True, exist_ok=True)

    tmp_path = _tmp_path(path)
    try:
        yield tmp_path
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def write_atomic(path: Path, data: bytes, *, root: Path | None = None) -> None:
    """:func:`atomic_write` 的字节便利壳（最常见的用法）。"""
    with atomic_write(path, root=root) as tmp_path:
        tmp_path.write_bytes(data)


def write_text_atomic(path: Path, text: str, *, root: Path | None = None) -> None:
    """UTF-8 文本的原子写。"""
    write_atomic(path, text.encode("utf-8"), root=root)
