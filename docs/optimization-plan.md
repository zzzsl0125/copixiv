# copixiv-v2 模块优化计划

> 基于 2026-07-07 全项目代码审查。之前在任务系统、通知系统上已完成一轮优化（引入 `TaskResult`、移除 config 管道、DI 安全化、通知按类型区分）。

## 项目结构

```
src/copixiv/
├── domain/           # 模型、端口（协议）、纯服务
├── application/      # 用例/编排层  ← 🔴 全部是死代码
├── infrastructure/   # 具体实现（DB、Pixiv、存储、EPUB）
├── web_api/          # FastAPI 端点 + schemas
├── tasks/            # 后台任务系统  ← ✅ 已优化
└── app/              # 配置、容器、日志
```

---

## 优先级 1 — `application/novel/` 全部是死代码

4 个文件，4 个 UseCase 类，**零引用**——全局 grep 无任何 import。容器 `container.py` 也未创建或注入。

| 文件 | 包含 | 状态 |
|---|---|---|
| `application/novel/batch_download.py` | `BatchDownloadUseCase` | 死代码，逻辑在 `web_api/endpoints/novels.py:155` 内联重复 |
| `application/novel/download_novel.py` | `DownloadNovelUseCase` | 死代码 |
| `application/novel/list_novels.py` | `ListNovelsUseCase` | 死代码 + 贫血（纯透传，无业务逻辑） |
| `application/novel/toggle_favourite.py` | `ToggleFavouriteUseCase` | 死代码 + 一行委托 |

**行动：**

- [ ] 删除 `application/novel/` 整个目录（包括 `__init__.py`）
- [ ] 或者：保留目录结构，但把端点里的内联业务逻辑迁入 UseCase，然后从容器注入。推荐后者——把以下逻辑迁出端点：
  - `novels.py:54-84` 搜索历史记录 → 迁入 SearchHistoryService 或专用 UseCase
  - `novels.py:155-193` 批量下载 → 使用已有的 `BatchDownloadUseCase`
  - `novels.py:196-208` 删除小说时的文件清理 → `DownloadNovelUseCase` 或 `DeleteNovelUseCase`

---

## 优先级 2 — `web_api/` 业务逻辑泄漏 + 重复定义

### 2.1 重复/死代码（bug）

| 问题 | 文件 | 行号 |
|---|---|---|
| **完全重复的端点** — `reorder_tokens` 定义了两次，相同的路由 `POST /reorder/` | `endpoints/tokens.py` | 42-53 |
| **重复的函数** — `_parse_json_str` 定义两次，第二个覆盖第一个 | `schemas.py` | 103-120 |
| **重复的 validator 注册** — `_parse_config` 和 `_parse_params` 各注册两遍 | `schemas.py` | 127-131, 145-149 |
| **重复 import** — `Request` 导入了两次 | `endpoints/tasks.py` | 5 |

**行动：**
- [ ] `tokens.py`: 删除第一个 `reorder_tokens` 定义（保留第二个）
- [ ] `schemas.py`: 删除第一个 `_parse_json_str`，删除多余的 validator 注册行
- [ ] `tasks.py`: 删除多余的 `Request` import

### 2.2 业务逻辑泄漏到路由

| 问题 | 文件 | 行号 |
|---|---|---|
| 搜索历史记录逻辑（30+ 行，含 session 创建、名称解析、错误处理） | `endpoints/novels.py` | 54-84 |
| 批量下载逻辑（40 行，含 repo 实例化、zip 构建、文件名组装、响应头） | `endpoints/novels.py` | 154-193 |
| 文件删除直接实例化 `FileStorage` | `endpoints/novels.py` | 196-208 |
| tag alias 创建中的事务管理 + domain 操作 + try/except | `endpoints/tag_aliases.py` | 40-54 |

**行动：**
- [ ] 将搜索历史逻辑提取为独立函数或服务
- [ ] 批量下载使用/重构 `BatchDownloadUseCase`
- [ ] 删除小说端点通过 DI 获取 `FileStorage`，不自行实例化
- [ ] tag alias 的业务逻辑迁入 service 层

### 2.3 Schema 类型问题

