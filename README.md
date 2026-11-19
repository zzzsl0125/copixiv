# copixiv

抓取、检索、管理与导出 Pixiv 中文小说。

## 技术栈

| 维度 | 说明 |
|---|---|
| 后端 | Python 3.12+，FastAPI + SQLAlchemy 2.0 + `pydantic-settings` + `tenacity` + Alembic + Loguru |
| 检索引擎 | Rust (`crates/copixiv_rs`，PyO3 + `lasso` + `roaring` + `memchr` + `rayon` + `parking_lot`) |
| 前端 | Vue 3 + Vite + TypeScript + Tailwind CSS v4 + `@tanstack/vue-query` + `@tanstack/vue-virtual` + `reka-ui`（`frontend/package.json`） |
| 数据库 | 标准 PostgreSQL 16（无需 `pg_bigm`，Alembic 基线迁移 `0001_baseline`；`BIGINT` / `timestamptz` / `JSONB` / `TEXT[]`） |
| Pixiv 客户端 | 原生异步 `httpx` 客户端 + Pydantic v2 DTO（`pixiv/`，含统一 `Retry-After` / `tenacity` 重试与账号池调度） |
| 服务端口 | `9000`（生产由后端同进程托管 `frontend/dist`，Vite 开发代理转发 `/api` → `9000`） |

---

## 快速开始

### 环境要求

