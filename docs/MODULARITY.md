# copixiv 模块化设计文档

> 灵感来源：DSH「一切皆模块」。本文档是模块化改造的**唯一依据**：模块契约、边界规则、执行路线图。
> 配套：`PROJECT_OVERVIEW.md`（现状总览）、`tests/architecture/`（边界规则的代码化执行）。
> 状态：**设计定稿，待实施**。每个阶段的完成状态见文末「状态跟踪」。

---

## 1. 目标与原则

### 1.1 什么是「模块」

一个模块 = 三要素：

| 要素 | 含义 | 本项目内的表达 |
|------|------|----------------|
| **契约** | 对外接口说得清、类型化 | 端口 Protocol / Pydantic 描述符 / 公开 API 声明 |
| **生命周期** | 能独立初始化与释放 | `start/stop`、`close`、`shutdown` 钩子 + 组合根统一管理 |
| **可替换性** | 换实现或加实现的成本明确 | 三级分级（见下） |

### 1.2 可替换性三级

| 级别 | 定义 | 动作 |
|------|------|------|
| **固定** | 永远只有一个实现 | 拆边界只为维护性，不做端口多态 |
| **接口可替换** | 会换实现，但不同时存在多个 | 端口 + 防腐层（ACL） |
| **运行时可插拔** | 同时存在多个实现，用户可选/可组合/可第三方贡献 | 注册表 + 入口点 + 自描述 |

### 1.3 铁律

> 模块化是为了让**该变的地方**容易变，不是让所有地方都能变。

- 每引入一个插件机制，必须有真实的多实现/第三方贡献需求作理由
- 边界规则必须可执行（测试钉死），不靠自觉
- 全程测试绿；每个阶段同步更新本文档状态表

---

## 2. 模块清单（自底向上）

### M0 平台模块 `copixiv/log.py`　等级：固定

- **职责**：loguru 配置（三 sink）、stdlib 桥接（InterceptHandler）、任务日志捕获（capture_logs）
- **契约**：`logger` / `setup_logging` / `capture_logs` / `InterceptHandler`
- **生命周期**：`setup_logging()` 进程级一次（由入口点调用）
- **边界规则**：任何层都可 import；`app/logger.py` 降级为兼容 shim（仅供外部脚本/旧测试）
- **现状**：位于 `app/logger.py`，被 22 个文件反向依赖 → **阶段 0 迁移**

### M1 厂商边界（pixivpy3 ACL）　等级：接口可替换

- **职责**：隔离 pixivpy3 的 schema 漂移与同步实现
- **契约**：`PixivNovelPort` / `PixivAccountPort`（domain/ports）+ 自定义异常（`RateLimitError` 等）
- **边界规则**：**`import pixivpy3` 只允许出现在 `infrastructure/pixiv/patch.py` 与 `account.py`** 两个文件
- **现状**：`patch.py`（4 个 monkey patch，形态良好）+ 泄漏点：`client.py` 直接 import `PixivError`
- **动作**：`client.py` 改用自定义异常；加 import 白名单测试

### M2 Pixiv 访问模块　等级：接口可替换（全项目模块质量标杆）

- **位置**：`infrastructure/pixiv/`（account / accounts / client / patch）
- **契约（公开 API）**：`PixivClient`、`AccountPool`、`PixivAccount`、`AccountStrategy`、`TokenInfo`、异常族。其余内部符号禁止跨模块 import
- **生命周期**：`AccountPool.authenticate_all()`（启动并行预热）、client 无显式释放（semaphore 随进程）
- **现状**：已近乎自包含（零 DB/存储耦合，测试 262 行）→ 动作仅为「正式声明公开 API + 标杆化」，**阶段 0 完成**

### M3 领域契约内核（domain）　等级：固定

- **位置**：`domain/models|services|ports|exceptions`
- **契约**：Pydantic 实体（读写路径共用）、值对象、端口 Protocol、DomainError 异常族
- **缺口（阶段 2 补全）**：
  1. 读路径绕过模型返回裸 dict → 读模型补全，repo 签名改为返回领域模型
  2. `SearchConditions = list[tuple[str, str]]` 裸别名 → 查询值对象 `QuerySpec`
  3. ports 文档化分级：NotifierPort（可插拔）↔ 其余端口（接口可替换/防腐）