| 问题 | 文件 | 行号 |
|---|---|---|
| **ORM 模型泄露** — `TagPreferenceResponse.preference` 直接引用 SQLAlchemy `TagPreferenceORM` | `schemas.py` | 14 |
| **bool 用 int** — `has_epub: int`, `is_favourite: int`, `is_special_follow: int` | `schemas.py` | 67-69 |
| **裸 `dict` 输入** — tag_preferences 和 tag_aliases 端点不接受 Pydantic schema | `endpoints/tag_preferences.py` | 19, 25 |
| 缺少输入校验 — `order_by` 无 allowlist、`per_page` 无上限、`format_mode` 无 Literal | `schemas.py` | 各处 |

**行动：**
- [ ] `schemas.py`: `TagPreferenceResponse` 改用 Pydantic 模型而非 ORM 类
- [ ] `schemas.py`: `has_epub` → 改为 `EpubStatus` enum；`is_favourite`/`is_special_follow` → `bool`，加 `field_validator` 从 int 转换
- [ ] `schemas.py`: 为 tag preferences 创建 `TagPreferenceCreate`/`TagPreferenceUpdate` schema
- [ ] `schemas.py`: `order_by` → `Literal[...]`；`order_direction` → `Literal["ASC", "DESC"]`；`per_page` → `Field(ge=1, le=200)`；`format_mode` → `Literal["txt", "prefer_epub"]`

### 2.4 其他

| 问题 | 文件 | 行号 |
|---|---|---|
| `system.py` HTTP 传输 sudo 密码 | `endpoints/system.py` | 47-70 |
| `system.py` `RestartRequest` 内联定义，不在 `schemas.py` | `endpoints/system.py` | 24-25 |

**行动：**
- [ ] 考虑用 token-based 或 key-file 认证替代 sudo 密码传输
- [ ] 将 `RestartRequest` 移至 `schemas.py`

---

## 优先级 3 — `domain/models/` 类型质量

### 3.1 普遍问题

| 问题 | 涉及 model | 字段 |
|---|---|---|
| **datetime 存为 `str`** | Novel, Author, SearchHistory, TaskHistory | `create_time`, `last_update`, `timestamp`, `start_time` |
| **int 当 bool** | Novel | `is_favourite: int`, `is_special_follow: int` |
| **魔数代替 enum** | Novel | `has_epub: int  # 0=no, 1=pending, 2=done` |

**行动：**
- [ ] `Novel.create_time`, `Author.last_update`, `SearchHistory.timestamp`, `TaskHistory.start_time/end_time` → `datetime | None`
- [ ] `Novel.is_favourite`, `Novel.is_special_follow` → `bool`，OR 层加 int↔bool 转换
- [ ] 创建 `EpubStatus(IntEnum)` — `NO=0, PENDING=1, DONE=2`，用于 `Novel.has_epub`

### 3.2 未使用的 Pydantic 模型

以下 domain Pydantic 模型在项目中**零引用**（只在 `__init__.py` 中 re-export，无其他 import）：

- `NovelTag`
- `Favourite`
- `SpecialFollow`

它们的 ORM 版本存在于 `infrastructure/database/models.py`，且全部代码都使用 ORM 版本。Domain 的 Pydantic 版本是无用代码。

**行动：**
- [ ] 确认无引用后删除 `NovelTag`、`Favourite`、`SpecialFollow`
- [ ] 同理检查 `FailedNovel`、`ProcessedPeriod`、`NovelEpubConversion`

### 3.3 `TaskResult` 不一致

`TaskResult` 是项目中唯一的 `@dataclass`，其他 model 全是 `BaseModel`。

**行动：**
- [ ] 将 `TaskResult` 改为 `BaseModel`（保持字段不变，改基类）
- [ ] `__post_init__` 逻辑改为 `@field_validator` 或 `model_validator`

---

## 优先级 4 — `infrastructure/repositories/` 不一致 + 重复

### 4.1 重复方法

| 问题 | 文件 | 行号 |
|---|---|---|
| `reorder` 方法定义两次（一模一样的 body） | `repositories/token.py` | 48-60 |
| `_row_to_dict` / `model_to_dict` 重复 4 次 | base.py:15, novel.py:422, author.py:37(内联), series.py:34(内联) |
| `get_by_id` 在不同 repo 中返回类型不一致 | 各 repo | — |

**行动：**
- [ ] `token.py`: 删除重复的 `reorder`
- [ ] 将 `model_to_dict` 提取到 `base.py` 作为唯一实现，所有 repo 共用
- [ ] 统一 `get_by_id` 返回 `dict | None`（所有 repo 都遵循一个约定）