- Python 3.12+（使用 [`uv`](https://docs.astral.sh/uv/) 与 `uv.lock` 管理）
- Rust 工具链（`stable` `cargo` / `rustc`）与 `maturin`（构建进程内检索引擎 `copixiv_rs`）
- Node.js 20.19+（仅前端构建需要）
- 标准 PostgreSQL 16（无需安装任何额外扩展；本地开发推荐用 `scripts/pg_dev.py` 管理的自带 `pgserver` 实例，端口 `5433`）
- Pixiv 账号（需获取 `refresh_token`）

### 1. 初始化配置

```bash
git clone https://github.com/zzzsl0125/copixiv.git
cd copixiv
cp config.example.yaml config.yaml
```

配置加载顺序：**Pydantic 默认值 → `config.yaml` → 环境变量（前缀 `COPIXIV_`，嵌套键使用 `__` 分隔，如 `COPIXIV_PROXY__URL`、顶级键如 `COPIXIV_DATABASE_URL`）**。主要配置节：

- `database_url`：PostgreSQL 连接串（默认 `127.0.0.1:5433`）
- `notifications`：通知后端列表（如 `type: telegram` + `token` / `chat_id`，空列表 `[]` 关闭通知）
- `proxy.url`：单键同时用于 HTTP/HTTPS
- 其余配置节：`path`、`pixiv_client`、`batch_download`、`backup`、`security`（见 `config.example.yaml` 与 `src/copixiv/config.py`）

### 2. 构建 Rust 扩展、启动 PostgreSQL 与后端

```bash
uv sync --extra dev --extra local-pg
uv pip install --no-build-isolation ./crates/copixiv_rs

# 启动本地 PostgreSQL（数据目录 data/pg/pgdata，端口 5433，日志 log/pg.log）
uv run python scripts/pg_dev.py start

# 启动应用（自动应用 Alembic 基线迁移 0001_baseline，随后流式加载内存检索快照）
uv run python -u main.py
```

> **老库升级到 Phase 6 压缩基线**：若现有生产库曾在旧 `0001`–`0017` 迁移链上运行，先执行只读检查，再清理历史索引/列并重打基线戳：
> ```bash
> uv run python scripts/transition_to_baseline.py          # 只读检查
> uv run python scripts/transition_to_baseline.py --apply  # 幂等清理并 stamp 至 0001_baseline
> ```

### 3. 构建前端（可选）

后端（`copixiv.app.mount_frontend`）会直接静态托管 `frontend/dist`，因此只需构建一次，无需单独运行 `vite preview`：

```bash
cd frontend
npm install
npm run build
```

打开 `http://localhost:9000` 即可使用（API 与页面同进程同端口）。前端源码修改后重新执行 `npm run build` 即可生效，无需重启后端；`frontend/dist` 不存在时后端仅提供 API 服务。前端开发细节见 [frontend/README.md](frontend/README.md)。

### 4. Systemd 部署

仓库提供拆分的 PostgreSQL 单元 [deploy/copixiv-pg.service](deploy/copixiv-pg.service) 与主服务单元 [deploy/copixiv.service](deploy/copixiv.service)，以及无 `root` 污染的构建发布脚本 [scripts/deploy_backend.sh](scripts/deploy_backend.sh)：

```bash
sudo cp deploy/copixiv-pg.service deploy/copixiv.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now copixiv-pg.service copixiv.service
```

---

## 小说入库

1. 在 **侧边栏 - 账号管理** 内添加可用的 Pixiv 账号。
2. 在 **侧边栏 - 任务管理** 内配置或运行抓取任务；本地 TXT / EPUB 可在 **我的上传** 中直接上传。

## API

| 前缀 | 路由模块 | 说明 |
|------|----------|------|
| `/api/novels` | `features/novels/routes/` | 列表、计数、按 ID 批量卡片（`by-ids`）、排序/范围辅助（`sort-ids` / `match-ids` / `ids` / `blocked-ids`）、作者/系列预览、下载、收藏、特别关注、单本编辑/同步/删除、批量操作与打包导出 |
| `/api/library` | `features/library/api.py` | 本地 TXT / EPUB 上传与入库 |
| `/api/authors` | `features/authors/api.py` | 作者删除与级联清理 |
| `/api/tasks` | `tasks/api.py` | 任务方法、定时任务管理、立即运行、任务取消与执行历史 |
| `/api/system` | `features/system/api.py` | 系统配置（含 `exclude_blocked_tag_novels` / `batch_download_naming`） |
| `/api/tag-preferences` | `features/tags/preferences.py` | 标签偏好（喜爱 / 厌恶）与排序 |
| `/api/tag-aliases` | `features/tags/aliases.py` | 标签别名映射与基于 Rust 标签索引的相似标签建议 |
| `/api/search-history` | `features/search_history/api.py` | 搜索历史 |
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
core/                          纯 Python（实体 / 命名 / 搜索分词 / 文本启发式 / 异常；零框架与 ORM 依赖）

db / pixiv / storage / notify / query_engine 为底层适配叶子，不被反向依赖，也不持有上层引用；
crates/copixiv_rs 为 PyO3 原生检索扩展，由 query_engine 适配层封装调用。
```

### 数据库、检索引擎与后台任务

- **PostgreSQL 持久化真源**：`db/models.py` 定义全部 13 张表（`author` / `series` / `novel` / `tag` / `tag_preference` / `setting` / `tag_alias` / `scheduled_task` / `task_history` / `token` / `failed_novel` / `series_dead_letter` / `search_history`），`db/pg_objects.py` 定义唯一真源序列、标签触发器与表级 autovacuum 参数，由单一基线迁移 `0001_baseline` 管理（可用 `alembic check` 校验 ORM 与迁移一致）。写入采用 PG MVCC 多写者 + `lock_timeout='5s'` + `ON CONFLICT` upsert + `db/tx.py::run_write_transaction`（针对 `55P03` / `40P01` / `40001` 退避重试）。
- **Rust 进程内检索引擎（`copixiv_rs`）**：
  - 启动时由 `db/novel_snapshot.py::PgSnapshotStore` 在 `REPEATABLE READ READ ONLY` 快照下流式投影窄列构建内存索引；所有写事务通过 `session_factory` 原生 SQLAlchemy 事件（`after_flush` / `after_commit` / `after_rollback`）收集 `PendingEngineDelta`，提交后再原子调用 `apply_delta(...)` 增量同步，回滚绝不污染内存。
  - 内部按 `engine`/`storage`/`interner`/`postings`/`text_index`/`tag_index`/`tracks`/`mutation`/`query`/`hydrate`/`staging` 模块化拆分，支持小说全文/多维检索、真随机分页（`splitmix64`）与标签子串/相似度别名推荐，列表查询与 `/by-ids` 直出卡片对象。
- **后台任务内核（`tasks/`）**：
  - `registry.py`（`@register(lane, timeout, notify)` 与独立 `TaskContext::report_progress`）+ `executor.py` + `cron.py`（`cronstrue` / `croniter`）+ `manager.py`（双 lane `asyncio.Event` 与任务取消追踪）。
  - 业务任务覆盖抓取（`tasks/novels.py`）、批量删改/打包（`tasks/batch.py`）与维护巡检（`tasks/maintenance/`：`search` / `epub` / `series` / `files` / `history` / `backup` / `refetch`）。

---

## 目录与关键文件索引

```text
crates/
└── copixiv_rs/         # Rust PyO3 检索引擎（arena / filter / random / engine / interner / postings / text_index / tag_index / tracks / query / ...）
src/copixiv/
├── app.py              # FastAPI 组合根：create_app / mount_routers / mount_frontend / main()
├── middleware.py       # Host 校验、API Key 鉴权、CORS、GZip 与访问日志中间件
├── error_handlers.py   # 域异常 → HTTP 状态码与 DB 约束映射
├── lifespan.py         # AsyncExitStack 统一托管 DB、共享 httpx、检索引擎、账号池、任务系统与备份
├── config.py           # pydantic-settings 配置模型与 config.yaml/环境变量加载器
├── deps.py             # FastAPI DI 依赖（get_uow / get_write_uow / get_task_manager / API Key 等）
├── log.py              # Loguru 日志配置与有界环形缓冲区
├── export_openapi.py   # 无 DB/网络依赖导出 OpenAPI schema
├── core/               # 纯 Python：models / draft / naming / search / text / time / snapshot_types / exceptions
├── db/                 # PG 适配：models / engine / uow / tx / pg_objects / backup / constants / base / delta / novel_snapshot
├── query_engine/       # 进程内引擎适配层：port / adapter / sync
├── pixiv/              # 原生异步 httpx Pixiv 协议层：client / http / models / account / accounts / errors
├── storage/            # 本地存储：file_storage / image_downloader / encoding / orphan_cleanup / zip / atomic / assets / epub/
├── notify/             # 通知适配：telegram / factory
├── tasks/              # registry / executor / cron / manager / history_repo / schemas / api / novels / batch / maintenance/
└── features/           # accounts / authors / failures / library / novels / search_history / series / system / tags
alembic/                # 数据库迁移基线 (0001_baseline)
deploy/                 # systemd 单元文件 (copixiv.service + copixiv-pg.service)
docs/                   # MODULARITY.md、REFACTOR_PLAN.md、设计文档与性能分析
frontend/               # Vue 3 + TypeScript + Tailwind CSS v4 + Vue Query + reka-ui 前端
scripts/                # pg_dev.py / transition_to_baseline.py / deploy_backend.sh / run_verification.sh / bench_engine_comparison.py
tests/                  # unit/、db/（PG 基线迁移、触发器与 Rust 对拍）与 integration/
```

---

## 质量门禁与基准

```bash
bash scripts/run_verification.sh                            # 完整 8 项本地门禁（Rust → ruff → mypy → lint-imports → OpenAPI → pytest → alembic check → 前端）
uv run pytest tests -v --cov=copixiv                      # 后端 unit + PG16 + integration 全量测试
uv run alembic check                                      # ORM 模型 ↔ 迁移基线对齐检查
uv run python scripts/transition_to_baseline.py           # 生产老库迁移至 0001_baseline 的只读预检
uv run python scripts/bench_engine_comparison.py          # 只读检索延迟与一致性基准
```
