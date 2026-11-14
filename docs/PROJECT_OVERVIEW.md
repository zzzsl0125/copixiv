# copixiv 项目速览

> 事实性速览，与 `MODULARITY.md`（目录边界速查）配套。

## 1. 基本信息

| 维度 | 内容 |
|------|------|
| 定位 | Pixiv 与本地小说下载/管理系统 v2 |
| 版本 | 2.0.0（`pyproject.toml`） |
| 后端 | Python 3.12+，FastAPI + SQLAlchemy 2.0 + Pydantic v2 + croniter + Alembic + Loguru |
| 前端 | Vue 3 + Vite + TypeScript + Tailwind CSS v4（`frontend/package.json`） |
| 数据库 | PostgreSQL（Alembic 迁移 `0001`–`0015`；BIGINT / timestamptz / JSONB / `TEXT[]`+GIN / `pg_bigm`） |
| Pixiv 客户端 | `pixivpy3`（git 直连 `upbit/pixivpy`）+ 自定义 patch（`pixiv/` 防腐层） |
| 服务端口 | `9000`（生产同进程托管 `frontend/dist`，Vite dev proxy → `9000`） |

## 2. 架构

模块化单体：组合根 + 纯核心 + 适配层 + 任务内核 + 按功能切片。架构契约由 `.importlinter` + `lint-imports` 执法，详见 `MODULARITY.md`：

```text
app.py + lifespan.py + middleware.py + error_handlers.py（组合根与装配）
   │
   ▼
features/*  +  tasks/*         业务切片 + 任务内核
   │
   ▼
core/                          纯 Python（实体 / 纯服务 / 异常；零框架与 ORM 依赖）

db / pixiv / storage / notify 为适配层，不被反向依赖，也不持有上层引用。
```

| 目录 / 模块 | 职责 |
|-------------|------|
| `app.py` | 组合根：`create_app()` 工厂、路由挂载、`mount_frontend`、`main()` |
| `middleware.py` | API Key 鉴权与请求耗时/日志中间件 |
| `error_handlers.py` | 域异常 → HTTP 状态码统一映射 |
| `lifespan.py` | 数据库、账号池、任务系统与自动备份生命周期管理 |
| `deps.py` | FastAPI DI 依赖（`get_uow` / `get_write_uow` / `get_task_manager` 等） |
| `features/` | 按功能切片：`accounts` / `authors` / `failures` / `library` / `novels` / `system` / `tags` |
| `tasks/` | 拆分后的任务内核：`registry` / `executor` / `cron` / `manager` + `kernel` 兼容门面 + 业务任务 |
| `core/` | 纯 Python：Pydantic 实体、纯服务、域异常；无 FastAPI / SQLAlchemy 依赖 |
| `db/` | PostgreSQL 唯一适配：`engine` / `uow` / `write_lock` / `backup` / `models` / `constants` / `base` |
| `pixiv/` | `pixivpy3` 防腐层（唯一允许直接导入 `pixivpy3` 的目录） |
| `storage/` | 文件存储 / 原子写 / 压缩打包（`zip.py`）/ 图片下载 / EPUB |
| `notify/` | Telegram 通知后端与工厂 |

## 3. 目录结构

```text
src/copixiv/
├── app.py              # 组合根：create_app / mount_routers / mount_frontend / main()
├── middleware.py       # API Key 鉴权与请求日志中间件
├── error_handlers.py   # 域异常映射
├── lifespan.py         # 启动/关闭生命周期钩子
├── config.py           # 配置模型与加载
├── deps.py             # FastAPI 依赖（从 app.state 读取单例）
├── log.py              # Loguru 日志
├── export_openapi.py   # OpenAPI schema 导出入口
├── core/               # 纯 Python：models / services / exceptions / draft / naming / ids / language / calendar / formats
├── db/                 # engine / uow / write_lock / backup / models / constants / base
├── pixiv/              # client / patch / account / errors
├── storage/            # file_storage / image_downloader / epub/ / zip / atomic / assets
├── notify/             # telegram / factory
├── tasks/              # registry / executor / cron / manager / kernel / history_repo / schemas / api / novels / batch / maintenance/
└── features/           # accounts / authors / failures / library / novels(含 repo/) / system / tags
```

项目根目录：`main.py`、`pyproject.toml`、`.importlinter`、`config.yaml`（示例 `config.example.yaml`）、`alembic/`（`0001`–`0015` 迁移）、`data/pg/`（本地 `pg_dev.py` PostgreSQL 数据目录）、`download/`（FileStorage 根目录）、`frontend/`（Vue 3）、`scripts/`、`docs/`、`deploy/`（`copixiv.service`）。

## 4. 数据库

- **ORM 模型**：`db/models.py`（`author` / `series` / `novel` / `tag` / `tag_preference` / `setting` / `tag_alias` / `scheduled_task` / `task_history` / `token` / `failed_novel` / `series_dead_letter` / `search_history`），与 Alembic `0001`–`0015` 严格对齐（`alembic check` 零漂移）。
- **检索与排序**：关键词搜索走生成列 `novel.search_lower`（`copixiv_search_text(...)`）+ `pg_bigm` GIN 索引 `novel_search_bigm`，查询侧使用真子串 `LIKE`（`features/novels/search.py`）；时间轴排序通过 `abs(id)` 兼顾正数 Pixiv ID 与负数本地上传 ID（`ix_novel_abs_id`）。
- **并发事务**：PostgreSQL MVCC 多写者 + 多语句事务 + `lock_timeout='5s'` + `ON CONFLICT` upsert + `run_write_transaction`（针对 `55P03` / `55006` 退避重试）。
- **Novel 仓储**：`features/novels/repo/` 子包按职责拆分为 `query_builder.py`、`read_repo.py`、`write_repo.py`、`series_repo.py`，对外经 `__init__.py` 暴露统一门面。