### 4.2 不恰当的抽象

| 问题 | 文件 | 行号 |
|---|---|---|
| `BaseRepository._update_summary` 硬编码 `models.Novel`，却放在基类中 | `base.py` | 70-75 |
| `_apply_cursor` 只支持 DESC 排序，硬编码 `<` | `query_builder.py` | 139-143 |

**行动：**
- [ ] `_update_summary` 只被 Author 和 Series repo 使用——移到具体子类或提取为独立 helper
- [ ] `_apply_cursor` 根据 `order_direction` 选择 `>` 或 `<`

### 4.3 安全 + 性能

| 问题 | 文件 | 行号 |
|---|---|---|
| FTS 查询字符串拼接进原始 SQL（单引号未转义） | `query_builder.py` | 451, 461 |
| `upsert_novels` 87 行，应拆分 | `novel.py` | 145-231 |
| `fts.py` O(n) 逐行 INSERT/DELETE，应批量 | `fts.py` | 97-108, 304-307 |

**行动：**
- [ ] FTS MATCH 字符串对单引号转义
- [ ] `upsert_novels` 拆分为：tag 解析、existing-ID 批量查、field 过滤、upsert 执行、FTS 更新
- [ ] FTS 批量操作改用 `executemany`

---

## 优先级 5 — 零散 bug 和小优化

### 5.1 确定是 bug

| 问题 | 文件 | 行号 |
|---|---|---|
| `uow.py` `begin()` 的 `finally` 块里清理代码重复（同段写两遍） | `database/uow.py` | 138-147 |
| `client.py` 重复的 `except AccountInvalidError`（第二个不可达） | `pixiv/client.py` | 205-212 |

**行动：**
- [ ] `uow.py`: 删除重复段
- [ ] `client.py`: 删除不可达的 except

### 5.2 资源管理

| 问题 | 文件 | 行号 |
|---|---|---|
| `httpx.AsyncClient` 每次请求新建一个（无连接复用） | `notifier/telegram.py` | 129, 149 |
| `engine.py` backup 时临时 engine 不 dispose | `database/backup.py` | 44-46 |
| `ImageDownloader` 的 ThreadPoolExecutor 依赖 `shutdown()` 才释放 | `storage/image_downloader.py` | 39 |
| `epub/builder.py` 用 `__import__("re")` 而不是 `import re` | `epub/builder.py` | 16 |

**行动：**
- [ ] `TelegramNotifier`: 创建实例级 `httpx.AsyncClient`，加 `close()` 方法
- [ ] `backup.py`: `finally` 中 `engine.dispose()`
- [ ] `ImageDownloader`: 加 `__del__` 或 `atexit` 兜底
- [ ] `builder.py`: `import re` at module level

### 5.3 其他

| 问题 | 文件 | 行号 |
|---|---|---|
| `patch.py` 4 个 patch 函数有相同的 try/except 包装器 | `pixiv/patch.py` | 各处 |
| `account.py` 用 `time.time()` 而非 `time.monotonic()` 做 TTL 检查 | `pixiv/account.py` | 86, 139 |
| `config.py` Config 在 import 时加载（`SystemExit`）— 测试无法导入 | `app/config.py` | 139 |
| `system.py` HTTP 传输 sudo 密码 | `web_api/endpoints/system.py` | 47-70 |

**行动：**
- [ ] `patch.py`: 提取 `@safe_patch` 装饰器，消除 4 次重复
- [ ] `account.py`: `time.time()` → `time.monotonic()`
- [ ] Config 延迟加载（用函数代替模块级 singleton）
- [ ] 评估 system restart 的安全风险

---

## 建议执行顺序

按投入产出比排列：

1. **删掉 `application/novel/`**（或完成重构）——死代码；保留则把端点内联逻辑迁入
2. **`web_api/schemas.py` + 端点** ——修重复 validator/函数、补 typed schema、bool/int 修正
3. **`domain/models/`** ——datetime 替换 str、魔数→enum、清理未使用的 Pydantic 模型
4. **`infrastructure/repositories/`** ——合并重复方法、统一返回类型、拆分大方法、FTS 安全修复
5. **零散 bug** ——uow/client 重复块、telegram 连接复用、config 延迟加载

---

*分析日期: 2026-07-07*
*前序优化: 任务系统（TaskResult）、通知系统（HTML parse mode、类型感知）、config 管道清理、DI 安全化*