### M4 数据内核（infrastructure/database）　等级：固定（SQLite 是产品特性）

- **子模块**：
  - `engine.py`：引擎/PRAGMA 调优（不动）
  - `write_lock.py`：全进程写锁（不动，已有端口）
  - `repositories/`：**读写分离**（阶段 2）——`NovelReadRepository` / `NovelWriteRepository`；`fts.py` 保持独立
  - `query_builder*.py`：收编为「QuerySpec 值对象 + 单一翻译器」（阶段 2）
  - `uow.py`：组合边缘，保持
- **领域策略上移**：屏蔽标签排除（`_exclusion_active` / `_blocked_tag_names`）从 repo 移入 domain
- **明确不做**：DB 方言多态

### M5 存储 / EPUB　等级：固定

- `FileStorage`：已干净，不动
- `EpubBuilder`：输入裸 dict → 类型化输入（Novel + 路径）（阶段 2）
- `ImageDownloader`：轻拆 HTTP 下载 / 线程池调度 / EPUB 触发三职责（阶段 2）
- **明确不做**：存储后端多态（S3 等）——本地文件目录是产品形态

### M6 通知模块（插件面②）　等级：运行时可插拔

- **契约**（阶段 3 落地）：

```python
class NotifierBackend(Protocol):          # 扩展 NotifierPort
    name: str                              # 后端自述名
    async def send_task_result(task_name, status, duration=None,
                               error=None, result=None) -> None: ...
    async def close(self) -> None: ...     # 生命周期钩子
```

- **装配**：`config.yaml` 增 `notifiers: [telegram, ...]`，容器按配置实例化列表；后端注册表 = 装饰器注册（第三方扩展点留 `copixiv.notifiers` entry point，视需求启用）
- **现状**：`TelegramNotifier` 具体类 + NotifierPort 已有 → Telegram 降级为第一个后端实现

### M7 应用层（application）　等级：固定

- **契约**：一个用例 = 一个文件一个入口函数；**业务编排只活在这里**
- **动作（阶段 2）**：回收散在 endpoints / tasks 的编排残留（批量打包、范围解析等）

### M8 任务系统（插件面①，DSH 灵感主载体）　等级：运行时可插拔

- **分层**：任务内核（registry / executor / scheduler / history——通用、可独立成库）↔ 业务任务（novel_tasks / batch_tasks / maintenance——内核的用户）
- **契约（阶段 1 落地）**：

```python
# 声明式任务描述符：args 为 Pydantic 模型，元数据注册时自带
@register(
    name="novel_fetch",
    description="抓取单本小说并入库",
    args=NovelFetchArgs,          # Pydantic model；JSON 参数按此校验/转换
)
async def novel_fetch(args: NovelFetchArgs, ctx: TaskContext) -> TaskResult:
    ...
```

```python
class TaskContext:                  # 注入通道显式化——与业务参数彻底分离
    uow: UnitOfWork
    client: PixivNovelPort
    file_storage: FileStoragePort
    image_downloader: ImageDownloaderPort
    epub_builder: EpubBuilderPort
    notifier: NotifierPort
    config: AppConfig
    write_lock: WriteLockPort
    task_id: int | None
```

- **发现机制**：`importlib.metadata` entry point group **`copixiv.tasks`**——第三方 `pip install copixiv-task-xxx` 即自注册；删除 `manager.py` / `endpoints/tasks.py` 两处硬编码 import
- **现状**：`@register` + 名字注入 + TaskResult 已有（骨架良好），问题 = 反射描述符、双通道同名冲突、544 行 manager 六职责

### M9 web_api 薄适配层　等级：固定（不做路由热插拔）

- **契约**：端点 = HTTP↔用例的纯翻译；依赖走类型化 FastAPI DI（替换 `request.app.state.*` 黑盒）；schema 按端点就近归位
- **微插件化**：端点模块自述 `(router, prefix)`，container 自动挂载（与任务注册表同一心智模型）
- **具体类规则**：端点只依赖端口 + 用例；`SqlUnitOfWork` 等具体类的 import 仅允许在 `deps.py` / container / 自家实现内

