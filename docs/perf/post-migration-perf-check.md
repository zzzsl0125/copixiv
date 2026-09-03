# copixiv PostgreSQL 迁移后性能核查报告（搜索 / 翻页 / 冷启动）

- **日期**：2026-09-05（实测）
- **范围**：SQLite → PostgreSQL 迁移完成后，对「小说列表 / 搜索 / 计数 / 翻页 / 冷启动」做系统性实测，并与迁移前基线对比。
- **工具**：`scripts/bench_pg_query_performance.py`（check / bench / cold / explain 四模式，均可复现）。
- **数据库**：`postgresql+psycopg2://postgres@127.0.0.1:5433/copixiv`（scripts/pg_dev.py 管理的 pgserver PG16，数据目录 `.spike/pgdata`）。
- **数据量**：novel **236,983** / novel_search 236,983 / tag 73,822 / author 8,767；blocked tags 4 个（`BL`、`gay`、`男同`、`纯男性`，命中 **8,071** 行）；`is_favourite` 71、special-follow 64。
- **口径**：走真实 repo 路径（`_get_novels_sync / _count_novels_sync`），包含每次请求的 Setting / TagPreference 小查询与 blocked 排除；count 场景每样本清空进程内 count 缓存（测首击）。冷启动 = `pg_ctl stop → start` 后用全新 engine 首查。

---

## 1. 结论摘要（TL;DR）

| 维度 | 迁移前 SQLite | 本次实测 PG（真实库 + blocked 排除） | 判定 |
|---|---:|---:|---|
| 默认列表（首页） | ~0.1ms（SQL 口径） | 3.2ms（repo 全路径） / SQL 0.46ms | ✅ 可接受（绝对开销小） |
| 随机浏览 | ~0.1ms | 8–28ms（首页波动，多查询路径） | ⚠ 可接受但波动 |
| keyset 深翻页第 1000 页 | — | 2.8ms（repo） / SQL 0.19ms | ✅ 无深翻页退化 |
| 热 tag 列表（R-18） | ~0.1ms（自适应 EXISTS） | 3.3ms | ✅ |
| 热 tag 计数（R-18） | 232ms（IN）/FTS5 8.7ms | **100ms**（blocked 排除把 29ms 拖到 100ms） | ⚠ 首击偏慢 |
| keyword 列表（恋，20k 命中） | 17.8ms（TS 词） | **27.7ms** | ⚠ 中频词偏慢 |
| keyword 列表（R-18，94% 命中） | ~188ms | 5.5ms | ✅ 早停有效 |
| keyword 计数（R-18） | 8.7ms（FTS5） | **2.46s 首击**；缓存命中 **0.8ms** | ⚠ 首击慢；写后自动重算已落地（P0） |
| keyword 计数（催眠） | — | **0.61s 首击** | ❌ 同上 |
| 冷启动首请求（默认列表） | SQLite mmap+预热 | **58.5ms vs warm 3.0ms（19.7×）** | ⚠ 无预热 |
| 标签建议 suggest_aliases | — | **160ms**（每次击键） | ⚠ 可感知慢 |

**总体判定**：翻页与列表型查询在 PG 上健康（keyset 第 1000 页 2.8ms）；**本次会话已落地两处缓解**：冷启动预热 + 调参（P3）、写后后台重算 count 缓存（P0 精简版）。剩余可感知成本：① keyword 计数首击 2.5s（写提交后 ~6s 窗口内 + 进程重启后首次）；② 中频 keyword 列表 20–40ms；③ blocked 排除使计数 ~29ms→100ms（首击）。三者经单用户场景评估均保留现状（理由见 §5 P1/P2）。

---

## 2. 环境核查（check 输出摘要）

- **PG 运行时**：`shared_buffers=128MB`（默认，未按 spike 建议调优）、`work_mem=4MB`、`effective_cache_size=4GB`、`max_parallel_workers_per_gather=2`。
- **索引**：novel 12 个 + `ix_novel_tags_gin`（text[] GIN）+ `novel_search_gin`（to_tsvector 表达式 GIN）全部在位；`novel_search` 与 `novel` 行数一致（无漂移）。
- **ANALYZE**：novel / tag 有近期 autoanalyze；stats 可用。
- **GIN 使用**：`ix_novel_tags_gin` 29 次扫描、`novel_search_gin` 41 次扫描——索引在服役。
- **命中规模**（解释性能的关键）：keyword「恋」20,446 行、「R-18」223,740 行（94%）、「催眠」15,192 行、「TS」13,267 行、「哈利波特」68 行；tag「NTR」34,885 行、「R-18」218,272 行；默认阈值（like≥500/text≥3000，排除 blocked）**60,539** 行。

