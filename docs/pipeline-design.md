# 任务流水线重构设计

## 1. 现状分析

### 1.1 当前架构

```
ScheduledTask / API 手动触发
  │
  ▼
_run_task_wrapper()  [30min timeout, log capture]
  │  注入 client, uow, file_storage, image_downloader, config
  ▼
任务函数 (author_fetch / novel_ranking / novel_search / …)
  │
  ├── 翻页型 (novel_follow / author_fetch):
  │     │
  │     ▼
  │   PixivClient._paginate()          ← 串行翻页（游标分页）
  │     │
  │     └── 每页 fire asyncio.Task ──→ _make_page_handler()
  │                                       │  独立 session per page
  │                                       ▼
  │                                     _batch_handle()
  │                                       ├── get_existing_ids()
  │                                       ├── 已存在 → _batch_upsert(metadata only)
  │                                       └── 新 ID   → _download_novels()
  │                                            │  asyncio.gather(全部)
  │                                            │  全等 ← 耦合点 A
  │                                            └── _batch_upsert()
  │                                                  │  _db_write_lock
  │                                                  │  ← 耦合点 B
  │                                                  └── commit (在锁内)
  │
  └── 发现型 (novel_ranking / novel_search):
        │
        ▼
      _fan_out_author_fetch()
        │  asyncio.gather(N 个 author_fetch)
        │  每个 author_fetch 内部：翻页 → page handler → _batch_handle
        │  所有 author_fetch 的 handler 共享 _db_write_lock ← 耦合点 D
        └── ...
```

### 1.2 四个耦合点

