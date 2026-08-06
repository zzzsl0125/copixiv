"""只读诊断:库内 has_epub=1 的「做不出 EPUB」的小说到底怎么回事

只读模式连接 database/database.db,绝不做任何修改。
回答三个问题:
  1. has_epub 状态分布如何?
  2. PENDING(1)的篇目是什么时候入库的(存量还是集中爆发)?
  3. 它们的 txt 文件还在吗?failed_novel 表里有失败记录吗?
"""

import sqlite3
from collections import Counter
from pathlib import Path

DB = "database/database.db"
conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

print("=" * 60)
print("1) has_epub 状态分布")
for row in conn.execute("SELECT has_epub, COUNT(*) n FROM novel GROUP BY has_epub"):
    name = {0: "NO(0) 无图", 1: "PENDING(1) 待制作", 2: "DONE(2) 已完成"}[row["has_epub"]]
    print(f"   {name}: {row['n']} 篇")

print("\n2) PENDING 篇目按入库日期分布(前 15 天)")
rows = conn.execute(
    "SELECT substr(create_time, 1, 10) d, COUNT(*) n FROM novel "
    "WHERE has_epub=1 GROUP BY d ORDER BY d DESC LIMIT 15"
).fetchall()
for r in rows:
    print(f"   {r['d']}: {r['n']} 篇")

print("\n3) PENDING 篇目的 txt 文件是否还在(抽样 300 篇)")
pending = conn.execute(
    "SELECT id, path FROM novel WHERE has_epub=1 LIMIT 300"
).fetchall()
txt_exists = 0
epub_exists = 0
missing_path = 0
for r in pending:
    if not r["path"]:
        missing_path += 1
        continue
    p = Path(r["path"])
    if p.exists():
        txt_exists += 1
    if p.with_suffix(".epub").exists():
        epub_exists += 1
print(f"   样本 {len(pending)} 篇: txt 在盘 {txt_exists} | epub 在盘 {epub_exists} "
      f"| path 为空 {missing_path}")

print("\n4) failed_novel 表:这些小说有没有被记录过下载失败")
fail_rows = conn.execute("SELECT novel_id, failure_type, error_message, failed_times "
                         "FROM failed_novel LIMIT 5").fetchall()
print(f"   failed_novel 总行数: {conn.execute('SELECT COUNT(*) FROM failed_novel').fetchone()[0]}")
for r in fail_rows:
    print(f"   #{r['novel_id']} [{r['failure_type']}] x{r['failed_times']}: "
          f"{(r['error_message'] or '')[:80]}")

print("\n5) 有失败记录的 PENDING 小说数量")
n = conn.execute(
    "SELECT COUNT(DISTINCT f.novel_id) FROM failed_novel f "
    "JOIN novel n ON n.id = f.novel_id WHERE n.has_epub=1"
).fetchone()[0]
print(f"   {n} 篇 PENDING 小说在 failed_novel 里有记录")

print("\n6) PENDING 中已超过 7 天的(排除『刚入库还没生成完』的良性情况)")
n7 = conn.execute(
    "SELECT COUNT(*) FROM novel WHERE has_epub=1 "
    "AND create_time < datetime('now', '-7 days')"
).fetchone()[0]
print(f"   入库超过 7 天仍是 PENDING 的: {n7} 篇")
