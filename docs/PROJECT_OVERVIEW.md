# copixiv 项目速览

> 事实性速览，与 `MODULARITY.md`（架构边界速查）配套。
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
| Pixiv 客户端 | pixivpy3（git 直连 `upbit/pixivpy`）+ 自定义 patch（见 MODULARITY.md §M2） |
| 后端端口 | 9000（Vite dev proxy → 9000） |

## 2. 架构

Clean Architecture / 六边形风格，依赖单向。边界规则（哪些被测试钉死、
哪些是约定）见 `MODULARITY.md`：

```
web_api → application → domain ← infrastructure
                ↑
        tasks ——┘
```

| 层 | 目录 | 职责 |
|---|---|---|
| app | `src/copixiv/app/` | 组合根：`Container.build()` 装配一切，`create_app()` 工厂 |
| web_api | `src/copixiv/web_api/` | 薄 FastAPI 层：路由 + schema + 类型化 DI（`deps.py`） |
| application | `src/copixiv/application/` | 用例编排（下载/批量/删除/文件/作者解析/搜索历史） |
| domain | `src/copixiv/domain/` | Pydantic 实体、端口 Protocol、纯函数服务、领域异常 |
| infrastructure | `src/copixiv/infrastructure/` | database/repositories、pixiv、storage、epub、notifier |
| tasks | `src/copixiv/tasks/` | 任务注册表 + 调度内核 + 业务任务（`@register`） |

## 3. 目录结构

```
copixiv/
├── main.py                # uvicorn 入口 shim（真实入口 copixiv.app.main）
├── pyproject.toml         # 元数据 + 依赖
├── config.yaml            # 运行时配置（见 config.example.yaml 模板）
├── pixiv_token.py         # 账号 token（DB tokens 表的兜底来源）
├── alembic/               # 数据库迁移（versions/ 下为全部版本）
├── database/              # SQLite 数据库文件 + 周备份（database/backups/）
├── download/              # 小说文本/EPUB/封面（FileStorage 根目录）
├── frontend/              # Vue 3 前端
│   └── src/{api, components(ui|features), composables, router, types, views}
├── scripts/               # 运维/验证脚本（含批量基准）
├── docs/                  # MODULARITY.md + 本文件
├── deploy/                # systemd 等部署文件
└── tests/
    ├── architecture/      # 分层矩阵 + pixivpy3 白名单（AST 执法）
    ├── regression/        # 写锁、application 纯度等回归钉
    ├── plugins/           # 任务插件发现链路的演示件
    └── {domain,application,infrastructure,tasks,web_api}/
```

## 4. 数据库

- ORM 模型：`infrastructure/database/models.py`（novel / author / series / tag /
  novel_tag / favourite / special_follow / failed_novel / search_history /
  task_history / scheduled_tasks / tag_preferences / tag_aliases / token / setting，
  FTS5 全文索引 novel_fts）
- 迁移：Alembic（`alembic/versions/`），启动时由 `init_database()` 自动应用
- 并发：WAL + 全进程写锁（`write_lock.db_write()`），API 写端点与后台任务共享
- 仓储：读写分离（`novel_read.py` / `novel_write.py`，facade `novel.py`）

## 5. API

| 前缀 | 模块 | 说明 |
|------|------|------|
| `/api/novels` | `web_api/endpoints/novels.py` | 列表/计数/下载/收藏/批量打包/批量操作/厌恶标签排除 |
| `/api/tasks` | `endpoints/tasks.py` | 任务方法、定时任务管理、立即运行、执行历史 |
| `/api/system` | `endpoints/system.py` | 系统配置（含运行时设置） |
| `/api/tag-preferences` | `endpoints/tag_preferences.py` | 标签偏好（喜爱/屏蔽） |
| `/api/tag-aliases` | `endpoints/tag_aliases.py` | 标签别名、相似标签建议 |
| `/api/search-history` | `endpoints/search_history.py` | 搜索历史 |
| `/api/tokens` | `endpoints/tokens.py` | Pixiv 刷新令牌管理 |

- 每个端点模块自带 `ROUTE = (prefix, tags)` 清单，container 循环挂载（MODULARITY.md §M9）
- 写端点必须 `Depends(get_write_uow)`（全局写锁，见 MODULARITY.md §2.3）

## 6. 后台任务

- 声明式注册：`@register(name, description, args=Pydantic模型)`（`tasks/registry.py`）
- 发现：内置 `DEFAULT_TASK_MODULES`（novel_tasks / batch_tasks / maintenance），无第三方插件机制
- 依赖注入：`TaskContext`（uow / client / storage / epub / notifier / config / write_lock）
- 任务清单以运行时 `/api/tasks/methods` 为准（`describe_tasks()` 从 Pydantic args 模型推导，无反射）
- 内置任务模块：`novel_tasks.py`（单本/关注/作者/排行/搜索）、`batch_tasks.py`（批量操作/导出）、`maintenance.py`（FTS/EPUB/系列索引等）

## 7. 配置

加载顺序：Pydantic 默认值 → `config.yaml` → 环境变量（前缀 `COPIXIV_`，
`__` 分隔层级）。完整项与示例见 `config.example.yaml` 与 `app/config.py`。

关键节：`path`（token/database/download）、`pixiv_client`（限速/冷却/并发）、
`proxy`、`telegram`、`notifiers.enabled`（通知后端列表）、`security`
（api_key / allowed_hosts / allowed_origins）、`batch_download.naming`。

## 8. 测试

```bash
pytest -q                      # 全量
pytest tests/architecture -q   # 边界执法（矩阵 + 白名单）
pytest tests/regression -q     # 写锁 / application 纯度
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
| `src/copixiv/app/container.py` | 组合根：按域装配 + FastAPI 工厂 |
| `src/copixiv/app/config.py` | 配置模型 |
| `src/copixiv/domain/ports/` | 端口 Protocol（分层矩阵的承重墙） |
| `src/copixiv/domain/services/query_spec.py` | 查询值对象 |
| `src/copixiv/infrastructure/database/uow.py` | SqlUnitOfWork（事务边界） |
| `src/copixiv/infrastructure/database/write_lock.py` | 全进程写锁 |
| `src/copixiv/infrastructure/pixiv/{patch,account,client,accounts}.py` | Pixiv 防腐层（MODULARITY.md §M2） |
| `src/copixiv/infrastructure/notifier/{factory,composite,telegram,webhook}.py` | 通知后端 |
| `src/copixiv/tasks/registry.py` | 任务注册表（MODULARITY.md §M8） |
| `src/copixiv/web_api/deps.py` | FastAPI 依赖（唯一碰 app.state 的地方） |
| `tests/architecture/test_layering.py` | 边界执法测试 |