| 耦合点 | 位置 | 表现 |
|--------|------|------|
| **A: 下载→入库** | [pipeline.py:144-147](src/copixiv/tasks/pipeline.py#L144-L147) | `asyncio.gather` 等同一页所有小说下载完才入库。1 本慢则整批等 |
| **B: 页→页争锁** | [pipeline.py:88](src/copixiv/tasks/pipeline.py#L88) | `_db_write_lock` 串行化所有 page handler 的写操作。多页并发 handler 排队等锁 |
| **C: 翻页→下载** | [client.py:144](src/copixiv/infrastructure/pixiv/client.py#L144) | 翻页 API 调用和内容下载共享 `Semaphore(5)`，抢占同一信号量 |
| **D: fan-out 争锁放大** | [novel_tasks.py:63-73](src/copixiv/tasks/novel_tasks.py#L63-L73) | N 个 `author_fetch` 并发 → N 条翻页流水 → N 套 page handler → 全部抢 `_db_write_lock` |

### 1.3 三个硬约束导出流水线拓扑

| 约束 | 原因 | 导出 |
|------|------|------|
| 翻页串行 | Pixiv 游标分页，需 `next_url` | 1 个 Crawler |
| API ≤5 并发 | `Semaphore(5)` 全局限流 | 最多 5 个 Download Worker |
| SQLite 单写者 | WAL 模式也不允许多写者 | 1 个 Store Worker |

---

## 2. 关键设计决策

### 2.1 流水线粒度：任务级别，非全局

每条流水线的生命周期绑定到一个**处理型任务**（`author_fetch`、`novel_follow`）。不再有全局常驻的流水线服务。

**发现型任务**（`novel_ranking`、`novel_search`）不直接走流水线——它们只翻页收集作者 ID，然后通过任务调度器提交 N 个 `author_fetch` 子任务：

```
novel_ranking (发现)
  │
  │  翻页 → 过滤中文 → 提取作者 ID 集合
  │
  └── 对每个作者 ID:
        task_manager.run_task("author_fetch", author_id=...)
        │
        ▼
      author_fetch (处理)
        │  ① 查 author 表 → 新作者则 user_detail 拿名称 → INSERT 占位
        │  ② account_rule 包裹
        │  ③ 启动流水线
        │  ④ 翻页 → 路由 → 下载 → 入库
        │  ⑤ 关闭流水线
        │  ⑥ 更新 author 统计
```

这解决了耦合 D：fan-out 不再是任务内部 `asyncio.gather(N 个 author_fetch)`，而是任务调度层面的多个独立任务。每个 `author_fetch` 拥有自己的流水线，互不干扰。

### 2.2 作者名：流水线启动前解决

新作者（库内无记录）的名称在流水线启动**之前**查好并写入 author 表。流水线处理小说时，author 行已存在、名称已填充。

现有的 `AuthorRepository` 已有 `ensure_exists`（INSERT OR IGNORE 占位行）和 `update_author_name`（更新 author + novel 表中的名称），组合使用即可，不需要新增方法。

```
author_fetch(author_id):
  ┌─ 阶段 0: 准备（流水线启动前）
  │    async with uow.begin():
  │      author = await uow.authors.get_by_id(author_id)
  │      if not author:
  │        # 新作者：关注 + 获取名称 + 占位
  │        async with client.account_rule(force_account=_account("follow", config)):
  │          await client.user_follow_add(author_id)
  │        detail = await client.user_detail(author_id)     ← API 调用
  │        name = safe_get(safe_get(detail, "user", {}), "name", "")
  │        uow.authors.ensure_exists({author_id})           ← 现有方法
  │        await uow.authors.update_author_name(author_id, name)  ← 现有方法
  │    # commit 在 uow.begin() 退出时自动执行
  │
  ┌─ 阶段 1: 流水线（翻页 → 下载 → 入库）
  │    ...
  │
  └─ 阶段 2: 收尾（流水线结束后）
       async with uow.begin():
         await uow.authors.update_last_update(author_id)
```

流水线保证执行顺序：阶段 0 完成（author 行已存在）→ 阶段 1 中的 store worker 才开始处理第一本小说。所以 store worker 处理任意小说时，其 author 已就位。

对于老作者：名称已在库中，无需额外处理。`upsert_novels` 中 novel 的 `author_name` 字段可以从 novel data 或 author 表回填。

### 2.3 `account_rule`：ContextVar + 任务级流水线

当前 v2 的 `account_rule` 直接修改 `pool` 的全局状态（set/reset）。v1 使用的是 `ContextVar`：

```python
# v1 实现 (~/copixiv/core/pixiv_client.py)
self._strategy: ContextVar[AccountStrategy] = ContextVar(
    'strategy', default=AccountStrategy()
)

@asynccontextmanager
async def account_rule(self, need_premium=False, force_account=None):
    token = self._strategy.set(AccountStrategy(need_premium, force_account))
    try: yield self
    finally: self._strategy.reset(token)

# _call 中：
strategy = self._strategy.get()  # 当前 task 的策略
# → 传给 pool.select(strategy)
```

**为什么用 ContextVar 而不是 pool 全局状态：**

`asyncio.create_task()` 会自动把父协程的 ContextVar 复制到子协程。这意味着流水线 worker 无需任何额外代码就能继承任务的策略：

```python
@register("novel_follow")
async def novel_follow(*, client, file_storage, image_downloader,
                       uow, config, **_):
    async with client.account_rule(force_account=_account("follow", config)):
        # ContextVar 已设为 force_account="follow"

        store_pool = WorkerPool("store", _store_batch, concurrency=1,
                                maxsize=200, batch_size=10)
        dl_pool = WorkerPool("dl", _download_one, concurrency=5,
                             output_q=store_pool.input_q, maxsize=100)
        await store_pool.start()
        await dl_pool.start()
        # ↑ create_task 自动继承 ContextVar = force_account("follow")

        async for page in client.novel_follow_pages(fetch_til=...):
            # ...路由到 dl_pool.input_q / store_pool.input_q

        await dl_pool.input_q.join()
        await dl_pool.stop()
        await store_pool.input_q.join()
    # 退出 context → ContextVar 自动恢复
```

对比两种方案：

| 方案 | 并发的不同策略 | worker 继承策略 | 复杂度 |
|------|:---:|:---:|------|
| pool 全局状态 (v2 当前) | ❌ 互斥 | N/A（无并发） | 低，但不正确 |
| ContextVar (v1) | ✅ task-local | ✅ `create_task` 自动继承 | 低，且正确 |
| ContextVar + 全局常驻 pipeline | ✅ | ❌ worker 在 app 启动时创建，不在 task context 内 | 需策略传递机制 |

**结论**：ContextVar + 任务级流水线是最优组合。v1 已验证 ContextVar 模式，worker 自动继承策略，无需在 queue item 中携带策略、无需 worker 感知策略变更。即使将来支持任务并发，这套设计也天然正确。

---

## 3. 流水线拓扑（单任务内）

```
                        ┌─────────────────────────┐
                        │        Crawler           │  1 个协程（任务函数内）
                        │  翻页取元数据 → 过滤中文  │
                        │  查只读DB区分新旧 → 路由  │
                        └─────┬────────────┬──────┘
                  new IDs      │            │  existing IDs
              ┌────────────────┘            └────────────────┐
              ▼                                              ▼
   ┌──────────────────┐                          ┌──────────────────┐
   │   download_q     │                          │    store_q       │
   │   asyncio.Queue  │                          │   asyncio.Queue  │
   │   maxsize=100    │                          │   maxsize=200    │
   └────────┬─────────┘                          └────────┬─────────┘
            │                                             │
   ┌────────┴────────┐                                    │
   │ Download Workers │  最多 5 个                         │
   │ 通过 client 共享  │  client.webview_novel             │
   │ Semaphore(5)     │  下载文本/图片/EPUB               │
   │ 下载完即推store_q│                                    │
   └────────┬────────┘                                    │
            │  下载完的 NovelData                           │
            └──────────────────────┬──────────────────────┘
                                   ▼
                          ┌──────────────────┐
                          │   Store Worker   │  1 个
                          │  mini-batch upsert│  攒 10 本或 2s flush
                          │  唯一写 DB 路径   │  无 _db_write_lock
                          └──────────────────┘
```

### 3.1 解耦效果

| 耦合 | 当前 | 流水线 | 机制 |
|------|------|--------|------|
| A: 下载→入库 | gather 全等 | 单本即推 | `asyncio.Queue` 解耦 |
| B: 页→页争锁 | `_db_write_lock` | **消失** | 单 Store Worker 天然串行 |
| C: 翻页→下载 | 共享 Semaphore(5) | **保留**（可接受） | client 级别 Semaphore 仍是正确的全局限流 |
| D: fan-out 放大 | N 个 author_fetch 并发 | **消失** | 发现型任务只收集 ID，通过调度器提交独立子任务 |

---

## 4. 组件详细设计

### 4.1 翻页生成器（Client 改造）

当前 `_paginate` 和 handler 耦合。需要提取一个不负责 handler 调度的纯生成器。不新增公开方法，而是提取核心翻页逻辑为 `_paginate_pages`，让现有的 `_paginate` 和新的流水线模式共用：

```python
# client.py — 提取翻页循环核心
async def _paginate_pages(self, method: str, **kwargs
                          ) -> AsyncIterator[list[dict]]:
    """Yield each page as a list of novel dicts. 不负责 handler 调度。"""
    fetch_til = kwargs.pop("fetch_til", None)
    fetch_minlike = kwargs.pop("fetch_minlike", None)

    async with self._semaphore:
        account = self.pool.select()
        try:
            result = await account.execute(method, **kwargs)
        except RateLimitError:
            account.start_cooldown()
            account = self.pool.select()
            result = await account.execute(method, **kwargs)
        except AccountInvalidError:
            account = self.pool.select()
            result = await account.execute(method, **kwargs)

    if result is None:
        return

    yield safe_get(result, "novels", [])
    novels_count = len(safe_get(result, "novels", []))

    while result.next_url:
        next_qs = account.api.parse_qs(result.next_url)
        async with self._semaphore:
            try:
                next_result = await account.execute(method, **next_qs)
            except RateLimitError:
                account.start_cooldown()
                account = self.pool.select()
                next_result = await account.execute(method, **next_qs)
            except AccountInvalidError:
                account = self.pool.select()
                next_result = await account.execute(method, **next_qs)

        result.next_url = safe_get(next_result, "next_url")
        page_novels = safe_get(next_result, "novels", [])
        yield page_novels
        novels_count += len(page_novels)

        if page_novels and self._should_stop(
            page_novels[-1], fetch_til, fetch_minlike
        ):
            break
```

现有的 `_paginate(handler=...)` 改为内部调用 `_paginate_pages`，handler 逻辑移到外层：

```python
async def _paginate(self, method, result, handler, fetch_til, fetch_minlike):
    """保留旧接口兼容。改为调用 _paginate_pages + handler 调度。"""
    handler_tasks = []
    page = 1
    async for novels in self._paginate_pages(
        method, fetch_til=fetch_til, fetch_minlike=fetch_minlike, **kwargs
    ):
        if handler:
            handler_tasks.append(asyncio.create_task(handler(novels)))
        page += 1

    if handler_tasks:
        flat = await asyncio.gather(*handler_tasks)
        result.handler_results = [item for sublist in flat for item in (sublist or [])]
    return result
```

### 4.2 Crawler（任务函数内的协程）

Crawler 不是一个独立 class，而是任务函数中直接编写的 `async for` 循环——足够简单，不需要额外抽象：

```python
from copixiv.infrastructure.repositories.novel import NovelRepository

# 在 author_fetch 内部
read_session = uow._session_factory()
try:
    novel_repo = NovelRepository(read_session)
    async for page in client.user_novels_pages(author_id):
        cn = _filter_chinese_novels(page)
        ids = {n.id for n in cn}
        existing = await novel_repo.get_existing_ids(ids)

        for n in cn:
            if n.id in existing:
                await store_pool.input_q.put(build_from_novel_info(n))
            else:
                await dl_pool.input_q.put(n.id)

    await dl_pool.input_q.join()
    await dl_pool.stop()
    await store_pool.input_q.join()
finally:
    await read_session.close()
```

- `read_session` 是独立的只读 SQLite 连接（WAL 下不阻塞 Store Writer 的写）
- `NovelRepository` 接收该 session，复用现有 `get_existing_ids` 方法
- `finally` 确保 session 被关闭
- 有界队列提供天然背压——`dl_pool.input_q` 满时 `put` 阻塞，翻页自动暂停

### 4.3 WorkerPool — 通用 worker 池抽象

Download worker 和 store worker 本质是同一个模式：从 input queue 取 item → 调用 handler → （可选）把结果推 output queue。区别仅在于并发数、handler 函数、以及 store 需要 mini-batch。

提取为 `WorkerPool`：

```python
import asyncio
from dataclasses import dataclass, field

_SENTINEL = object()

@dataclass
class PoolStats:
    completed: int = 0
    failed: int = 0


class WorkerPool:
    """N 个 worker 共享一个 input queue，并发消费。

    支持两种模式：
    - 逐条模式（batch_size=0）：每取一条就调 handler(item)
    - 批量模式（batch_size>0）：攒够 batch_size 条或超时后调 handler(batch)

    每个 worker 通过 ``asyncio.create_task`` 创建，自动继承父协程的
    ContextVar（如 account_rule 的策略）。
    """

    def __init__(self, name: str, handler, *,
                 concurrency: int = 1,
                 input_q: asyncio.Queue | None = None,
                 output_q: asyncio.Queue | None = None,
                 maxsize: int = 100,
                 batch_size: int = 0,
                 batch_timeout: float = 2.0):
        self.name = name
        self.handler = handler              # async fn(item|batch) -> result|None
        self.concurrency = concurrency
        self.input_q = input_q or asyncio.Queue(maxsize=maxsize)
        self.output_q = output_q
        self.batch_size = batch_size
        self.batch_timeout = batch_timeout
        self.stats = PoolStats()
        self._failed = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    # ---- lifecycle ----

    async def start(self):
        """启动所有 worker 协程。"""
        self._tasks = [
            asyncio.create_task(self._run(i), name=f"{self.name}-{i}")
            for i in range(self.concurrency)
        ]

    async def stop(self):
        """发 sentinel 并等待所有 worker 退出。"""
        for _ in range(self.concurrency):
            await self.input_q.put(_SENTINEL)
        await asyncio.gather(*self._tasks)

    @property
    def failed(self) -> bool:
        return self._failed.is_set()

    # ---- internal ----

    async def _run(self, idx: int):
        if self.batch_size > 1:
            await self._run_batch(idx)
        else:
            await self._run_one(idx)

    async def _run_one(self, idx: int):
        """逐条模式：get → handler → task_done → (optional) put output."""
        while True:
            item = await self.input_q.get()
            if item is _SENTINEL:
                self.input_q.task_done()
                return
            try:
                result = await self.handler(item)
                if self.output_q is not None and result is not None:
                    await self.output_q.put(result)
                self.stats.completed += 1
            except Exception:
                self.stats.failed += 1
                logger.exception(f"[{self.name}-{idx}] handler failed")
            finally:
                self.input_q.task_done()

    async def _run_batch(self, idx: int):
        """批量模式：攒 batch → handler(batch) → batch 内逐个 task_done。"""
        batch: list = []
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.batch_timeout

        while True:
            try:
                timeout = max(0, deadline - loop.time()) if batch else None
                item = await asyncio.wait_for(self.input_q.get(), timeout=timeout)
            except asyncio.TimeoutError:
                if batch:
                    await self._flush_batch(batch)
                    batch.clear()
                    deadline = loop.time() + self.batch_timeout
                continue

            if item is _SENTINEL:
                if batch:
                    await self._flush_batch(batch)
                self.input_q.task_done()
                return

            batch.append(item)
            if len(batch) >= self.batch_size:
                await self._flush_batch(batch)
                batch.clear()
                deadline = loop.time() + self.batch_timeout

    async def _flush_batch(self, batch: list):
        try:
            await self.handler(batch)
            self.stats.completed += len(batch)
        except Exception:
            self.stats.failed += len(batch)
            logger.exception(f"[{self.name}] batch handler failed "
                             f"for {len(batch)} items")
        finally:
            for _ in batch:
                self.input_q.task_done()
```

### 4.4 Handler 函数 — 闭包捕获依赖

Handler 是纯 async 函数，由 `WorkerPool` 调用。依赖（client、file_storage 等）不通过参数传递，而是通过闭包从任务函数的局部作用域捕获。这避免了 `WorkerPool` 需要知道依赖的具体类型：

```python
# 在任务函数内定义 —— 闭包自动捕获 client, file_storage, image_downloader

async def _download_one(nid: int) -> dict | None:
    """下载单本小说，返回 NovelData dict 或 None。"""
    resp = await client.webview_novel(nid)       # client 来自闭包
    if resp is None:
        return None
    data = build_from_webview(resp, file_storage.download_dir)
    if content := data.pop("content", None):
        file_storage.save_novel_text(data["id"], data["title"], content)
    await image_downloader.process_novel_assets(data)
    return data


async def _store_batch(batch: list[dict]) -> None:
    """批量 upsert 小说到 DB。session_factory 来自闭包。"""
    uow = SqlUnitOfWork(session_factory)
    async with uow.begin():
        await _batch_upsert(batch, uow)
```

**关键**：`client.webview_novel` → `_call` → `async with self._semaphore` 已经做全局限流。Crawler 的翻页调用也走同一个 semaphore，所以 Crawler + 5 个 download worker 共享 `Semaphore(5)`。

### 4.5 流水线 = WorkerPool 组合

`NovelPipeline` 不再是一个独立类——它退化为任务函数内对 `WorkerPool` 的直接组合。需要显式管理的只有生命周期顺序（store 先停还是 download 先停）：

```
store_pool.start()  →  dl_pool.start()
                           │
  crawler 翻页 + 路由       │
  (推 dl_pool.input_q       │
   或 store_pool.input_q)   │
                           │
  dl_pool.input_q.join()   │  等所有下载完成
  dl_pool.stop()           │  发 sentinel，退 download workers
                           │
  store_pool.input_q.join()│  等所有入库完成
  store_pool.stop()        │  发 sentinel，退 store worker
```

注意：download workers 退出时，它们生产的最后一批数据可能还在 `store_q` 中未被消费。所以必须 **先 join download_q → stop download → 再 join store_q → stop store**。

### 4.6 错误处理

**逐条模式（download）**：`_run_one` 中 `try/except` 捕获，`stats.failed += 1`，`task_done()` 后继续。不影响其他 item。

**批量模式（store）**：`_flush_batch` 中若整个 batch 失败，降级为逐本重试。实现在 handler 内部（见 5.2 的 `_store_batch`）。

**Store pool 崩溃**：`WorkerPool._failed` Event 被设置。download handler 在 `output_q.put()` 前可检查 `store_pool.failed` 提前退出，避免永久阻塞。

### 4.7 Queue 设计

| 队列 | 所属 WorkerPool | 容量 | 来源 |
|------|----------------|------|------|
| download input | `dl_pool.input_q` | 100 | Crawler `put` new IDs |
| store input | `store_pool.input_q` | 200 | Crawler `put` metadata + dl_pool `output_q` |

有界队列提供天然背压——`put` 满时阻塞生产者。

## 5. 任务函数适配

### 5.1 `author_fetch`（处理型 — 翻页）

```python
from copixiv.infrastructure.repositories.novel import NovelRepository
from copixiv.infrastructure.database.uow import SqlUnitOfWork

@register("author_fetch")
async def author_fetch(author_id, force=False, redownload=False, *,
                       client, uow, file_storage, image_downloader, config, **_):
    # ── 阶段 0: 作者准备（流水线启动前） ──
    async with uow.begin():
        if not force and not await uow.authors.need_update(author_id):
            logger.info(f"Skip Author {author_id}, already updated today.")
            return 0
        author = await uow.authors.get_by_id(author_id)
        if not author:
            async with client.account_rule(
                force_account=_account("follow", config)
            ):
                await client.user_follow_add(author_id)
            detail = await client.user_detail(author_id)
            name = safe_get(safe_get(detail, "user", {}), "name", "")
            uow.authors.ensure_exists({author_id})
            await uow.authors.update_author_name(author_id, name)
            logger.info(f"New author #{author_id} — name={name!r}")

    # ── 阶段 1: 流水线 ──
    session_factory = uow._session_factory

    # Handler 闭包 — 捕获 client, file_storage, image_downloader, session_factory
    async def _download_one(nid: int) -> dict | None:
        resp = await client.webview_novel(nid)
        if resp is None:
            return None
        data = build_from_webview(resp, file_storage.download_dir)
        if content := data.pop("content", None):
            file_storage.save_novel_text(data["id"], data["title"], content)
        await image_downloader.process_novel_assets(data)
        return data

    async def _store_batch(batch: list[dict]) -> None:
        uow = SqlUnitOfWork(session_factory)
        async with uow.begin():
            await _batch_upsert(batch, uow)

    # 组合 WorkerPool
    store_pool = WorkerPool("store", _store_batch, concurrency=1,
                            maxsize=200, batch_size=10, batch_timeout=2.0)
    dl_pool = WorkerPool("dl", _download_one, concurrency=5,
                         output_q=store_pool.input_q, maxsize=100)

    await store_pool.start()
    await dl_pool.start()

    read_session = session_factory()
    try:
        novel_repo = NovelRepository(read_session)
        async for page in client.user_novels_pages(author_id):
            cn = _filter_chinese_novels(page)
            if not cn:
                continue
            ids = {n.id for n in cn}
            existing = await novel_repo.get_existing_ids(ids)

            for n in cn:
                if n.id in existing:
                    await store_pool.input_q.put(build_from_novel_info(n))
                else:
                    await dl_pool.input_q.put(n.id)

        await dl_pool.input_q.join()
        await dl_pool.stop()
        await store_pool.input_q.join()
    finally:
        await read_session.close()

    await store_pool.stop()

    # ── 阶段 2: 收尾 ──
    async with uow.begin():
        await uow.authors.update_last_update(author_id)

    return store_pool.stats.completed
```

### 5.2 `novel_follow`（处理型 — 翻页）

与 `author_fetch` 相同结构。区别：
- 翻页来源是 `client.novel_follow_pages(fetch_til=...)`
- 无需阶段 0 的新作者检测
- `account_rule(force_account="follow")` 包裹整个流水线

```python
@register("novel_follow")
async def novel_follow(days=3, force=False, *,
                       client, uow, file_storage, image_downloader, config, **_):
    fetch_til = datetime.now().astimezone() - timedelta(days=days)
    session_factory = uow._session_factory

    async def _download_one(nid: int) -> dict | None:
        resp = await client.webview_novel(nid)
        if resp is None:
            return None
        data = build_from_webview(resp, file_storage.download_dir)
        if content := data.pop("content", None):
            file_storage.save_novel_text(data["id"], data["title"], content)
        await image_downloader.process_novel_assets(data)
        return data

    async def _store_batch(batch: list[dict]) -> None:
        uow = SqlUnitOfWork(session_factory)
        async with uow.begin():
            await _batch_upsert(batch, uow)

    async with client.account_rule(force_account=_account("follow", config)):
        store_pool = WorkerPool("store", _store_batch, concurrency=1,
                                maxsize=200, batch_size=10, batch_timeout=2.0)
        dl_pool = WorkerPool("dl", _download_one, concurrency=5,
                             output_q=store_pool.input_q, maxsize=100)

        await store_pool.start()
        await dl_pool.start()

        read_session = session_factory()
        try:
            novel_repo = NovelRepository(read_session)
            async for page in client.novel_follow_pages(fetch_til=fetch_til):
                cn = _filter_chinese_novels(page)
                if not cn:
                    continue
                ids = {n.id for n in cn}
                existing = await novel_repo.get_existing_ids(ids)

                for n in cn:
                    if n.id in existing:
                        await store_pool.input_q.put(build_from_novel_info(n))
                    else:
                        await dl_pool.input_q.put(n.id)

            await dl_pool.input_q.join()
            await dl_pool.stop()
            await store_pool.input_q.join()
        finally:
            await read_session.close()

        await store_pool.stop()

    return store_pool.stats.completed
```

### 5.3 `novel_ranking`（发现型 — 提交子任务）

```python
@register("novel_ranking")
async def novel_ranking(mode="day_r18", days=3, *,
                        client, task_manager, **_):
    """发现型任务：翻页收集作者 ID，提交 author_fetch 子任务。"""
    submitted = 0
    for delta in range(1, max(2, days)):
        target = datetime.now().astimezone() - timedelta(days=delta)
        async for page in client.novel_ranking_pages(mode=mode, date=target):
            cn = _filter_chinese_novels(page)
            author_ids = {safe_get(n, "user.id") for n in cn if safe_get(n, "user")}
            for aid in author_ids:
                if aid:
                    await task_manager.run_task("author_fetch", author_id=aid)
                    submitted += 1
    return submitted
```

`novel_search` 同理——翻页过滤后提取作者 ID，提交子任务。

### 5.4 `novel_fetch`（单本，不走流水线）

单本下载保持简单：`webview_novel` → `build_from_webview` → `save_novel_text` → `process_novel_assets` → `_batch_upsert`。不需要流水线。

---

## 6. `_run_task_wrapper` 改动

当前 wrapper 注入 `client, uow, file_storage, image_downloader, config` 给任务函数。流水线模式下 `pipeline` 由任务函数自行创建，不需要注入。只需新增 `task_manager` 给发现型任务提交子任务：

```python
# manager.py _run_task_wrapper 中
kwargs = {
    "client": client,
    "file_storage": file_storage,
    "image_downloader": image_downloader,
    "epub_builder": epub_builder,
    "config": config,
    "uow": uow,
    "task_manager": self,           # 新增：供发现型任务提交子任务
}
```

---

## 7. 待删除 / 简化的代码

| 文件 | 内容 | 处理 |
|------|------|------|
| [pipeline.py:13](src/copixiv/tasks/pipeline.py#L13) | `_db_write_lock` | **删除** |
| [pipeline.py:114-161](src/copixiv/tasks/pipeline.py#L114-L161) | `_download_novels` | 逻辑迁移到 download worker |
| [pipeline.py:169-227](src/copixiv/tasks/pipeline.py#L169-L227) | `_batch_handle` | **删除**。路由进 Crawler，upsert 进 Store Worker |
| [pipeline.py:235-266](src/copixiv/tasks/pipeline.py#L235-L266) | `_make_page_handler` | **删除** |
| [pipeline.py:68-106](src/copixiv/tasks/pipeline.py#L68-L106) | `_batch_upsert` | **保留**，移除 `async with _db_write_lock` |
| [novel_tasks.py:37-75](src/copixiv/tasks/novel_tasks.py#L37-L75) | `_fan_out_author_fetch` | **删除**。改为发现型任务提交子任务 |
| [client.py:180-244](src/copixiv/infrastructure/pixiv/client.py#L180-L244) | `_paginate` (handler 模式) | **新增** `_paginate_pages` 生成器版本；保留旧版兼容 |
| [client.py:169-177](src/copixiv/infrastructure/pixiv/client.py#L169-L177) | `_run_handlers` | 可保留（单页场景），或统一到生成器 |

---

## 8. 实施步骤

1. **Client 新增翻页生成器** — `user_novels_pages`、`novel_follow_pages` 等
2. **实现 `WorkerPool` 类** — 通用 worker 池，支持逐条/批量两种模式
3. **实现 download worker** — 复用现有 `_fetch_one` 逻辑
4. **实现 store worker** — 复用现有 `_batch_upsert`，移除锁
5. **改造 `_run_task_wrapper`** — 注入 `task_manager`
6. **改造 `author_fetch`** — 阶段 0 (作者准备) → 阶段 1 (流水线) → 阶段 2 (收尾)
7. **改造 `novel_follow`** — 同上模式
8. **改造 `novel_ranking` / `novel_search`** — 改为发现型，翻页收集 ID → 提交子任务
9. **删除旧代码** — `_make_page_handler`, `_download_novels`, `_batch_handle`, `_fan_out_author_fetch`, `_db_write_lock`
10. **测试** — 每个任务类型单独跑，验证入库数量和时间
