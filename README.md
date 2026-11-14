# copixiv

Pixiv 小说下载与管理系统（v2）：抓取、检索、管理与导出 Pixiv 中文小说，并支持本地 TXT / EPUB 上传入库与进程内高速检索。

## 功能特性

- **批量入库与本地上传**：支持爬取关注更新、作者作品、排行榜或搜索结果入库；也支持上传本地 TXT / EPUB（负数自增 ID、正文规范化哈希去重）并编辑元数据（作者 ID / 系列 ID / 章节号 / 标签 / 发布时间 / 本地标题）
- **Rust 进程内检索与真随机推荐**：默认通过 Rust（PyO3 + `maturin`）扩展 `copixiv_rs` 将全部列表查询、计数、`/ids`、`/blocked-ids`、`/by-ids` 与无偏 `splitmix64` 随机分页在进程内存中直出完成，不回表 PostgreSQL；支持通过 `query_engine` 配置秒级回落 `pg` 或开启 `shadow` 影子对拍，PostgreSQL `pg_bigm` 索引与全文规范化链路完整保留作为真源与降级兜底
- **个人喜好与厌恶标签排除**：支持收藏作品、特别关注作者、标签偏好（喜爱 / 厌恶）与标签别名聚合映射；启用排除开关时（默认开启），带厌恶标签的小说从列表、搜索、计数、`/by-ids` 与全选中隐藏，并支持「查看被隐藏的小说」与范围内筛选排序
- **定时任务与死系列台账**：内置任务队列与 `croniter` 调度，支持进度追踪（`progress`）、Telegram 通知与连续确定性 404/410 死系列追踪（`series_dead_letter`）
- **Pixiv 账号池**：支持多 Pixiv 账号并发爬取、热重载、负载均衡与冷却，可指定追更账号（`is_follow`）
- **单进程一体部署**：FastAPI 直接静态托管构建后的 `frontend/dist`（`:9000`），API 与前端页面同源同端口

## 技术栈

| 维度 | 说明 |
|---|---|
| 后端 | Python 3.12+，FastAPI + SQLAlchemy 2.0 + Pydantic v2 + croniter + Alembic + Loguru |
| 检索引擎 | Rust (`crates/copixiv_rs`，PyO3 + `roaring` + `memchr` + `rayon` + `parking_lot`) |
| 前端 | Vue 3 + Vite + TypeScript + Tailwind CSS v4（`frontend/package.json`） |
| 数据库 | PostgreSQL（Alembic 迁移 `0001`–`0016`；`BIGINT` / `timestamptz` / `JSONB` / `TEXT[]`+GIN / `pg_bigm`） |
| Pixiv 客户端 | `pixivpy3`（git 直连 `upbit/pixivpy`）+ `pixiv/` 防腐层补丁 |
| 服务端口 | `9000`（生产由后端同进程托管 `frontend/dist`，Vite 开发代理转发 `/api` → `9000`） |

---

## 快速开始

### 环境要求

- Python 3.12+
- Rust 工具链（`stable` `cargo` / `rustc`）与 `maturin`（构建进程内检索引擎 `copixiv_rs`）
- Node.js 20.19+（仅前端构建需要）
- PostgreSQL（带 `pg_bigm` 扩展；本地开发推荐用 `scripts/pg_dev.py` 管理的自带 `pgserver` 实例，端口 `5433`）
- Pixiv 账号（需获取 `refresh_token`）

### 1. 初始化配置

```bash
git clone https://github.com/zzzsl0125/copixiv.git
cd copixiv
cp config.example.yaml config.yaml
```

配置加载顺序：**Pydantic 默认值 → `config.yaml` → 环境变量（前缀 `COPIXIV_`，嵌套键使用 `__` 分隔，如 `COPIXIV_PROXY__URL`、顶级键如 `COPIXIV_DATABASE_URL` / `COPIXIV_QUERY_ENGINE`）**。主要配置节：

- `database_url`：PostgreSQL 连接串（默认 `127.0.0.1:5433`）
- `query_engine`：列表/检索/计数执行后端，可选 `rust`（默认，进程内索引）、`pg`（纯 PostgreSQL）、`shadow`（双跑校验一致性）
- `notifications`：通知后端列表（如 `type: telegram` + `token` / `chat_id`，空列表 `[]` 关闭通知）
- `proxy.url`：单键同时用于 HTTP/HTTPS
- 其余配置节：`path`、`pixiv_client`、`batch_download`、`backup`、`security`（见 `config.example.yaml` 与 `src/copixiv/config.py`）

### 2. 构建 Rust 扩展、启动 PostgreSQL 与后端

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install ".[dev]" maturin