---

## 3. 实测结果

### 3.1 列表查询（warm，n=5，min/median ms）

| 场景 | min | median | 说明 |
|---|---:|---:|---|
| 默认(500/3000)+like（含 blocked 排除） | 3.2 | 3.3 | SQL 本身 0.46ms，余量为 repo 附加查询+ORM |
| 默认+关闭 blocked 排除 | 2.6 | 2.7 | blocked 排除对列表的增量小（<1ms） |
| 随机浏览(500/3000) | 8.0 | 28.3 | max(shuffle)+随机起点+author 旗标查询，波动大 |
| ID 排序(无阈值) | 8.8 | 9.4 | |
| 标签 NTR | 4.5 | 6.8 | 34,885 命中 |
| 标签 R-18 | 3.3 | 3.4 | 218,272 命中，like 索引早停 |
| 标签稀有（伊理戸結女，1 行） | 2.1 | 2.2 | ✅ GIN 精确命中 |
| 关键词恋 | 27.7 | 28.3 | ⚠ 见 §4.2 |
| 关键词 R-18 | 5.5 | 5.6 | ✅ 94% 覆盖早停 |
| 关键词哈利波特（68 命中） | 9.2 | 9.4 | 与恋同形态，命中越稀越靠分段 |
| 作者 100770156 + id | 2.6 | 2.8 | |
| 系列 11250666 + id | 2.8 | 3.0 | |
| 收藏 + id | 3.2 | 3.5 | |
| 特别关注 + id | 10.3 | 10.9 | author 半连接 |
| 组合 tagNTR+关键词恋 | 36.6 | 37.8 | 两个过滤叠加 |
| 组合 tagNTR+关键词R-18 | 5.7 | 5.7 | R-18 早停兜底 |

### 3.2 计数查询（warm，绕过缓存首击，n=5）

| 场景 | min | median | 对比参考 |
|---|---:|---:|---|
| 默认阈值(500/3000) | 95.3 | 95.6 | greenfield 无 blocked 时 ~29ms |
| 标签 NTR | 82.9 | 106.4 | |
| 标签 R-18 | 100.3 | 101.8 | 有 blocked 时 Parallel Seq Scan（见 §4.1） |
| 关键词恋 | 111.0 | 111.6 | 单字词走 GIN 后逐行 probe，仍可接受 |
| 关键词 R-18 | 2459.1 | 2460.3 | ❌ Hash Join 全物化（见 §4.3） |
| 关键词催眠 | 609.0 | 621.1 | ❌ Bitmap 15k 行逐行 probe |
| 关键词哈利波特 | 7.2 | 7.4 | 稀有词 GIN 快 |
| 特别关注 | 8.1 | 8.2 | |
| 组合 tagNTR+关键词恋 | 82.3 | 83.2 | |
| 被排除（blocked） | 5.6 | 5.8 | blocked 命中 8,071 行，GIN 精准 |
| **缓存命中对照（关键词恋）** | 0.7 | 0.8 | ✅ epoch 缓存兜底有效（但只覆盖读多写少的窗口） |

### 3.3 翻页（keyset，warm）

| 场景 | min | median |
|---|---:|---:|
| like 第 1 页（实达 1） | 2.9 | 3.2 |
| like 第 10 / 50 / 100 / 500 / 1000 页 | 2.7–3.0 | 2.8–3.1 |
| random 第 1 页 | 4.5 | 11.3 |
| random 第 10 / 50 页 | 3.0–3.2 | 3.1–3.3 |

✅ **深翻页完全水平**：第 1000 页与第 1 页同价（SQL 口径 0.19ms，`Index Cond: ROW(like,id) < ROW(...)`）。keyset 分页在 PG 成立，无 offset 灾难。

### 3.4 标签建议（warm）

| 场景 | min | median |
|---|---:|---:|
| suggest_aliases(limit=5) | 159.7 | 160.1 |

⚠ `ILIKE %..%` 全表扫 tag（73,822 行）约 160ms，搜索框每击键一次；可优化（见 §5 P4）。

### 3.5 冷启动（PG stop→start，全新连接池）

| 场景 | 冷-首查(ms) | warm(重启前,ms) | 倍率 |
|---|---:|---:|---:|
| 默认列表 | **58.5** | 3.0 | **19.7×** |
| 关键词恋列表 | 38.7 | 27.4 | 1.4× |
| 关键词 R-18 列表 | 5.4 | 5.3 | 1.0× |
| 标签 R-18 列表 | 4.4 | 3.1 | 1.4× |
| 计数默认阈值 | 55.9 | 56.1 | 1.0× |
| 计数关键词 R-18 | 2482.7 | 2447.8 | 1.0× |
| 翻页第 2 页 | 7.7 | 3.6 | 2.1× |