### M10 组合根（app）　等级：固定

- **契约**：`create_app()` 工厂——**import `copixiv.app.main` 零副作用**；`Container.build()` 按域拆装配函数（`_build_database` / `_build_pixiv` / `_build_notifiers` / `_build_tasks`），每个域一段「装配清单」
- **配置**：config.py 按域分模型（现状已好）；运行时设置收敛为 RuntimeSettings 端口（消除 `SETTING_EXCLUDE_BLOCKED` / `EXCLUDE_BLOCKED_KEY` 双份 key）

### M11 前端契约　等级：固定

- **动作（阶段 2 收尾可选）**：openapi-typescript 从 FastAPI OpenAPI 生成 `types/`，消灭手工镜像漂移
- 其余（按域 api/、ui 组件库、路由懒加载）已达标，不动

---

## 3. 边界规则（可执行，tests/architecture 钉死）

### 3.1 分层 import 矩阵

| 导入方 ＼ 被导入 | domain | application | infrastructure | tasks | web_api | app | copixiv.log |
|---|---|---|---|---|---|---|---|
| **domain** | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ | ✓* |
| **application** | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ | ✓* |
| **infrastructure** | ✓ | ✗ | ✓ | ✗ | ✗ | ✗ | ✓ |
| **tasks** | ✓ | ✓ | ✓ | ✓ | ✗ | ✗ | ✓ |
| **web_api** | ✓ | ✓ | ✓ | ✓ | ✓ | ✗ | ✓ |
| **app** | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

\* domain / application 现状不 import logger，规则仅为未来预留。

### 3.2 厂商白名单

`import pixivpy3` 仅允许：`infrastructure/pixiv/patch.py`、`infrastructure/pixiv/account.py`。

### 3.3 具体类规则

具体实现类（`SqlUnitOfWork`、`SQLAlchemy*Repository`、`TelegramNotifier`、`FileStorage`、`PixivClient`、`EpubBuilder`、`ImageDownloader`、`TaskManagerSystem`）的 import 仅允许出现在：

1. 该实现的自家包内（实现内部互引）
2. `app/container.py`（组合根）
3. `web_api/deps.py`（FastAPI 依赖装配边缘，阶段 2 收窄）

其余一律通过端口/用例。

### 3.4 现状与目标的差距

- ✅ 已达标：domain 零泄漏、infra→application 零、application→infra 零（原 record.py 延迟 import 豁免已消灭——改为注入 UoW 工厂）、infra/web_api/tasks→app 零（logger 平台化 + config 构造注入后）
- ⏳ 待收窄（阶段 2）：web_api 端点对具体类 `SqlUnitOfWork` 的 import（§3.3 完全生效）

---

## 4. 执行路线图（L 层 × 阶段映射）

原 P0~P3 与 L0~L8 合并后的最终顺序：

| 阶段 | 名称 | 覆盖层 | 条目 |
|------|------|--------|------|
| **阶段 0** | 边界钉子（主线） | L0/L1/L7 | 0.1 logger 平台化（M0）；0.2 config 构造注入（M5/M6/M9）；0.3 main 工厂化（M10）；0.4 pixivpy3 白名单 + client 自定义异常（M1）；0.5 container 装配函数化 + M2 公开 API 声明；0.6 架构边界测试（§3.1/3.2）+ 全测试绿 |
| **阶段 1** | 任务系统 DSH 化（插件面①） | L5 | 1.1 内核/业务任务分离；1.2 声明式描述符（args=Pydantic）；1.3 TaskContext 注入通道；1.4 entry-points 自注册 + 去硬编码 import；1.5 拆 manager.py；验收：测试插件包安装即用 |
| **阶段 2** | 内核工程 | L2/L3/L4/L6/L8 | 2.1 读模型补全（M3-1）；2.2 QuerySpec 值对象（M3-2）；2.3 读写仓储分离 + 策略上移（M4）；2.4 编排回收（M7）；2.5 web_api 薄化 + 类型化 DI + schema 归位（M9）；2.6 自述式路由；2.7 storage/epub 边界化（M5）；2.8 可选：前端 OpenAPI 类型生成（M11）；验收：novel.py < 400 行、端点无业务逻辑 |
| **阶段 3** | 通知后端注册表（插件面②） | L3 | 3.1 NotifierBackend 契约；3.2 注册表 + config 装配，TG 降级为后端之一；3.3 第二个后端示范（webhook）+ 测试；验收：加渠道 = 新文件 + 一行 config |

