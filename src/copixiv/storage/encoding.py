"""Shared text-byte decoding helpers (UTF-8 / BOM UTF-16 / charset-normalizer)."""

from __future__ import annotations

import codecs
import unicodedata

from charset_normalizer import from_bytes

# ``utf-8-sig`` = UTF-8(BOM)，无 BOM 时等同 ``utf-8``。
_UTF8 = "utf-8-sig"

# ``utf-16`` 必须显式 BOM 门控：CPython 的 utf-16 解码器在缺少 BOM 时并不报错，
# 而是按本机字节序硬解，会把偶数长度二进制 / Big5 流静默吃成乱码。
_UTF16_BOMS = (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)

# 限制候选代码页为东亚常用编码：对短 Big5 样本，无隔离时可能误判成 cp949。
_CJK_CODEPAGES = ["gb18030", "big5", "cp932"]

# 可疑字符占比上限：超过即判该候选不可信（乱码宁可报错，也不静默入库）。
MAX_SUSPICIOUS_RATIO = 0.02

_SUSPICIOUS_CATEGORIES = frozenset({"Co", "Cn", "Cs", "Cf", "Cc"})
_BENIGN_CONTROL_CHARS = frozenset("\t\n\r")
_REPLACEMENT_CHAR = "\ufffd"

DECODE_ERROR = "无法识别编码（尝试 UTF-8 / GB18030 / Big5 / UTF-16）"


class TextDecodeError(ValueError):
    """所有候选编码都失败或不可信（单条 ``failed``，不写任何库表）。"""


def is_suspicious_char(char: str) -> bool:
    """该字符是否属于「解码错了」的强信号（乱码字形）。"""
    if char == _REPLACEMENT_CHAR:
        return True
    category = unicodedata.category(char)
    if category == "Cc":
        return char not in _BENIGN_CONTROL_CHARS
    return category in _SUSPICIOUS_CATEGORIES


def suspicious_ratio(text: str) -> float:
    """可疑字符占比（分母 = 正文总字符数，空串为 0.0）。"""
    if not text:
        return 0.0
    return sum(1 for char in text if is_suspicious_char(char)) / len(text)


def decode_text_bytes(
    data: bytes,
    *,
    declared_encoding: str | None = None,
    strict: bool = True,
    max_suspicious_ratio: float = MAX_SUSPICIOUS_RATIO,
) -> str:
    """Decode raw text/markup bytes to ``str``.

    Order:
      1. Caller-supplied *declared_encoding* (e.g. from XML/HTML ``charset``).
      2. ``utf-8-sig`` fast path (covers UTF-8 with/without BOM and ASCII).
      3. BOM-gated ``utf-16``.
      4. ``charset-normalizer`` scoped to East-Asian codepages (``gb18030``,
         ``big5``, ``cp932``), guarded by :func:`suspicious_ratio` when
         *strict* is True.
      5. Raise :class:`TextDecodeError` if *strict*, otherwise replace errors
         as UTF-8.
    """
    if declared_encoding:
        try:
            return data.decode(declared_encoding)
        except (UnicodeError, LookupError):
            pass

    try:
        return data.decode(_UTF8)
    except UnicodeError:
        pass

    if data.startswith(_UTF16_BOMS):
        try:
            return data.decode("utf-16")
        except UnicodeError:
            pass

    best = from_bytes(data, cp_isolation=_CJK_CODEPAGES).best()
    if best is not None:
        candidate = str(best)
        if not strict or suspicious_ratio(candidate) <= max_suspicious_ratio:
            return candidate

    if strict:
        raise TextDecodeError(DECODE_ERROR)
    return data.decode("utf-8", errors="replace")


__all__ = [
    "DECODE_ERROR",
    "MAX_SUSPICIOUS_RATIO",
    "TextDecodeError",
    "decode_text_bytes",
    "is_suspicious_char",
    "suspicious_ratio",
]
