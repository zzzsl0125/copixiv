# copixiv 目录边界与架构约束

> 本文件定义模块化单体的职责边界、依赖约定以及静态执法规则。

## 1. 目录职责

| 目录 / 模块 | 职责 |
|---|---|
| `app.py` / `middleware.py` / `error_handlers.py` / `lifespan.py` | 组合根：FastAPI 工厂、中间件、域异常映射、生命周期装配（含查询引擎启动与 UoW 提交后钩子绑定）与 SPA 静态托管 |
| `config.py` | Pydantic 配置模型、严格校验（`extra="forbid"`）与加载 |
| `deps.py` | FastAPI DI 依赖（`get_uow` / `get_pixiv_client` / `get_source_registry` / `get_source_service` / `get_source_http_manager` / `get_image_downloader` / `get_task_manager` / API Key 鉴权及 `TaskManagerProtocol` 等） |
| `export_openapi.py` | 无 DB / 网络依赖导出 OpenAPI schema（`python -m copixiv.export_openapi`） |
| `core/` | 纯 Python 核心：`models.py`、`sources.py`、`draft.py`、`naming.py`、`search.py`、`text.py`、`time.py`、`snapshot_types.py`、`exceptions.py` |
| `db/` | PostgreSQL 唯一适配层：`engine.py` / `uow.py` / `tx.py`（`read_transaction`、`run_write_transaction[_sync]` 与重试退避）/ `backup.py` / `models.py`（13 张业务与任务表：含 `source_config` 与 `source_credential`，已移除 `token` 与 `tag` 表）/ `pg_objects.py` / `base.py` / `delta.py`（事务提交后增量缓冲）/ `novel_snapshot.py`（窄列快照投影） |
| `query_engine/` | 进程内查询引擎叶子适配层：`port.py`（协议与 DTO）/ `adapter.py`（封装 `copixiv_rs`）/ `sync.py`（启动快照与串行增量同步） |
| `crates/copixiv_rs/` | Rust PyO3 原生检索引擎 crate：`arena.rs` / `engine.rs` / `filter.rs` / `hydrate.rs` / `interner.rs` / `lib.rs` / `model.rs` / `mutation.rs` / `postings.rs` / `query/`（`mod.rs`, `executor.rs`, `mask.rs`, `planner.rs`）/ `random.rs` / `staging.rs` / `storage.rs` / `tag_index.rs`（惰性派生 `TagIndex`）/ `text_index.rs` / `tracks.rs` |
| `pixiv/` | 原生异步 `httpx` Pixiv 客户端与 DTO：`client.py` / `http.py` / `models.py` / `account.py` / `accounts.py` / `errors.py` |
| `sources/` | 多数据源协议插件适配层：`base.py` / `http.py`（按数据源隔离的 `httpx.AsyncClient` 与 `ImageDownloader`）/ `registry.py` / `pixiv.py` / `local.py` / `esj/`（`plugin.py`, `client.py`, `parser.py`） |
| `storage/` | 本地存储与文件处理：`file_storage.py` / `image_downloader.py` / `encoding.py` / `orphan_cleanup.py` / `zip.py` / `atomic.py` / `assets.py` / `epub/`（`builder.py`, `reader.py`, `state.py`） |
| `notify/` | 通知后端适配：`telegram.py` |
| `tasks/` | 任务内核（`registry.py` / `executor.py` / `cron.py` / `manager.py`）+ `history_repo.py` / `api.py` / `schemas.py` / 业务任务（`novels.py`、`batch.py` 统一异步 `batch_export`、`sources.py` 通用 `source_fetch`、`maintenance/` 含 `backup.py`, `epub.py`, `files.py`, `refetch.py`, `series.py`） |
| `features/` | 业务功能切片：单文件切片 `search_history.py`；子包切片 `authors/`（`api.py` 含 `/api/novels/author/search`, `repo.py`, `service.py`）、`failures/`（`api.py`, `repo.py`）、`library/`（`api.py`, `ingest.py`, `repo.py`）、`series/`（`api.py` 含 `/api/novels/series/search`, `repo.py`, `service.py`）、`sources/`（`api.py` 含 `/api/sources/fetch`, `entity_resolver.py`, `repo.py`, `schemas.py`, `service.py`）、`system/`（`api.py`, `repo.py`）、`tags/`（`aliases.py`, `preferences.py`, `repo.py`, `resolver.py`）、`novels/`（`field_policy.py`, `schemas.py`, `repo/{read_repo,write_repo}.py`, `routes/{novel,query,batch,downloads}.py`, `ingest/{pipeline,download,persist,asset_pipeline,book_chapter}.py`, 扁平函数式 `use_cases/`） |

