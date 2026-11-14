# copixiv 目录边界与架构约束

> 本文件定义模块化单体的职责边界、依赖约定以及静态执法规则。

## 1. 目录职责

| 目录 / 模块 | 职责 |
|-------------|------|
| `app.py` / `middleware.py` / `error_handlers.py` / `lifespan.py` | 组合根：FastAPI 工厂、中间件、域异常映射、生命周期与 SPA 静态托管 |
| `config.py` | Pydantic 配置模型、严格校验与加载 |
| `deps.py` | FastAPI DI 依赖（`get_uow` / `get_write_uow` / `get_task_manager` 等） |
| `core/` | 纯 Python 核心：实体 `models.py`、纯服务 `services.py`、异常 `exceptions.py`、`draft.py`、`naming.py`、`ids.py`、`language.py`、`calendar.py`、`formats.py` |
| `db/` | PostgreSQL 唯一适配层：`engine` / `uow` / `write_lock`（事务边界标记）/ `backup` / `models` / `constants` / `base` |
| `pixiv/` | `pixivpy3` 防腐层（`client.py` / `patch.py` / `account.py` / `errors.py`） |
| `storage/` | 本地存储与文件处理：`file_storage.py` / `image_downloader.py` / `zip.py` / `atomic.py` / `assets.py` / `epub/` |
| `notify/` | 通知后端适配：`telegram.py` / `factory.py` |
| `tasks/` | 任务内核（`registry.py` / `executor.py` / `cron.py` / `manager.py` / `kernel.py` 兼容门面）+ `history_repo.py` / `api.py` / 业务任务（`novels.py` / `batch.py` / `maintenance/`） |
| `features/` | 业务功能切片：`accounts` / `authors` / `failures` / `library` / `novels`（含拆分的 `repo/` 子包）/ `system` / `tags` |

## 2. 架构契约（由 `lint-imports` 强制执行）

根目录 `.importlinter` 定义了四条静态契约，运行 `.venv/bin/lint-imports` 检查：

1. **`core-is-pure`**：`copixiv.core` 为纯 Python 层，禁止导入 `copixiv.{db,features,tasks,pixiv,storage,notify,app}` 以及 `fastapi` / `sqlalchemy`。
2. **`adapters-are-leaves`**：适配层 `copixiv.{db,pixiv,storage,notify}` 为底层叶子，禁止反向依赖 `copixiv.{features,tasks,app}`。
3. **`pixivpy3-whitelist`**：除 `copixiv.pixiv` 外，全仓任何业务与核心模块均禁止直接导入 `pixivpy3`（含间接直接引用约束，`allow_indirect_imports = true`）。
4. **`db-is-the-only-sql-layer`**：除 `db/` 及明确操作会话的业务层外，`copixiv.{core,pixiv,storage,notify}` 禁止导入 `sqlalchemy`。

## 3. 分层与编码约定

1. **`features` 单向依赖**：允许跨功能切片单向引用公开接口，禁止互相循环依赖，也禁止跨切片导入私有符号（`_xxx`）。
2. **写事务边界纪律**：
   - 普通 HTTP 写端点通过 `Depends(get_write_uow)` 获取受控事务；
   - 需先执行外部网络 I/O 再落库的用例（如单本同步、入库管线）使用 `run_write_transaction(...)` 管理短暂写事务并内建 `55P03` / `55006` 退避重试。
3. **ORM 与迁移同步**：修改 `db/models.py` 后必须同步检查 `.venv/bin/alembic check`，确保不产生隐性 Schema 漂移。
4. **前端状态隔离**：小说列表、筛选、批量选择与编辑/上传模态作用域收口于 `frontend/src/views/Novels.vue`，外壳 `App.vue` 仅承担导航栏壳层与全局消息/任务状态轮询。
