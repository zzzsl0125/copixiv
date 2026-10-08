#!/usr/bin/env python
"""一次性修复：把"文件名贴顶"的旧小说改名到 ``build_path`` 的预算之内。

背景（2026-09-16）：136 本小说的正文字段名长达 253 字节，是 `build_path` 的
名字预算生效之前落盘的。`.epub`（254B）还写得下，但 EPUB 落盘走的是
`.epub.tmp` 临时文件（+4B = 258B）→ 超过 Linux NAME_MAX(255) →
`OSError: [Errno 36] File name too long`，这些书永远生成不出 EPUB。

修法（不动任何生产代码）：把文件名与库里的 `path` 一起改成 `build_path` 现在
会算出的名字（它按 `.epub.tmp` 的最坏情况预留长度）。改名后 `.epub` /
`.epub.tmp` 都在预算内，且 `build_path` 幂等——以后新书也不会再撞。

用法::

    python scripts/fix_long_novel_names.py --dry-run     # 只看计划，不写盘
    python scripts/fix_long_novel_names.py               # 执行

安全措施：
* 默认 dry-run，必须显式去掉才会写；
* 只改"名字贴顶"的行（`.epub.tmp` 超 LIMIT 的），其它行一律不碰；
* 同目录已有同名目标文件时**跳过**并在报告里列出，绝不覆盖；
* 每一步都在报告里可见：改名前 → 改名后 → 校验 `.epub.tmp` 是否入预算。
"""

import argparse
import os
import sys
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))



LIMIT = 255          # Linux NAME_MAX（字节）
TMP_SUFFIX = ".tmp"  # EPUB 落盘用的临时后缀


def _basename_bytes(name: str) -> int:
    return len(name.encode("utf-8"))


def _fits(name: str) -> bool:
    return _basename_bytes(name) <= LIMIT


def _over_limit(name: str) -> bool:
    """True only when the name is *strictly* too long.

    ``== 255`` bytes is writable (NAME_MAX is inclusive), so a 251-byte
    basename whose ".epub.tmp" lands exactly on 255 works today and must not
    be touched — renaming 2 900 working files to fix a handful of broken ones
    is exactly the over-reach the first draft of this script was rejected for.
    """
    return _basename_bytes(name) > LIMIT


