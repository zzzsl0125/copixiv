# copixiv 项目速览

> 事实性速览，与 `MODULARITY.md`（目录边界速查）配套。
> 原则：**只写不易漂移的结构性内容**；计数类信息（测试数、迁移数、行数）
> 一律以工具输出为准，不在此维护。

## 1. 基本信息

| 维度 | 内容 |
|------|------|
| 定位 | Pixiv 小说下载管理器 v2 重写版 |
| 版本 | 2.0.0（`pyproject.toml`） |
| 后端 | Python 3.12+，FastAPI + SQLAlchemy 2.0 + APScheduler + Alembic + loguru |
| 前端 | Vue 3 + Vite + TypeScript + Tailwind v4 + Vitest（`frontend/package.json`） |
| 数据库 | SQLite（WAL 模式），schema 兼容 v1 |
| Pixiv 客户端 | pixivpy3（git 直连 `upbit/pixivpy`）+ 自定义 patch（见 MODULARITY.md `pixiv/`） |
| 后端端口 | 9000（Vite dev proxy → 9000） |

## 2. 架构

模块化单体：纯核心 + 适配层 + 任务内核 + 按功能切片。依赖规则（哪些是约定、
哪些有测试执法）见 `MODULARITY.md`：

```
app.py（组合根：create_app / lifespan / 中间件 / 异常映射 / 挂载路由）
   │
   ▼
features/*  +  tasks/*         业务切片 + 任务内核
   │
   ▼
core/                          纯 Python（实体 / 服务 / 异常，零依赖）

app.py → features(+tasks) → core；db / pixiv / storage / notify 是适配层，
只被 features / tasks / app 使用，彼此不互相 import。
```

| 目录 | 职责 |
|------|------|
| `app.py` | 组合根：`create_app()` 工厂 + lifespan + 中间件 + 异常映射 |
| `deps.py` | FastAPI 依赖（类型化 DI） |
| `features/` | 按功能切片：api + repo + schemas 同置 |
| `tasks/` | 任务注册表 + 调度内核 + 业务任务 |
| `core/` | 纯 Python：Pydantic 实体、纯服务、域异常 |
| `db/` | SQLite 唯一适配：engine / uow / write_lock |
| `pixiv/` | pixivpy3 防腐层（唯一厂商边界） |
| `storage/` | 文件存储 / 图片下载 / EPUB |
| `notify/` | 通知后端 |

## 3. 目录结构

```
src/copixiv/
├── app.py          # 组合根：create_app / lifespan / 中间件 / 异常映射 / 启动项 / main()
├── config.py       # 配置模型与加载
├── deps.py         # FastAPI 依赖（读 app.state 单例）
├── log.py          # 日志
├── core/           # 纯 Python：models.py / services.py / exceptions.py
├── db/             # engine / uow / write_lock / backup / models / constants / base
├── pixiv/          # pixivpy3 防腐层（唯一允许 import pixivpy3 的目录）
├── storage/        # file_storage / image_downloader / epub
├── notify/         # telegram / webhook / composite / factory
├── tasks/          # kernel.py（registry+context+history+executor+scheduler+manager）+ novels / batch / maintenance / pipeline / api / schemas / history_repo
└── features/       # accounts / authors / failures / novels / system / tags（api + repo + schemas）
```

项目根下还有 `main.py`（uvicorn 入口 shim，真实入口 `copixiv.app.py`）、
`pyproject.toml`、`config.yaml`（模板 `config.example.yaml`）、`pixiv_token.py`、
`alembic/`、`database/`（SQLite 文件 + 周备份）、`download/`（FileStorage 根目录）、
`frontend/`（Vue 3）、`scripts/`、`docs/`、`deploy/`（systemd 等）、`tests/`。

`tests/` 下分 `architecture/`（pixivpy3 白名单 AST 执法）与 `regression/`
（写锁、删除路径等回归钉），及各功能的测试目录。

## 4. 数据库

- ORM 模型：`db/models.py`（novel / author / series / tag / novel_tag /
  favourite / special_follow / failed_novel / search_history / task_history /
  scheduled_tasks / tag_preferences / tag_aliases / token / setting，
  FTS5 全文索引 novel_fts）
- 迁移：Alembic（`alembic/versions/`），启动时由 `init_database()` 自动应用
- 并发：WAL + 全进程写锁（`db/write_lock.db_write()`），API 写端点与后台任务共享
- 仓储：按功能归位（novel 读写合并进 `features/novels/repo.py`）

## 5. API