# 编译并安装进程内检索引擎 copixiv_rs 到 .venv
maturin develop --release --manifest-path crates/copixiv_rs/Cargo.toml

# 启动本地 PostgreSQL（数据目录 data/pg/pgdata，端口 5433，日志 log/pg.log）
python scripts/pg_dev.py start
# 如需编译 pg_bigm 扩展，参见 scripts/build_pg_bigm.sh

# 启动应用（自动应用 Alembic 迁移 0001–0016，随后流式加载内存检索快照）
python -u main.py
```

### 3. 构建前端（可选）

后端（`copixiv.app.mount_frontend`）会直接静态托管 `frontend/dist`，因此只需构建一次，无需单独运行 `vite preview`：

```bash
cd frontend
npm install
npm run build
```

打开 `http://localhost:9000` 即可使用（API 与页面同进程同端口）。前端源码修改后重新执行 `npm run build` 即可生效，无需重启后端；`frontend/dist` 不存在时后端仅提供 API 服务。前端开发细节见 [frontend/README.md](frontend/README.md)。

### 4. Systemd 部署

仓库提供统一的单单元部署文件 [deploy/copixiv.service](deploy/copixiv.service)（`KillMode=process`，重用 `ExecStartPre` 拉起的后台 PostgreSQL 实例）与更新脚本 [scripts/deploy_backend.sh](scripts/deploy_backend.sh)：

```bash
sudo cp deploy/copixiv.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now copixiv.service
```

---

## 小说入库

1. 在 **侧边栏 - 账号管理** 内添加可用的 Pixiv 账号。
2. 在 **侧边栏 - 任务管理** 内配置或运行抓取任务；本地 TXT / EPUB 可在 **我的上传** 中直接上传。

## API

| 前缀 | 路由模块 | 说明 |
|------|----------|------|
| `/api/novels` | `features/novels/api.py` | 列表、计数、按 ID 批量卡片（`by-ids`）、排序/范围辅助（`sort-ids` / `match-ids` / `ids` / `blocked-ids`）、下载、收藏、特别关注、单本编辑/同步/删除、批量操作与打包导出 |
| `/api/library` | `features/library/api.py` | 本地 TXT / EPUB 上传与入库 |
| `/api/tasks` | `tasks/api.py` | 任务方法、定时任务管理、立即运行、执行历史 |
| `/api/system` | `features/system/api.py` | 系统配置（含 `exclude_blocked_tag_novels` / `batch_download_naming`） |
| `/api/tag-preferences` | `features/tags/preferences.py` | 标签偏好（喜爱 / 厌恶）与排序 |
| `/api/tag-aliases` | `features/tags/aliases.py` | 标签别名映射与相似标签建议 |
| `/api/search-history` | `features/novels/history_api.py` | 搜索历史 |
| `/api/tokens` | `features/accounts/api.py` | Pixiv 刷新令牌管理 |
| `/api/failed-novels` | `features/failures/api.py` | 失败台账查看、清除、重置计数与重试 |

OpenAPI 契约由 `src/copixiv/export_openapi.py` 导出为 `frontend/openapi.json`，再通过 `npm --prefix frontend run codegen` 生成 `frontend/src/types/api.generated.ts`。

---

## 架构与数据流

### 模块化单体边界

项目采用**模块化单体**：组合根（`app.py` / `middleware.py` / `error_handlers.py` / `lifespan.py`）+ 纯 Python 核心（`core/`）+ 底层适配器叶子（`db/` / `pixiv/` / `storage/` / `notify/` / `query_engine/`）+ 任务内核（`tasks/`）+ 按功能切片的 `features/`。全部静态边界契约由 `.importlinter` + `lint-imports` 强制执行，详见 [docs/MODULARITY.md](docs/MODULARITY.md)。

```text
app.py + lifespan.py + middleware.py + error_handlers.py（组合根与装配）
   │
   ▼
features/*  +  tasks/*         业务切片 + 任务内核
   │
   ▼
core/                          纯 Python（实体 / 纯服务 / 异常；零框架与 ORM 依赖）

db / pixiv / storage / notify / query_engine 为底层适配叶子，不被反向依赖，也不持有上层引用；
crates/copixiv_rs 为 PyO3 原生检索扩展，由 query_engine 适配层封装调用。
```

### 数据库、检索引擎与后台任务