### 不做清单（每阶段验收时对照确认未引入）

- ✗ DB 方言多态（SQLite 锁定是特性）
- ✗ 存储后端多态（S3）
- ✗ Pixiv 源替换/多源
- ✗ web 路由热插拔
- ✗ 拆多仓库/多包（除非任务插件生态证明需要）

---

## 5. 状态跟踪

| 阶段 | 条目 | 状态 |
|------|------|------|
| 0 | 0.1 logger 平台化（`copixiv/log.py`，app/logger.py 降级 shim，21 个内部文件改 import） | ✅ 完成 |
| 0 | 0.2 config 构造注入（ImageDownloader 代理、TelegramNotifier token/chat_id/proxy、system 端点走 app.state） | ✅ 完成 |
| 0 | 0.3 main 工厂化（`create_app()` 零导入副作用，root main.py shim 承接 uvicorn main:app） | ✅ 完成 |
| 0 | 0.4 pixivpy3 白名单（client.py 改用 `PixivApiError`，account.py 负责异常翻译） | ✅ 完成 |
| 0 | 0.5 container 按域拆装配函数 + M2 公开 API 声明（pixiv/__init__.py） | ✅ 完成 |
| 0 | 0.6 架构边界测试（`tests/architecture/test_layering.py` 分层矩阵 + 厂商白名单） | ✅ 完成（391 passed） |
| 0 | 附赠：record.py 的 infra 豁免点消灭（UoW 工厂注入），M10 回归测试同步收紧 | ✅ 完成 |
| 1 | 1.1 内核/业务任务分离（registry/context/executor/history/scheduler + manager 门面；manager 544→231 行） | ✅ 完成 |
| 1 | 1.2 声明式描述符（`@register(name, description, args=Pydantic模型)`；describe_tasks 纯查表，无反射） | ✅ 完成 |
| 1 | 1.3 TaskContext 注入通道（业务参数与依赖彻底分离；ctx.child_uow() 扇出） | ✅ 完成 |
| 1 | 1.4 entry-points 自注册（`copixiv.tasks` 组 + 内置回退；两处硬编码 import 已删除；真实安装验证通过） | ✅ 完成 |
| 1 | 1.5 拆 manager.py（history/executor/scheduler 各司其职；兼容 .scheduler 与显式 func 通道） | ✅ 完成 |
| 1 | 验收：测试插件包（tests/plugins/copixiv_task_demo.py + test_plugin_registry.py 发现/描述符/校验四测） | ✅ 完成（395 passed） |
| 2 | 2.1 读模型补全（读路径全部返回领域 `Novel` 模型：get_by_id/get_novels/get_novels_by_ids/随机路径；archive/filename/用例/端点全部改属性访问；`Novel` 增补 `shuffle` 字段） | ✅ 完成 |
| 2 | 2.2 QuerySpec 值对象（`domain/services/query_spec.py` 取代 9 参数汤；端口/仓储/查询构建器/端点/用例全部 spec 化；SQL 专属输入作为构建器显式参数） | ✅ 完成 |
| 2 | 2.3 读写仓储分离（novel.py 1189 行 → facade 17 行 + novel_read.py + novel_write.py） | ✅ 完成 |
| 2 | 2.3b 屏蔽标签策略上移（domain/services/exclusion.py：resolve_active + 唯一设置键，消除 system.py/repo 双份常量） | ✅ 完成 |
| 2 | 2.4 编排回收 | ⬜ 待实施 |
| 2 | 2.5 web_api 薄化 + 类型化 DI + schema 归位 | ⬜ 待实施 |
| 2 | 2.6 自述式路由 | ⬜ 待实施 |
| 2 | 2.7 storage/epub 边界化 | ⬜ 待实施 |
| 3 | 全部 | ⬜ 待实施 |

> 每完成一条：勾选 + 一行变更说明 + 关联测试。每阶段完成：全测试绿 + 更新本表。