| 前缀 | 模块 | 说明 |
|------|------|------|
| `/api/novels` | `features/novels/api.py` | 列表/计数/下载/收藏/批量打包/批量操作/厌恶标签排除 |
| `/api/tasks` | `tasks/api.py` | 任务方法、定时任务管理、立即运行、执行历史 |
| `/api/system` | `features/system/api.py` | 系统配置（含运行时设置） |
| `/api/tag-preferences` | `features/tags/preferences.py` | 标签偏好（喜爱/屏蔽） |
| `/api/tag-aliases` | `features/tags/aliases.py` | 标签别名、相似标签建议 |
| `/api/search-history` | `features/novels/history_api.py` | 搜索历史 |
| `/api/tokens` | `features/accounts/api.py` | Pixiv 刷新令牌管理 |

- 每个路由模块自带 `ROUTE = (prefix, tags)` 清单，由 `app.py` 的 `create_app()`
  统一 `include_router` 挂载（见 MODULARITY.md）
- 写端点必须 `Depends(get_write_uow)`（全局写锁，见 MODULARITY.md 硬规则 §3.2）

## 6. 后台任务

- 任务内核：`tasks/kernel.py`（registry + context + history_recorder +
  executor + scheduler + manager 合并）
- 声明式注册：`@register(name, args=Pydantic模型)`；发现：内置
  `DEFAULT_TASK_MODULES`（novels / batch / maintenance），无第三方插件机制
- 依赖注入：`TaskContext`（uow / client / storage / epub / notifier / config / write_lock）
- 任务清单以运行时 `/api/tasks/methods` 为准（`describe_tasks()` 从 Pydantic
  args 模型推导，无反射）
- 内置业务任务：`tasks/novels.py`（单本/关注/作者/排行/搜索）、`tasks/batch.py`
  （批量操作/导出）、`tasks/maintenance.py`（FTS/EPUB/系列索引等）

## 7. 配置

加载顺序：Pydantic 默认值 → `config.yaml` → 环境变量（前缀 `COPIXIV_`，
`__` 分隔层级）。完整项与示例见 `config.example.yaml` 与 `src/copixiv/config.py`。

关键节：`path`（token/database/download）、`pixiv_client`（限速/冷却/并发）、
`proxy`、`telegram`、`notifiers.enabled`（通知后端列表）、`security`
（api_key / allowed_hosts / allowed_origins）、`batch_download.naming`。

## 8. 测试

```bash
pytest -q                      # 全量
pytest tests/architecture -q   # 边界执法（pixivpy3 白名单）
pytest tests/regression -q     # 写锁 / 删除路径等回归钉
pytest -m "not slow"           # 快速门禁
```

配置在 `pyproject.toml`：`asyncio_mode = auto`、`pythonpath = ["src"]`。

## 9. 运行

```bash
python -m venv .venv && source .venv/bin/activate
pip install .
python main.py                 # 后端 :9000

cd frontend && npm install && npm run build && npm run preview   # 前端 :4173
```

## 10. 关键文件索引

| 文件 | 说明 |
|------|------|
| `src/copixiv/app.py` | 组合根：create_app / lifespan / 中间件 / 异常映射 |
| `src/copixiv/config.py` | 配置模型与加载 |
| `src/copixiv/deps.py` | FastAPI 依赖（读 app.state 单例） |
| `src/copixiv/core/models.py` | 全部 Pydantic 实体 |
| `src/copixiv/core/services.py` | 纯函数服务（parsing / filename / language / tags / exclusion / archive / query_spec 等） |
| `src/copixiv/core/exceptions.py` | 域异常（无 status_code，映射在 app.py） |
| `src/copixiv/core/draft.py` | NovelDraft 纯数据结构 + 写路径工厂（build_novel / build_from_webview / build_from_novel_info） |
| `src/copixiv/db/uow.py` | SqlUnitOfWork（纯事务边界） |
| `src/copixiv/db/write_lock.py` | 全进程写锁（db_write） |
| `src/copixiv/features/novels/repo.py` | novel 读写仓储 |
| `src/copixiv/tasks/kernel.py` | 任务注册表 + 调度内核 + 上下文 |
| `src/copixiv/pixiv/{client,patch,account,errors}.py` | Pixiv 防腐层（MODULARITY.md `pixiv/`） |
| `tests/architecture/test_vendor_whitelist.py` | pixivpy3 厂商白名单执法 |
| `tests/regression/test_m6_write_lock.py` | 写锁纪律执法 |
