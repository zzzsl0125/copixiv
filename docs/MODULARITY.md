# copixiv 目录速查

> 本文件的定位：**给人和 AI 的目录边界卡**。它写清每个目录的职责、
> 依赖约定与硬规则，并如实标注**哪些规则有测试钉死、哪些只是约定**。
> 结构已从六边形分层精简为模块化单体；旧的分层矩阵 / M 编号体系已作废。

## 1. 目录职责

| 目录 | 一句话职责 |
|------|-----------|
| `src/copixiv/app.py` | 组合根：create_app / lifespan / 中间件 / 异常映射 / 启动项 / main() |
| `src/copixiv/config.py` | 配置模型与加载 |
| `src/copixiv/deps.py` | FastAPI 依赖（get_session_factory / get_app_config / get_file_storage / get_task_manager / get_uow / get_write_uow / parse_json_param） |
| `core/` | 纯 Python：models.py / services.py / exceptions.py / draft.py（零 IO、零框架、零 SQLAlchemy） |
| `db/` | engine / uow（纯事务边界）/ write_lock / backup / models / constants / base |
| `pixiv/` | pixivpy3 防腐层（唯一允许 import pixivpy3 的目录） |
| `storage/` | file_storage / image_downloader / epub 子包 |
| `notify/` | telegram / webhook / composite / factory |
| `tasks/` | kernel.py（registry + context + history + executor + scheduler + manager 合并）/ history_repo / schemas / api / novels / batch / maintenance / pipeline |
| `features/` | accounts / authors / failures / novels / system / tags（每目录 api(+schemas)+repo） |

## 2. 依赖规则（约定，无矩阵执法）

1. **`core` 不 import 任何其他目录。**
2. **`db/pixiv/storage/notify` 为适配层**：彼此不互相 import，
   只被 `features` / `tasks` / `app` 使用。
3. **`features` 之间允许单向 import**（模块化单体），一行一个方向，避免成环。

> 不再有全局 import 矩阵执法，这是**有意的取舍**——这个体量配不上 7 层矩阵。
> 前两条靠"目录即文档"与 code review 守住；只有下面的两条是测试执法。

## 3. 硬规则（违反 = 测试变红）

### 3.1 pixivpy3 厂商白名单

`import pixivpy3` 只允许出现在 `pixiv/` 下（`patch.py` / `account.py` /
`errors.py`，后者因异常族必须继承 `pixivpy3.PixivError`）。其余代码一律
通过 `pixiv/` 导出的接口与异常族。

执法：`tests/architecture/test_vendor_whitelist.py::test_pixivpy3_vendor_whitelist`
（AST 扫描整个 `src`，任何新模块 import pixivpy3 都会立刻失败）。

### 3.2 写路径必须走全局写锁

- API 写端点必须声明 `Depends(get_write_uow)`；读端点用 `get_uow`。
- 后台任务写库走 `uow.begin()` / `db_write()`。SQLite 单写者，API 与后台任务
  共享同一把锁。

执法：`tests/regression/test_m6_write_lock.py`（`test_get_write_uow_holds_global_lock`
/ `test_write_endpoints_declare_write_uow`）。

> 这两条是**仅剩的**测试执法边界；其余规则在 §2 与 §4，皆为约定，违反不会红。

## 4. 约法三章（新增代码的默认姿势）

1. **新增功能 = 一个 `features/<name>/` 目录**：`api.py` / `repo.py` /
   `schemas.py` 同置，不各起各的层。
2. **跨 feature 引用允许，但保持单向**：不要成环（A → B、B → A 互为依赖）。
3. **`core` 不放 IO**：零框架、零 SQLAlchemy、零外部副作用；需要 IO 的东西
   放 feature 或适配层。
