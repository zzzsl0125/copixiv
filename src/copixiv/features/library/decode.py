"""上传文件的解码 —— TXT 字节流 → 正文（委托 ``storage.encoding``）。"""

from __future__ import annotations

from copixiv.storage.encoding import (
    DECODE_ERROR,
    MAX_SUSPICIOUS_RATIO,
    TextDecodeError,
    decode_text_bytes,
    is_suspicious_char,
    suspicious_ratio,
)


def decode_txt(data: bytes) -> str:
    """按契约 §4-1 解码；全部失败或不可信时抛 :class:`TextDecodeError`."""
    return decode_text_bytes(data, strict=True)


__all__ = [
    "DECODE_ERROR",
    "MAX_SUSPICIOUS_RATIO",
    "TextDecodeError",
    "decode_txt",
    "is_suspicious_char",
    "suspicious_ratio",
]
