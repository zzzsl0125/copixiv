# copixiv 架构边界速查

> 本文件的定位：**给人和 AI 的共识边界卡，不是宪法。**
> 它只保证一件事——**哪些规则有测试钉死、哪些只是约定**，全部如实标注。
> 曾经的路线图/状态表/验收清单已删除（历史见 git log `refactor/modularity`）。

## 1. 分层

依赖方向严格单向，import 规则见 §3.1（有测试执法）：

```
web_api → application → domain ← infrastructure
        （tasks 在两者之间：允许 domain/application/infrastructure/tasks）
                 ↑
             app（组合根，允许一切）
```

| 层 | 目录 | 一句话职责 |
|---|---|---|
| app | `src/copixiv/app/` | 组合根：Container 装配一切（§M10） |
| web_api | `web_api/` | 薄 FastAPI 适配层（§M9） |
| application | `application/` | 用例编排，一个用例一个文件（§M7） |
| domain | `domain/` | Pydantic 实体 + 端口 Protocol + 纯函数服务（§M3） |
| infrastructure | `infrastructure/` | 数据库、pixiv、存储、EPUB、通知的实现（§M2/M4/M5/M6） |
| tasks | `tasks/` | 任务内核（registry/executor/scheduler）+ 业务任务（§M8） |

## 2. 硬规则（违反 = 测试变红）

### 2.1 分层 import 矩阵（§3.1）

执法：`tests/architecture/test_layering.py::test_layer_import_matrix`（AST 扫描）

| 导入方 ＼ 被导入 | domain | application | infrastructure | tasks | web_api | app | log |
|---|---|---|---|---|---|---|---|
| **domain** | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ | ✓* |
| **application** | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ | ✓* |
| **infrastructure** | ✓ | ✗ | ✓ | ✗ | ✗ | ✗ | ✓ |
| **tasks** | ✓ | ✓ | ✓ | ✓ | ✗ | ✗ | ✓ |
| **web_api** | ✓ | ✓ | ✓ | ✓ | ✓ | ✗ | ✓ |
| **app** | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

\* domain / application 现状不 import logger，规则仅为未来预留。

### 2.2 pixivpy3 厂商白名单（§3.2）

执法：`tests/architecture/test_layering.py::test_pixivpy3_vendor_whitelist`

`import pixivpy3` 只允许出现在 `infrastructure/pixiv/patch.py`、`account.py` 与
`errors.py`（异常层次必须继承 `pixivpy3.PixivError` 才能被既有 `except` 链捕获）。
其余代码一律通过 `domain/ports/pixiv.py` 的 Protocol 和 `account.py` 导出的异常族。

### 2.3 写路径必须走全局写锁

执法：`tests/regression/test_m6_m10_write_lock_and_ports.py`

- API 写端点必须声明 `Depends(get_write_uow)`；读端点用 `get_uow`。
- 后台任务写库走 `uow.begin()` / `db_write()`。SQLite 单写者，API 与后台任务共享同一把锁。

### 2.4 application 层零基础设施依赖

执法：`tests/regression/...::test_application_layer_does_not_import_infrastructure`

## 3. 约定（没有测试执法，违反不会红，发现就修）

- 具体实现类（`SqlUnitOfWork`、各 `SQLAlchemy*Repository`、`TelegramNotifier`、`FileStorage`、`PixivClient` 等）的 import 只应出现在：自家包内 / `app/container.py` / `web_api/deps.py`。
- 端点里 `SqlUnitOfWork` 只作类型注解，实例一律来自 `Depends(...)`；不得摸 `uow.session`。
- 新增 API 区域 = 一个端点模块（自带 `ROUTE = (prefix, tags)` 清单）+ container 里加一行注册。

> 反省记录（2025-08）：旧版 §3.3 声称以上"具体类规则"由测试钉死，实际从未实现执法测试。本版如实降级为约定——宪法可以有承诺，速查卡只写事实。

## 4. 模块定位表（M 编号，供代码注释引用）

| 编号 | 模块 | 定位 | 说明 |
|---|---|---|---|
| M0 | `copixiv/log.py` | 平台 | 任何层可 import；`app/logger.py` 是兼容 shim |
| M2 | `infrastructure/pixiv` | 防腐层（唯一"接口可替换"） | 公开 API 见 `pixiv/__init__.py`；异常翻译在 `account.py` |
| M3 | `domain` | 内核 | Pydantic 实体（读写共用）+ 纯服务；业务策略在 domain 不在 repo |
| M4 | `infrastructure/database` + `repositories` | 固定 | SQLite 是产品特性；读写仓储分离；`QuerySpec` 值对象 |
| M5 | `storage` / `epub` | 固定 | 本地文件目录是产品形态 |
| M6 | `notifier` | 配置驱动 | `notifiers.enabled` 选后端；`CompositeNotifier` 故障隔离 |
| M7 | `application` | 用例 | 只保留有真实编排的用例；CRUD 由端点直连仓库 |
| M8 | `tasks` | 注册表 | `@register(name, args=Pydantic模型)` + `TaskContext` 注入；`DEFAULT_TASK_MODULES` 内置发现 |
| M9 | `web_api` | 薄适配 | `deps.py` 是唯一碰 `app.state` 的地方；ROUTE 自述清单 |
| M10 | `app/container` | 组合根 | 按域 `_build_*` 装配；`create_app()` import 零副作用 |

## 5. domain/ports 的真实定位

端口 Protocol（约 370 行、零运行时开销）有两个作用，按重要性排序：

1. **分层矩阵的承重墙**：application/tasks 被禁止 import infrastructure（§2.1），端口是它们引用这些能力的唯一合法通道；
2. **类型契约文档**：标注方法签名，供 IDE / 类型检查器。

它们**不是**"可替换性"承诺：除 pixiv（M2）外全部端口只有一个实现，测试也不构造端口的替代实现（fake 均为鸭子类型）。新增端口前先问一句：是否真有第二个实现或跨层引用需求？

## 6. 明确不做（除非出现真实需求，不重新打开）

- DB 方言多态（SQLite 锁定是特性）
- 存储后端多态（S3 等）
- Pixiv 源替换 / 多源
- web 路由热插拔
- 任务 / 通知的第三方插件生态（任务发现只走内置 `DEFAULT_TASK_MODULES`，无 entry point 机制）

## 7. 配套文档

- `PROJECT_OVERVIEW.md`：项目事实速览（技术栈、目录、API、运行方式）
- `README.md`：用户侧文档
