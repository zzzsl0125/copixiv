# copixiv-v2

Pixiv 小说下载管理器 v2 重写版。

## 快速开始

### 1. 创建虚拟环境

```bash
cd copixiv-v2
python3 -m venv .venv
source .venv/bin/activate
```

以后每次打开新终端都要先 `source .venv/bin/activate` 激活环境。

### 2. 安装依赖

```bash
pip install -e ".[dev]"
```

`-e` 表示编辑模式（改代码立即生效，不用反复 pip install）。
`[dev]` 会额外安装 pytest、httpx 等测试工具。

### 3. 配置文件

复制旧项目的 `config.yaml` 过来即可，格式完全兼容：

```bash
cp ../copixiv/config.yaml .
```

没有旧配置的话，自己建一个最小的也行（其余字段用默认值）：

```yaml
path:
  database: database/database.db
  download: download
```

环境变量可覆盖任意配置项（前缀 `COPIXIV_`，双下划线分隔层级）：

```bash
export COPIXIV_PROXY__HTTP="http://127.0.0.1:20172"
export COPIXIV_PROXY__HTTPS="http://127.0.0.1:20172"
```

### 4. 启动

```bash
python main.py
# 或
uvicorn main:app --host 0.0.0.0 --port 9000 --reload
```

`http://localhost:9000` 即可访问。

> **提示**：如果旧项目 `../copixiv/.venv` 已装好依赖，也可以直接复用：
> ```bash
> source ../copixiv/.venv/bin/activate
> pip install -e ".[dev]"   # 只补装 v2 新增的包
> ```

### API 端点（与 v1 完全兼容）

| 前缀 | 说明 |
|------|------|
| `/api/novels` | 小说列表、下载、收藏、批量打包 |
| `/api/tasks` | 定时任务管理、执行历史 |
| `/api/system` | 系统配置 |
| `/api/tag-preferences` | 标签偏好（喜爱/屏蔽） |
| `/api/tag-aliases` | 标签别名 |
| `/api/search-history` | 搜索历史 |
| `/api/tokens` | Pixiv 刷新令牌管理 |

---

## 测试

### 运行全部测试

```bash
pytest tests/ -v
```

### 按层运行

```bash
# 纯单元测试（零 I/O，无需数据库）
pytest tests/domain/ -v

# 基础设施测试（内存 SQLite，无外部依赖）
pytest tests/infrastructure/ -v
```

### 运行单个文件

```bash
pytest tests/domain/test_services.py -v
pytest tests/infrastructure/test_repositories.py -v
```

### 按名称过滤

```bash
pytest tests/ -k "test_upsert" -v
pytest tests/ -k "parse_tags" -v
```

---

## 项目结构

```
src/copixiv/
├── app/                    # 组合根：配置加载 + 依赖组装
│   ├── config.py           # Pydantic Settings（YAML → env 覆盖）
│   └── container.py        # Container.build() 创建全部对象
├── domain/                 # 纯核心 — 零外部依赖
│   ├── models/             # Pydantic 实体
│   ├── ports/              # Protocol 抽象接口
│   └── services/           # 纯函数：标签解析、中文检测、路径生成…
├── application/            # 用例：编排 domain 端口完成任务
│   ├── novel/              # ListNovels, DownloadNovel, ToggleFavourite…
│   ├── author/             # resolve_author_names（作者名解析）
│   ├── tag/ task/ token/   # 标签、定时任务、Token 用例
│   ├── search_history/     # 列表/删除 + record（后台记录）
│   └── system/             # GetConfigUseCase
├── infrastructure/         # 实现层
│   ├── database/           # SQLAlchemy engine / session / ORM / UnitOfWork
│   ├── repositories/       # Novel, Author, Series, Tag, Token, Task…
│   ├── pixiv/              # PixivAccount, AccountPool, PixivClient, patch
│   ├── storage/            # FileStorage, ImageDownloader
│   └── epub/               # EpubBuilder
├── tasks/                  # 后台任务（每文件一个任务）
├── web_api/                # 薄 FastAPI 层
│   ├── schemas.py          # 请求/响应 Pydantic（与 v1 契约一致）
│   ├── deps.py             # FastAPI Depends
│   └── endpoints/          # 7 个路由模块
tests/                       # 按层组织
├── domain/                  # 纯单元测试（零 I/O，models + services）
└── infrastructure/          # 集成测试（内存 SQLite，database + repositories）
```

依赖方向：`web_api` → `application` → `domain` ← `infrastructure`（application 通过 `domain/ports` 的 Protocol 依赖抽象，不直接依赖具体实现）

---

## 与 v1 的关系

- 旧项目 `/home/invocation/copixiv` 保持不变，互不干扰
- API 路径、参数、响应格式完全兼容，前端无需改动
- 数据库 schema 不变，可直接复用旧 DB 文件
- 配置文件 `config.yaml` 格式不变