## 5. API

| 前缀 | 模块 | 说明 |
|------|------|------|
| `/api/novels` | `features/novels/api.py` | 列表/计数/下载/收藏/特别关注/单本编辑同步删除/批量打包/批量操作/厌恶标签排除（含 `sort-ids` / `match-ids`） |
| `/api/library` | `features/library/api.py` | 本地 TXT/EPUB 上传与入库 |
| `/api/tasks` | `tasks/api.py` | 任务方法、定时任务管理、立即运行、执行历史 |
| `/api/system` | `features/system/api.py` | 系统配置（含 `exclude_blocked_tag_novels` / `batch_download_naming`） |
| `/api/tag-preferences` | `features/tags/preferences.py` | 标签偏好（喜爱/厌恶）与排序 |
| `/api/tag-aliases` | `features/tags/aliases.py` | 标签别名与相似标签建议 |
| `/api/search-history` | `features/novels/history_api.py` | 搜索历史 |
| `/api/tokens` | `features/accounts/api.py` | Pixiv 刷新令牌管理 |
| `/api/failed-novels` | `features/failures/api.py` | 失败台账查看/清除/重置计数/重试 |

## 6. 后台任务

- **任务内核拆解**：
  - `tasks/registry.py`：`@register`、Pydantic 参数模型绑定与 `TaskContext`
  - `tasks/executor.py`：任务执行、异步结果落库、进度回报与 Telegram 通知
  - `tasks/cron.py`：基于 `croniter` 的定时任务调度与并发控制
  - `tasks/manager.py`：`TaskManagerSystem`（生命周期管理、queue polling、enqueue / run_now / reload）
  - `tasks/kernel.py`：向外统一导出常用符号的兼容门面
- **业务任务**：`tasks/novels.py`（单本/关注/作者/排行/搜索）、`tasks/batch.py`（大批量删改标签/打包导出）、`tasks/maintenance/`（搜索索引刷新 / EPUB 对账 / 章节号补全 / 孤儿文件回收 / 任务历史裁剪）。
- **死系列台账**：`features/novels/series_ledger.py` + `series_dead_letter` 表，连续 3 次确定性 404/410 才标记退役。

## 7. 配置

加载顺序：Pydantic 默认值 → `config.yaml` → 环境变量（前缀 `COPIXIV_`，`__` 分隔层级）。示例见 `config.example.yaml` 与 `src/copixiv/config.py`。

配置节包括：`path`、`pixiv_client`、`proxy`、`batch_download`、`backup`、`security`、`notifications`（后端列表）。

## 8. 质量门禁

```bash
bash scripts/run_verification.sh                        # 完整本地验证脚本
.venv/bin/ruff check src tests scripts                  # 代码静态检查
.venv/bin/mypy src/copixiv                              # 类型检查
.venv/bin/lint-imports                                  # 架构边界契约 (.importlinter)
.venv/bin/alembic check                                 # ORM 模型 ↔ 迁移历史对齐检查
.venv/bin/pytest tests -v                               # unit / db / integration 测试套件
npm --prefix frontend run typecheck                     # 前端 TypeScript / Vue 校验
npm --prefix frontend run build                         # 前端构建
```

## 9. 运行与运维

```bash
python -m venv .venv && source .venv/bin/activate
pip install .
.venv/bin/python scripts/pg_dev.py start                # 启动本地 PG (127.0.0.1:5433, data/pg/pgdata)
cd frontend && npm install && npm run build && cd ..
.venv/bin/python -u main.py                             # API + 前端 :9000
```

## 10. 关键文件索引

| 文件 / 包 | 说明 |
|-----------|------|
| `src/copixiv/app.py` | 组合根：`create_app` / `mount_routers` / `mount_frontend` |
| `src/copixiv/{middleware,error_handlers,lifespan}.py` | 中间件、异常映射与生命周期装配 |
| `src/copixiv/config.py` | Pydantic 配置模型与加载器 |
| `src/copixiv/deps.py` | FastAPI DI 依赖 |
| `src/copixiv/core/{models,services,exceptions,draft}.py` | 核心实体、纯服务、域异常与草稿构建 |
| `src/copixiv/db/{models,engine,uow,write_lock,backup,base}.py` | ORM 模型、PG 引擎、UoW、压缩备份与基础仓储 |
| `src/copixiv/features/novels/repo/` | 分拆后的小说读/写/查询构造/系列仓储 |
| `src/copixiv/features/novels/search.py` | `pg_bigm` 分段 `LIKE` 构造 |
| `src/copixiv/tasks/{registry,executor,cron,manager,kernel}.py` | 任务注册、执行、Cron 调度与生命周期管理器 |
| `src/copixiv/pixiv/{client,patch,account,errors}.py` | Pixiv 防腐层 |