- PG 自身：stop 344ms / start 471ms（小数据目录，无崩溃恢复）。
- 冷启动首查最差 +55ms（默认列表 58.5ms）；关键词计数类冷/暖无差（瓶颈在查询形态而非缓存）。

---

## 4. 关键 EXPLAIN 发现（ANALYZE, BUFFERS）

### 4.1 计数型查询普遍绕开 GIN / 走全表

- **标签 R-18 计数**：`Parallel Seq Scan on novel` + Filter `(tags @> '...') AND (NOT tags && blocked)` → **107ms**。planner 对 92% 覆盖 tag 放弃 GIN 合理，但 blocked `NOT &&` 使每行做双数组比较，比 greenfield 无 blocked 的 29ms 慢 3.5×。
- **默认计数（阈值+blocked）** 95ms 同理。

### 4.2 中频 keyword 列表不经过 GIN

- **关键词恋列表（带阈值）**：`Nested Loop`——外层 `Index Scan Backward (ix_novel_like_id)` 扫 453 行，内层按 PK probe `novel_search` 并**逐行计算 `to_tsvector`** → 23–28ms。GIN 不参与（correlated-EXISTS 形态下内外侧无法反转为 GIN 驱动）。成本正比于“like 排序前段中该词的稀疏度”。
- 收益场景：`R-18`（94% 覆盖）5ms 早停；`哈利波特`（68 命中）9ms。

### 4.3 keyword 计数是最大单个瓶颈

- **关键词 R-18 计数**：`Parallel Hash Join`——novel **全表 Seq Scan**（过滤 blocked）+ `novel_search` Bitmap/GIN 命中 225,721 行全量物化进 hash → **2.46s**（read 26,056 页）。planner 把相关 EXISTS 重写成了 hash semi-join。
- **关键词催眠计数**：`Bitmap Heap Scan(novel_search, 15,192 行) → Nested Loop probe novel PK` → **653ms**（57k shared hit + 10.8k read，逐行单点读）。
- 两者都是“命中集大 + 必须与 novel 全量对比 blocked/阈值”的固有成本；**epoch 计数缓存是当前唯一兜底**（命中 0.8ms），但其失效窗口 = 每次真实写提交 + 每次进程重启。

### 4.4 健康项

- 默认列表：`Index Scan Backward (ix_novel_like_id)` + Filter blocked → 0.46ms。
- 翻页第 500 页：`Index Cond: ROW(like,id) < ROW(...)` → 0.19ms，单一 range scan 成立。

---

## 5. 结论与优化建议（按优先级）

### P0 — keyword 计数首击（2.5s / 0.6s）与缓存失效窗口 ✅ 已于 2026-09-05 落地（精简版）

背景：`_count_cache`（进程内 epoch 缓存）命中 0.8ms，但**任何写提交都会 bump epoch 使缓存全失效**，且进程重启后为空 → 写后第一个 count 请求付全价（R-18 2.5s / 催眠 0.6s）。

精简落地（`src/copixiv/features/novels/repo.py`）：写提交后由**后台 daemon 线程重算全部已缓存 key**（单用户场景 key 集很小）。实现 ~40 行：模块级注册 `Session.after_commit` 监听，用 epoch 守卫过滤掉只读 commit（防止每请求开线程）；线程 best-effort（失败静默）。

**验证**（真实库）：R-18 count 首次 2490ms 进缓存 → 一次写提交（epoch 0→1）→ 6 秒内后台线程重算完成（缓存条目 epoch 对齐）→ 再次 count **3.8ms 且数值一致**。`tests/api/test_blocked_tag_exclusion.py` 17 项全过。

剩余窗口：写提交后 ~6 秒内的首次 count 仍可能付全价（后台线程尚未完成）——单用户场景接受。进程重启后的首次 count 同样付一次全价（P0 不解决，P3 预热也覆盖不到 count 语义，属已知残余）。

### P1 — 中频 keyword 列表（20–30ms）→ 单用户评估：不处理

冷启动层（P3）落地后恋列表预热态 42ms，绝对值可接受。改进仅有两条路，均有明显代价：改写为 GIN 驱动（`novel_search` 侧 top-N sort）会让 94% 覆盖词（R-18）从 5ms 退化到 ~30ms；按词频分流则引入复杂度。单用户场景收益 <30ms 且不改变功能 → **不做，保持现状**。若未来想要最简缓解，可给前端关键词搜索加 50ms 级防抖（大概率已存在）。