- **PostgreSQL 持久化真源**：`db/models.py` 定义全部 13 张表（`author` / `series` / `novel` / `tag` / `tag_preference` / `setting` / `tag_alias` / `scheduled_task` / `task_history` / `token` / `failed_novel` / `series_dead_letter` / `search_history`），由 Alembic `0001`–`0016` 管理迁移（可用 `alembic check` 校验 ORM 与迁移一致）。写入采用 PG MVCC 多写者 + `lock_timeout='5s'` + `ON CONFLICT` upsert + `run_write_transaction`（针对 `55P03` / `55006` 退避重试）。
- **Rust 进程内检索引擎（`copixiv_rs`）**：
  - 启动时由 `db/novel_snapshot.py::PgSnapshotStore` 流式投影窄列构建快照并原子交换上线，后续所有 ORM 刷新与 Core SQL 批量写通过 `db/delta.py::PendingEngineDelta` 收集并在 `SqlUnitOfWork.commit()` 成功后序列化回查同步，回滚绝不污染内存。
  - 内部维护列式数值列、`RoaringBitmap` 倒排、UTF-8 2-gram 位图 + `PackedArena`（`memmem` 精确子串核验，ASCII-only `to_ascii_lowercase` 对齐 PG `C` locale）、`NumericField` 预排序轨道与基于 `splitmix64(novel_id ^ seed)` 的 `rayon` 分块并行 Top-K 真随机分页（`4096` 槽位分块），列表查询与 `/by-ids` 由 `NovelRecord` 直出卡片对象。
  - PG 侧保留生成列 `novel.search_lower`（`copixiv_search_text(...)`）+ `pg_bigm` GIN 索引 `novel_search_bigm` 与 `abs(id)` 时间轴索引 `ix_novel_abs_id`，支持 `query_engine=pg` 一键降级。
- **后台任务内核（`tasks/`）**：
  - `registry.py`（`@register` 与 `TaskContext`）+ `executor.py`（执行、结果落库、进度回报与 Telegram 通知）+ `cron.py`（`croniter` 调度）+ `manager.py`（`TaskManagerSystem` 队列轮询与生命周期管理）+ `kernel.py`（兼容导出门面）。
  - 业务任务覆盖抓取（`tasks/novels.py`）、批量删改/打包（`tasks/batch.py`）与维护巡检（`tasks/maintenance/`：`search` / `epub` / `series` / `files` / `history` / `backup` / `refetch`）。

---

## 目录与关键文件索引

```text
crates/
└── copixiv_rs/         # Rust PyO3 检索引擎（src/: arena / filter / random / index / lib）
src/copixiv/
├── app.py              # FastAPI 组合根：create_app / mount_routers / mount_frontend / main()
├── middleware.py       # Host 校验、API Key 鉴权、CORS、GZip 与访问日志中间件
├── error_handlers.py   # 域异常 → HTTP 状态码统一映射
├── lifespan.py         # 数据库、检索引擎（引擎+commit 钩子）、账号池、任务系统与自动备份
├── config.py           # Pydantic 配置模型与环境变量加载器
├── deps.py             # FastAPI DI 依赖（get_uow / get_write_uow / get_task_manager 等）
├── log.py              # Loguru 日志配置
├── export_openapi.py   # OpenAPI schema 导出入口
├── core/               # 纯 Python：models / services / exceptions / draft / naming / ids / language / calendar / formats / search_text
├── db/                 # PG 适配：models / engine / uow / write_lock / backup / constants / base / delta / novel_snapshot
├── query_engine/       # 进程内引擎适配层：port / adapter / sync
├── pixiv/              # pixivpy3 防腐层：client / patch / account / accounts / errors
├── storage/            # 本地存储：file_storage / image_downloader / zip / atomic / assets / epub/
├── notify/             # 通知适配：telegram / factory
├── tasks/              # registry / executor / cron / manager / kernel / history_repo / schemas / api / novels / batch / maintenance/
└── features/           # accounts / authors / failures / library / novels(含 repo/) / system / tags
alembic/                # 数据库迁移 (0001–0016)
deploy/                 # systemd 单元文件 (copixiv.service)
docs/                   # MODULARITY.md 与性能分析文档
frontend/               # Vue 3 + TypeScript + Tailwind CSS v4 前端
scripts/                # pg_dev.py / build_pg_bigm.sh / deploy_backend.sh / run_verification.sh / bench_engine_comparison.py
tests/                  # unit/、db/（PG 与 Rust 影子/对拍测试）与 integration/
```

---

## 质量门禁与基准

```bash
bash scripts/run_verification.sh                        # 完整本地验证（ruff → mypy → lint-imports → pytest → 前端构建）
cargo test --manifest-path crates/copixiv_rs/Cargo.toml # Rust 检索引擎单元测试（需 PYO3_PYTHON 指向 .venv Python）
.venv/bin/alembic check                                 # ORM 模型 ↔ 迁移历史对齐检查
.venv/bin/python scripts/bench_engine_comparison.py     # 只读基准：对比当前库上 PG vs Rust 延迟与结果一致性
```
