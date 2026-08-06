"""只读排查 2:没有 failed_novel 记录的 PENDING 小说,卡在哪一步?

思路:图片下载和 EPUB 生成是 fire-and-forget(失败被吞),但会留下
磁盘痕迹 —— 下载成功的图会以 {id}_u_{img_id}.jpg 命名留在磁盘。
所以对比「正文占位符数量」和「磁盘图片文件数量」就能推断:

  - 占位符有、图片文件 0  → 下载阶段就失败(URL 失效/网络/限流)
  - 占位符有、图片文件有 → 下载成功,但 EPUB 构建阶段失败
"""

import re
import sqlite3
from collections import Counter
from pathlib import Path

DB = "database/database.db"
conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)

_PLACEHOLDER = re.compile(r"\[(uploadedimage|pixivimage):([\d\-]+)\]")

# 1) 全部 PENDING + 各自是否有 failed_novel 记录
pending = conn.execute(
    "SELECT n.id, n.path, n.create_time, "
    "(f.novel_id IS NOT NULL) AS has_fail "
    "FROM novel n LEFT JOIN failed_novel f ON f.novel_id = n.id "
    "WHERE n.has_epub = 1 ORDER BY n.create_time"
).fetchall()

with_fail = [r for r in pending if r[3]]
no_fail = [r for r in pending if not r[3]]
print(f"PENDING 总数 {len(pending)} | 有失败记录 {len(with_fail)} | 无记录 {len(no_fail)}")

# 2) 无记录组的正文占位符 vs 磁盘图片文件
print("\n无失败记录的篇目,按「磁盘图片文件数」分类:")
stats = Counter()
detail = []
for nid, path_str, ctime, _ in no_fail:
    txt_path = Path(path_str) if path_str else None
    placeholders = 0
    if txt_path and txt_path.exists():
        text = txt_path.read_text(encoding="utf-8", errors="ignore")
        placeholders = len(_PLACEHOLDER.findall(text))

    img_files = 0
    parent = txt_path.parent if txt_path else None
    if parent:
        img_files = len(list(parent.glob(f"{nid}_u_*"))) + \
                    len(list(parent.glob(f"{nid}_p_*"))) + \
                    len(list(parent.glob(f"{nid}_c_cover*")))

    bucket = ("有图文件" if img_files else "无图文件(下载失败?)")
    stats[bucket] += 1
    detail.append((nid, ctime[:10], placeholders, img_files, bucket))

for bucket, n in stats.most_common():
    print(f"   {bucket}: {n} 篇")

# 3) 无图文件组的占位符统计(确认它们确实需要图片)
print("\n「无图文件」组的正文占位符分布:")
no_img = [d for d in detail if d[4] == "无图文件(下载失败?)"]
ph_counts = Counter(d[2] for d in no_img)
print(f"   占位符=0 的: {ph_counts.get(0, 0)} 篇 (正文没图却标了 PENDING?)")
print(f"   占位符=1 的: {ph_counts.get(1, 0)} 篇")
print(f"   占位符>=2 的: {sum(v for k, v in ph_counts.items() if k >= 2)} 篇")

# 4) 「有图文件但 EPUB 没生成」的抽样
print("\n「有图文件」组的样本(前 5 篇):")
with_img = [d for d in detail if d[4] == "有图文件"]
for nid, ctime, ph, nimg, _ in with_img[:5]:
    print(f"   #{nid} ({ctime}) 占位符={ph} 图片文件={nimg}")

# 5) 时间分布:两组的入库时间是不是同一批
print("\n无记录组 vs 有记录组 的入库月份分布:")
def month_dist(rows):
    return Counter((r[2] or "")[:7] for r in rows)
print("   无记录组:", dict(sorted(month_dist(no_fail).items())))
print("   有记录组:", dict(sorted(month_dist(with_fail).items())))