## 2. 架构契约（由 `lint-imports` 强制执行）

根目录 `.importlinter` 定义了 12 条静态契约，运行 `.venv/bin/lint-imports` 检查：

1. **`core-is-pure`**：`copixiv.core` 为纯 Python 层，禁止导入 `copixiv.{db,features,tasks,pixiv,sources,storage,notify,query_engine,app}` 以及 `fastapi` / `sqlalchemy`。
2. **`adapters-are-leaves`**：适配层 `copixiv.{db,pixiv,sources,storage,notify,query_engine}` 为底层叶子，禁止反向依赖 `copixiv.{features,tasks,app}`。
3. **`no-removed-vendors`**：全仓禁止引入已淘汰的第三方依赖（`pixivpy3` / `requests` / `ebooklib` / `lxml`）。
4. **`storage-isolated-from-pixiv`**：`copixiv.storage` 不得反向依赖 `copixiv.pixiv` 或 `copixiv.sources`。
5. **`httpx-in-transport-only`**：`httpx` 仅允许被传输适配层（`pixiv` / `sources` / `storage` / `notify`）及 `lifespan` 直接导入。
6. **`db-is-the-only-sql-layer`**：`copixiv.{core,pixiv,sources,storage,notify,query_engine}` 禁止导入 `sqlalchemy`。
7. **`tasks-no-fastapi`**：`tasks/` 运行时与业务任务适配器（除 `tasks.api`）禁止依赖 `fastapi`。
8. **`tasks-no-feature-apis`**：`copixiv.tasks` 不依赖 `features/*/api`、`features.novels.routes` 或单文件路由切片（`search_history` / `tags.{aliases,preferences}`）。
9. **`features-no-tasks`**：`copixiv.features` 不反向依赖 `copixiv.tasks`。
10. **`leaf-features-no-novels`**：叶子切片（`authors` / `failures` / `library` / `search_history` / `series` / `sources` / `system` / `tags`）不反向依赖 `features.novels`。
11. **`novels-isolated-from-library`**：`features.novels` 不反向依赖 `features.library`。
12. **`library-isolated-from-failures`**：`features.library` 不依赖 `features.failures`。

## 3. 分层与编码约定

1. **`features` 单向依赖与扁平函数式用例**：叶子切片不反向依赖 `features.novels`，跨切片不循环依赖或导入私有符号（`_xxx`）；`features/novels/use_cases/` 与 `authors/service.py`、`series/service.py` 采用扁平函数式用例与服务函数，不做多余的类包裹。
2. **写事务边界与内存引擎同步纪律**：
   - HTTP 端点与用例通过 `UowDep` 获取请求作用域的 `SqlUnitOfWork`，并显式调用 `db/tx.py::run_write_transaction_sync` 或 `run_write_transaction`（内建 `55P03` / `40P01` / `40001` 退避重试）；不在写事务内持有外部网络 I/O；
   - `db/` 与 `query_engine/` 互为独立叶子：`db/delta.py` 在事务期间暂存变更，由 `lifespan.py` 在装配期向 `session_factory` 注入 `query_engine.sync` 的 `post_commit_hook`，保证只有已提交事务才会同步至内存索引（`TagIndex` 按需惰性构建），回滚自动隔离。
3. **ORM 与迁移同步**：修改 `db/models.py` 或 `db/pg_objects.py` 后必须同步检查 `.venv/bin/alembic -x url=... check`（在测试库 `copixiv_test` 上），确保不产生隐性 Schema 漂移。
4. **前端状态隔离**：跨页共享的壳层 UI 状态（`isBatchMode` / `selectedIds` / `isInCollectionView` / `refreshNonce`）由 `App.vue` 通过 `useProvideNovelShellState()` 注入；小说列表筛选、虚拟瀑布流、统一批量状态机 `useBatchMode` 与编辑/上传模态作用域收口于 `frontend/src/views/Novels.vue` 及对应 composables。