### P2 — blocked 排除的计数代价（29ms → 100ms）→ 单用户评估：不处理

替代方案（运行时物化 blocked id 集合 + `id != ANY(...)`）需要缓存/失效协调，且 100ms 只在 count 首击出现（缓存命中 0.8ms，且 P0 写后重算已预热）。单用户频率低 → **不做**。若 future blocked tags 数量显著增长（当前 8,071 行命中、仅 4 个 tag），再评估。

### P3 — 冷启动与 PG 配置（✅ 已于 2026-09-05 落地）

已实施的两步（即用户确认的"最简方案"，不做 pg_prewarm——pgserver 无 contrib）：
1. **应用启动预热**（`src/copixiv/app.py` lifespan）：与账号认证并行跑 4 个热查询形态（默认列表 / 随机一页 / keyword「恋」列表 / 默认阈值计数），预热成本实测 **127ms**，失败仅记日志不阻断启动。
2. **postgresql.conf 调参**（`.spike/pgdata/postgresql.conf`）：`shared_buffers 128MB→1GB`、`work_mem 4MB→16MB`、`effective_cache_size→6GB`（7.8GB 机器按 spike §9 建议），PG 重启后 `SHOW` 已验证生效。

**效果验证**（PG restart → 全新连接池 → 清空进程内 count 缓存，3 次取 min）：

| 形态 | 重启后首查（冷） | 预热后 | 结论 |
|---|---:|---:|---|
| 默认列表 | 35.7ms | **3.0ms** | ✅ 12×，热/冷差距消除 |
| 恋列表 | 57.7ms | 42.4ms | ⚠ 只改善 ~27%——本质是查询形态问题（见 §4.2/P1），预热治不了 |
| R-18 计数 | 2526.8ms | 2458.7ms | ⚠ 基本不变——2.5s 是 hash-join 计算成本（见 §4.3/P0），与页缓存无关 |
| 翻页第 2 页 | 8.6ms | 6.4ms | ✅ |

预热**未覆盖**的新形态首查在 1GB buffer 下：催眠列表 26.3ms / tagNTR 4.1ms / 哈利波特计数 7.7ms。

**说明**：预热+调参解决的是"冷启动/缓存冷"这一层（列表/翻页类）；keyword 计数 2.5s 与中频词列表 20–40ms 属于查询形态成本，仍需 P0/P1。预热代码在应用进程重启后生效（当前运行中的 main.py 需重启一次）；OS 页缓存兜底保证运行期数小时内热度不衰减，机器/PG 重启后由启动预热自动恢复。

### P4 — 标签建议 160ms（别名管理 UI，非主搜索框）→ 单用户评估：不处理

核查后更正：`suggest_aliases` 服务于**标签别名管理弹窗**（低频），不是主搜索框联想。其 160ms = 每候选 tag 一次 `ILIKE '%..%'` 全表扫（73,822 行）。两段式前缀优化（btree 前缀 + 子串兜底）会改变“按 reference_count 全局排序取 50”的语义（跨段并集无法保序），不值得为此破坏契约 → **不做**。若未来接入 contrib，`pg_trgm` 为根治。

### P5 — repo 每请求的固定小查询 → 单用户评估：不处理

`_exclusion_active`（Setting）+ `_blocked_tag_names`（TagPreference）每请求 2 次 DB 往返（默认列表 3.2ms 中约占一半）。加 5s TTL 缓存需约 15 行，但会破坏“偏好切换立即生效”的显式设计契约（`services.py` 注释声明），且单用户收益 ~1ms/请求 → **不做**。

### 附带修复（本检查中发现的产品 bug）

- **random 浏览翻页 500**：`novel.author_id IS NULL`（26 行）时 `_base_select` 的 `is_special_follow` 标量子查询返回 NULL，`Novel` 域模型校验失败 → 随机浏览翻到含此行的页就 500。
  - 修复：`src/copixiv/features/novels/repo.py` 中 sf 子查询改为 `COALESCE(..., FALSE)`（本会话已改，见 git diff）。

---

## 6. 复现

```bash
source .venv/bin/activate
python scripts/bench_pg_query_performance.py check     # 环境核查
python scripts/bench_pg_query_performance.py bench     # warm 矩阵（只读）
python scripts/bench_pg_query_performance.py explain   # 关键查询 EXPLAIN
python scripts/bench_pg_query_performance.py cold      # 冷启动实测（会重启本地 PG）
```

- 各命令输出即本报告 §3/§4 的数据来源；`--url` 可指向其它实例。
- 基准均在真实 `copixiv` 库上执行；`bench`/`explain` 只读，`cold` 重启本地 PG（数据不丢）。