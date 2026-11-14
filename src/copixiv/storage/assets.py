"""Novel asset files on disk — the naming convention, in one place.

一个小说的正文（``{…}_{id}.txt``）旁边还会落三类图片资产，名字里带着 novel id
和一个类型字母：

======================  ==============================================
``{id}_c_cover.{ext}``  封面（``c`` = cover）
``{id}_u_{image_id}``   正文内嵌图（``u`` = uploaded image）
``{id}_p_{illust_id}``  关联插图（``p`` = linked pixiv illustration）
======================  ==============================================

**这份约定原本抄在四个地方**：下载侧拼出名字
(``storage/image_downloader.py``)、EPUB 构建侧按同样的名字回找
(``storage/epub/builder.py::_build_image_map`` / ``_find_cover``)、占位符判定的
检查任务按 glob 数它们 (``tasks/maintenance/epub.py``)、孤儿文件回收再按名字把
id 解析回来 (``tasks/maintenance/files.py``)。任何一处改一次，另外三处就悄悄
对不上——症状是"图片明明下载了，EPUB 里却是缺图框"。

所以这里只做一件事：把「名字长什么样」与「怎么从名字里认出它」写成纯函数。
放 ``storage/`` 而不是 ``core/``：这是磁盘上的产物布局，不是领域词汇。
"""

from __future__ import annotations

import re
from pathlib import Path

# 类型字母 → 含义（也是文件名里的那段）。顺序即"同一本小说里先下谁"。
COVER = "c"
UPLOADED = "u"
ILLUSTRATION = "p"

# 下载时尝试的后缀顺序。图片 URL 的后缀不一定可信（``.jpg`` 里可能是 PNG），
# 真正决定格式的是字节，所以查到哪个算哪个。
IMAGE_SUFFIXES: tuple[str, ...] = (".jpg", ".png", ".jpeg", ".gif")

# ``{novel_id}_{kind}_{asset_id}`` —— 用于反解孤儿文件。
_ASSET_STEM_RE = re.compile(r"^(?P<novel_id>\d+)_(?P<kind>[cup])_(?P<asset>.+)$")
_COVER_STEM_RE = re.compile(r"^(?P<novel_id>\d+)_c_cover$")


def asset_path(
    directory: Path, novel_id: int | str, kind: str, asset_id: str | int = "",
    suffix: str = ".jpg",
) -> Path:
    """一本小说的某个资产在 *directory* 里的路径。

    ``kind=COVER`` 时忽略 *asset_id*（封面没有编号）。
    """
    if kind == COVER:
        return directory / f"{novel_id}_c_cover{suffix}"
    return directory / f"{novel_id}_{kind}_{asset_id}{suffix}"


def asset_suffix(url: str, default: str = ".jpg") -> str:
    """图片 URL 的后缀（取不到就用 *default*）。"""
    return Path(url.split("?", 1)[0]).suffix or default


def find_cover(directory: Path, novel_id: int | str) -> Path | None:
    """已有的封面文件（按 :data:`IMAGE_SUFFIXES` 顺序找第一个存在的）。"""
    for suffix in IMAGE_SUFFIXES:
        candidate = asset_path(directory, novel_id, COVER, suffix=suffix)
        if candidate.exists():
            return candidate
    return None


def iter_assets(directory: Path, novel_id: int | str, asset_ids) -> dict[str, Path]:
    """``{asset_id: path}`` —— 把 *asset_ids* 里有盘上文件的挑出来。

    *asset_ids* 是 ``(kind, id)`` 的可迭代对象（下载侧按类型分组传进来）。
    同一 asset id 出现两次（既是内嵌图又是关联插图）时先到先得。
    """
    found: dict[str, Path] = {}
    for kind, asset_id in asset_ids:
        if asset_id in found:
            continue
        for suffix in IMAGE_SUFFIXES:
            candidate = asset_path(directory, novel_id, kind, asset_id, suffix)
            if candidate.exists():
                found[str(asset_id)] = candidate
                break
    return found


def scan_assets(directory: Path, novel_id: int | str) -> dict[str, Path]:
    """目录扫描兜底：``{asset_id: path}``，不看 manifest 声明。

    给畸形 / 手改过的语料用：OPF 里没声明、但盘上确实有图的，仍然要能进 EPUB
    （否则一本读得出来的书会渲染出一排缺图框）。
    """
    found: dict[str, Path] = {}
    prefix = f"{novel_id}_"
    for path in directory.iterdir():
        if not path.name.startswith(prefix):
            continue
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        match = _ASSET_STEM_RE.match(path.stem)
        if match and match["kind"] in (UPLOADED, ILLUSTRATION):
            found.setdefault(match["asset"], path)
    return found


def novel_id_of(stem: str) -> int | None:
    """资产文件名主干 → novel id（认不出返回 ``None``）。

    ``12345_u_678`` / ``12345_c_cover`` / ``12345_p_90`` 都认。**只**认这三类：
    正文文件名是 ``{标题}_{id}``，由 :func:`copixiv.core.naming.build_path` 那边
    反解，与这里刻意分开——两套名字的"id 在哪一段"根本不同。
    """
    match = _ASSET_STEM_RE.match(stem) or _COVER_STEM_RE.match(stem)
    return int(match["novel_id"]) if match else None


def has_illustration_assets(directory: Path, novel_id: int | str) -> bool:
    """盘上有没有这本小说的插图资产（内嵌图或关联插图，不含封面）。

    判定"图片是不是彻底没了"用的就是这个——封面不算，因为只有封面的 EPUB 不能
    证明插图曾经落过盘。
    """
    prefix = f"{novel_id}_"
    for path in directory.glob(f"{prefix}*"):
        match = _ASSET_STEM_RE.match(path.stem)
        if match and match["kind"] in (UPLOADED, ILLUSTRATION):
            return True
    return False


def iter_novel_asset_files(directory: Path, novel_id: int | str) -> list[Path]:
    """返回目录下属于 *novel_id* 的全部资产文件（封面 / 内嵌图 / 关联插图）。"""
    nid_str = str(novel_id)
    if not nid_str.isdigit():
        return []
    matched: list[Path] = []
    for path in directory.glob(f"{nid_str}_*"):
        if _ASSET_STEM_RE.match(path.stem) or _COVER_STEM_RE.match(path.stem):
            matched.append(path)
    return matched


__all__ = [
    "COVER",
    "ILLUSTRATION",
    "IMAGE_SUFFIXES",
    "UPLOADED",
    "asset_path",
    "asset_suffix",
    "find_cover",
    "has_illustration_assets",
    "iter_assets",
    "iter_novel_asset_files",
    "novel_id_of",
    "scan_assets",
]