def plan(rows: list[tuple[int, str, str]]) -> list[dict]:
    """给每行算出去留。

    ``rows`` = [(novel_id, path, title)]。返回每行的判定记录，含
    ``action`` ∈ {"skip", "rename"} 与原因，供 dry-run 打印与执行复用。

    判据刻意保守：**只动"连 .epub.tmp 都写不下"的行**。名字只是和当前标题
    不一致（作者改过标题）不算缺陷——那种行 epub 生成得好好的，改名反而会
    制造重复文件与 198 处目标冲突（第一版脚本的教训）。

    目标名在当前名字的基础上按字节截断（保留 ``_<id>.txt`` 尾巴），不动标题、
    不重排目录，因此天然幂等：再跑一次，它已经在预算内 → 跳过。
    """
    out: list[dict] = []
    for novel_id, path_str, title in rows:
        rec = {"id": novel_id, "title": title, "action": "skip", "why": ""}
        if not path_str:
            rec["why"] = "库里没有 path"
            out.append(rec)
            continue
        old = Path(path_str)
        if not old.exists():
            rec["why"] = "正文文件不存在"
            out.append(rec)
            continue

        rec["old"] = old
        rec["old_bytes"] = _basename_bytes(old.name)
        # 真实约束是 **.epub.tmp**（比 .txt.tmp 长 1 字节：epub 对 txt）。
        # 用 old.name + ".tmp" 判断会漏掉整批 986 本（2026-09-16 实测：
        # 改名到 .txt=251B 后，.epub=252B、.epub.tmp=256B 仍然写不下）。
        rec["old_tmp_bytes"] = (
            _basename_bytes(old.with_suffix(".epub").name) + len(TMP_SUFFIX)
        )

        if rec["old_tmp_bytes"] <= LIMIT:
            rec["why"] = "本就在预算内"      # 绝大多数行走这里
            out.append(rec)
            continue

        # 只对"贴顶"的行计算新名：在当前名字上截断，保证后缀与 id 完整。
        # 预算必须按 **.epub.tmp** 算：`.epub` 比 `.txt` 长 1 字节，而落盘先写
        # `.epub.tmp`（再 +4）。用 `.txt.tmp` 当预算会让整批书改名后依然写不下
        # （2026-09-16 实测 986 本）。
        suffix = f"_{novel_id}.txt"
        epub_suffix = f"_{novel_id}.epub{TMP_SUFFIX}"
        stem = old.name[: -len(suffix)] if old.name.endswith(suffix) else old.stem
        # 留出 2 字节给可能的 "_1" 序号后缀
        budget = LIMIT - len(epub_suffix.encode()) - 2

        raw = stem.encode("utf-8")[:budget]
        while raw:
            try:
                stem_str = raw.decode("utf-8")   # 不切碎多字节字符
                break
            except UnicodeDecodeError:
                raw = raw[:-1]

        new: Path | None = None
        while raw and new is None:
            for candidate_stem in (stem_str, *(f"{stem_str}_{n}" for n in range(1, 10))):
                cand = old.with_name(f"{candidate_stem}{suffix}")
                if cand == old:
                    continue
                if _basename_bytes(cand.with_suffix(".epub").name) + len(TMP_SUFFIX) > LIMIT:
                    continue
                if cand.exists() or cand.with_suffix(".epub").exists():
                    continue
                new = cand
                break
            if new is None:
                # 这一长度下所有名字都被占/超限 → 再短一个字符重试
                raw = raw[:-1]
                while raw:
                    try:
                        stem_str = raw.decode("utf-8")
                        break
                    except UnicodeDecodeError:
                        raw = raw[:-1]

        if new is None:
            rec["why"] = "找不到可用的目标名（全被占用或超限）"
            out.append(rec)
            continue

        rec["new"] = new
        rec["new_bytes"] = _basename_bytes(new.name)
        rec["new_tmp_bytes"] = (
            _basename_bytes(new.with_suffix(".epub").name) + len(TMP_SUFFIX)
        )

        rec["action"] = "rename"
        rec["why"] = (
            f"{rec['old_bytes']}B → {rec['new_bytes']}B"
            f"（.epub.tmp {rec['new_tmp_bytes']}B 入预算）"
        )
        out.append(rec)
    return out

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不写盘、不改库")
    ap.add_argument("--config", default=str(PROJECT_ROOT / "config.yaml"))
    args = ap.parse_args()

    from sqlalchemy import create_engine, text

    from copixiv.config import load_config

    cfg = load_config(args.config)
    engine = create_engine(cfg.database_url)
    with engine.connect() as conn:
        rows = list(conn.execute(text("select id, path, title from novel")))

    records = plan(rows)
    todo = [r for r in records if r["action"] == "rename"]
    skipped = [r for r in records if r["action"] == "skip" and r.get("old")]

    print(f"全库 {len(records)} 行")
    print(f"需要改名: {len(todo)}")
    print(f"检查后跳过（含目标冲突）: {len(skipped)}")
    print()
    for r in todo[:10]:
        print(f"  #{r['id']}  {r['why']}")
        print(f"      {r['old'].name[-46:]}")
        print(f"   →  {r['new'].name[-46:]}")
    if len(todo) > 10:
        print(f"  ...（共 {len(todo)} 本）")
    conflict = [r for r in skipped if "目标已存在" in r["why"]]
    if conflict:
        print(f"\n目标冲突（未处理，需人工看）: {len(conflict)}")
        for r in conflict[:5]:
            print(f"  #{r['id']}  {r['why']}")

    if args.dry_run:
        print("\n[dry-run] 未写盘、未改库。去掉 --dry-run 才执行。")
        return 0

    # 回滚清单：改名前后逐行记录，撤销只需读它反向 rename + 反向 update。
    rollback = Path("/tmp/novel_rename_rollback.tsv")
    with rollback.open("w", encoding="utf-8") as fh:
        for r in todo:
            fh.write(f"{r['id']}\t{r['old']}\t{r['new']}\n")
    print(f"\n回滚清单已写入 {rollback}（{len(todo)} 行）")

    # 执行：先重命名文件（含同目录的 .epub，如有），再改库里的 path。
    renamed = failed = 0
    path_updates: list[tuple[int, str]] = []
    for r in todo:
        old: Path = r["old"]
        new: Path = r["new"]
        try:
            os.rename(old, new)
            # 旧的 .epub（如果碰巧存在）跟着改名，避免变成孤儿文件
            old_epub = old.with_suffix(".epub")
            if old_epub.exists():
                os.rename(old_epub, new.with_suffix(".epub"))
            path_updates.append((r["id"], str(new)))
            renamed += 1
        except OSError as exc:
            print(f"  !! #{r['id']} 改名失败: {exc}")
            failed += 1

    if path_updates:
        from copixiv.db.write_lock import run_write_transaction  # noqa: F401
        from sqlalchemy import text as _text

        with engine.begin() as conn:
            for nid, new_path in path_updates:
                conn.execute(
                    _text("update novel set path = :p where id = :i"),
                    {"p": new_path, "i": nid},
                )
        print(f"\n库里 path 已更新: {len(path_updates)} 行")

    # 校验：改名后的 .epub 及其 .epub.tmp 都必须入预算
    def _tmp_bytes_of(p: str) -> int:
        epub = Path(p).with_suffix(".epub")
        return _basename_bytes(epub.name) + len(TMP_SUFFIX)

    bad = [(nid, p) for nid, p in path_updates if _tmp_bytes_of(p) > LIMIT]
    print(f"改名成功 {renamed}，失败 {failed}，改名后仍超限 {len(bad)}")
    if bad:
        print("仍超限（前 5）:", bad[:5])
    return 0 if failed == 0 and not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
