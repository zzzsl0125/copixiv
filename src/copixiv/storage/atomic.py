"""原子写 —— "同目录随机短临时名 → ``os.replace``" 的唯一实现处。"""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path


def _tmp_path(path: Path) -> Path:
    """同目录随机短临时路径（29 字节），兼容 255 字节长目标名与并发写。"""
    return path.parent / f".copixiv-{secrets.token_hex(8)}.tmp"


@contextmanager
def atomic_write(path: Path, *, root: Path | None = None) -> Iterator[Path]:
    """在 ``path`` 同级开一个随机短 ``.tmp`` 文件交给调用方写，正常退出即替换。

    异常退出（含 ``KeyboardInterrupt``）会删掉临时文件后原样重抛。
    ``root`` 为目录越界防线：拒绝写到 ``root`` 本身或其目录之外。
    """
    if root is not None:
        resolved_target = path.resolve()
        resolved_root = root.resolve()
        if resolved_target == resolved_root or not resolved_target.is_relative_to(resolved_root):
            raise OSError(f"路径落在 download 根之外：{path}")

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = _tmp_path(path)
    try:
        yield tmp_path
        os.replace(tmp_path, path)
    except BaseException:
        with suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise


def write_atomic(path: Path, data: bytes, *, root: Path | None = None) -> None:
    """:func:`atomic_write` 的字节便利壳。"""
    with atomic_write(path, root=root) as tmp_path:
        tmp_path.write_bytes(data)


def write_text_atomic(path: Path, text: str, *, root: Path | None = None) -> None:
    """UTF-8 文本的原子写。"""
    write_atomic(path, text.encode("utf-8"), root=root)
