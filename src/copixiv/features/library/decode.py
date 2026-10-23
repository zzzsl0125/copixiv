"""上传文件的解码 —— TXT 字节流 → 正文，含「乱码宁可报错」的评分择一。

契约见 ``docs/LIBRARY_DESIGN.md`` §4-1。这个模块只有纯函数：给定字节流返回
文本，或抛出 :class:`TextDecodeError`；它不碰磁盘、不碰数据库、不知道
"上传"这件事，所以 :mod:`copixiv.features.library.ingest` 之外的入口（将来的
监控目录导入、命令行导入）可以直接复用。

**评分择一是本模块存在的理由。** GB18030 与 Big5 的字节结构高度重叠，任一方
排在前面都会把另一方**静默**解成乱码 —— 静默的数据损坏比报错严重得多
（用户会得到一本"能搜到但正文是鬼画符"的书）。所以两个候选各自解一遍，比较
"可疑字符占比"，谁低谁赢；都不可信就整条失败。
"""

from __future__ import annotations

import codecs
import unicodedata

# ``utf-8-sig`` = UTF-8(BOM)，无 BOM 时等同 ``utf-8``。
_UTF8 = "utf-8-sig"

# ``utf-16`` **必须显式 BOM 门控**：CPython 的 utf-16 解码器在缺少 BOM 时并不
# 报错，而是按本机字节序硬解（实测 ``b"ab".decode("utf-16") == "扡"``，
# ``b"\x80\x81"*8`` → ``"膀膀…"``）。不门控的话，任何偶数长度的二进制 / Big5
# 字节流都会在评分之前被静默吃成乱码。
_UTF16_BOMS = (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)

# 字节结构重叠、必须评分择一的候选编码（顺序 = 并列时的偏好）。
_SCORED_ENCODINGS = ("gb18030", "big5")

# 可疑字符占比上限：超过即判该候选不可信（乱码宁可报错，也不静默入库）。
MAX_SUSPICIOUS_RATIO = 0.02

# 可疑字符 = 私有区 Co / 未分配 Cn / 代理 Cs / 格式 Cf / 控制 Cc（\t \n \r 除外）
# + 替换符 U+FFFD（它的 Unicode 类别是 So，必须单独判）。
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


def _scored_decode(data: bytes, encodings) -> tuple[str, float] | None:
    """在字节结构重叠的候选编码里挑「可疑字符最少」的那个。

    并列时取先者（``<`` 而非 ``<=``）—— 真 GBK/GB18030 文件在两个解码器下
    都可能零可疑字符（Big5 只是解成另一串「像中文的」错字），此时行为必须
    可预期地落在 ``gb18030``（列表第一个）。
    """
    best: tuple[str, float] | None = None
    for encoding in encodings:
        try:
            candidate = data.decode(encoding)
        except UnicodeError:
            continue
        ratio = suspicious_ratio(candidate)
        if best is None or ratio < best[1]:
            best = (candidate, ratio)
    return best


def decode_txt(data: bytes) -> str:
    """按契约 §4-1 解码；全部失败或不可信时抛 :class:`TextDecodeError`。

    1. ``utf-8-sig`` —— UTF-8（含 BOM）；
    2. ``utf-16`` —— **仅在带 BOM（FF FE / FE FF）时尝试**。CPython 的 utf-16
       解码器无 BOM 时按本机字节序硬解而不报错，所以「门控」必须自己写，
       否则偶数长度的二进制流会在这里被静默吃成乱码；
    3. ``gb18030`` / ``big5`` —— 评分择一：解出来的「可疑字符占比」更小者胜，
       并列取 ``gb18030``；最佳占比仍 > :data:`MAX_SUSPICIOUS_RATIO` 判失败。

    为什么第 3 步不能先到先得：GB18030 几乎不会抛错，排在 Big5 前面会把
    Big5 正文**静默**解成乱码（静默数据损坏比报错严重）。评分让两种编码各
    自证明自己，乱码一侧的私有区/未分配字符会把占比顶上去。

    已知边界（无新依赖前提下的固有缺口）：字节结构在 GB18030 下完全合法、
    解出来又「都是常用汉字」的异种编码（如部分 Shift-JIS 文本）仍会被当成
    GB18030 收下 —— 评分抓不住零可疑字符的错字，这需要真正的编码探测器。
    见 ``docs/LIBRARY_DESIGN.md`` §8.1。
    """
    try:
        return data.decode(_UTF8)
    except UnicodeError:
        pass

    if data.startswith(_UTF16_BOMS):
        try:
            return data.decode("utf-16")
        except UnicodeError:
            pass

    best = _scored_decode(data, _SCORED_ENCODINGS)
    if best is None or best[1] > MAX_SUSPICIOUS_RATIO:
        raise TextDecodeError(DECODE_ERROR)
    return best[0]


__all__ = [
    "DECODE_ERROR",
    "MAX_SUSPICIOUS_RATIO",
    "TextDecodeError",
    "decode_txt",
    "is_suspicious_char",
    "suspicious_ratio",
]
