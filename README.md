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
backup:
  keep_count: 4   # 可选：保留最近 N 份每周备份（默认 4，防止误删无保护）
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
uvicorn main:app --host 0.0.0.0 --port 9000
```

`http://localhost:9000` 即可访问。

> **生产部署（systemd）不要带 `--reload`**：`main.py` 默认已关闭 reload，
> 仅当设置 `COPIXIV_RELOAD=1`（开发调试）时才启用。systemd 下由
> `Restart=` 负责拉起，uvicorn 的 reloader 会把任何对 `*.py` 的触碰
> （包括 `.venv` 内的文件）变成重启风暴。

> **提示**：如果旧项目 `../copixiv/.venv` 已装好依赖，也可以直接复用：
> ```bash
> source ../copixiv/.venv/bin/activate
> pip install -e ".[dev]"   # 只补装 v2 新增的包
> ```

### 5. 生产部署前端（不要再跑 `npm run dev`）

前端 systemd 服务原本跑的是 Vite 开发服务器（HMR/内存缓存/长驻进程都不适合
生产）。生产模式改为「构建 + 静态预览」：

```bash
cd frontend
npm run build            # 产出 frontend/dist/（vue-tsc 类型检查 + vite build）
npm run preview          # vite preview：静态服务 + /api 反代到 127.0.0.1:9000
```

仓库提供了现成的单元文件 `deploy/copixiv-frontend.service`（构建 → preview，
每次启动自动重新构建）：

```bash
sudo cp deploy/copixiv-frontend.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl stop copixiv-frontend    # 先停掉旧的 dev-server 单元
sudo systemctl start copixiv-frontend
sudo systemctl status copixiv-frontend  # 确认 active (running)
```

**启动失败排查**（`systemctl start` 报 "control process exited with error code" 时）：

```bash
journalctl -xeu copixiv-frontend.service -n 80 --no-pager   # systemd 侧真实错误
cd /home/invocation/copixiv-v2/frontend && npm run build    # 手动跑构建看报错
ss -tlnp | grep 5173                                        # 旧 dev 进程占端口？
# 若 5173 被非 systemd 的残留 vite 进程占用：
pkill -f "vite" && sudo systemctl start copixiv-frontend
```

`vite preview` 对单用户工具足够；若以后要多用户/HTTPS/压缩，把 ExecStart 换成
nginx（`frontend/dist` 静态 + `location /api { proxy_pass http://127.0.0.1:9000; }`）。

### 6. FTS 索引维护（升级旧库后必做）

v1 库的 `novel_fts` 表没有 `tags` 列，关键词搜索命中不了「只出现在标签里」的
文本。服务恢复后跑一次重建任务（或直接建一个每周定时任务自动维护）：

```bash
# 立即重建（通过 API 建一次性任务并触发）
curl -X POST http://127.0.0.1:9000/api/tasks/scheduled \
  -H 'Content-Type: application/json' \
  -d '{"name":"FTS重建","task":"rebuild_fts","cron":"0 4 * * 1","is_enabled":false}'
# 用返回的 id 触发立即执行（或在网页任务页点“立即运行”）
curl -X POST http://127.0.0.1:9000/api/tasks/scheduled/<id>/run

# 健康检查任务（孤儿/缺失条目/损坏检测）
curl -X POST http://127.0.0.1:9000/api/tasks/scheduled \
  -H 'Content-Type: application/json' \
  -d '{"name":"FTS检查","task":"check_fts","cron":"20 4 * * 1","is_enabled":false}'
```

把两个任务的 `is_enabled` 设为 `true` 即可每周自动执行。

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
# 含：数据库/仓库、FTS 标签索引、pixiv 层（AccountPool/重试/patch，零网络）、
#     Alembic 迁移链路（v1 形状旧库 → head）
pytest tests/infrastructure/ -v

# 用例 / 任务 / Web 层测试
# 含：DownloadNovelUseCase + persist_novels 单测、端点冒烟（TestClient，
#     流式 batch-download、DomainError→HTTP）、FTS 维护任务
pytest tests/application/ tests/tasks/ tests/web_api/ -v
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
├── application/            # 业务逻辑：仅保留有真实编排的模块（薄用例层已移除）
│   ├── novel/              # DownloadNovel, BatchDownload, DeleteNovel, GetNovelFile, persist
│   ├── author/             # resolve_author_names（作者名解析）
│   └── search_history/     # record（后台记录回调）
├── infrastructure/         # 实现层
│   ├── database/           # SQLAlchemy engine / session / ORM / UnitOfWork
│   ├── repositories/       # Novel, Author, Series, Tag, Token, Task…
│   ├── pixiv/              # PixivAccount, AccountPool, PixivClient, patch
│   ├── storage/            # FileStorage, ImageDownloader
│   └── epub/               # EpubBuilder
├── tasks/                  # 后台任务（每文件一个任务）
├── web_api/                # 薄 FastAPI 层（endpoint 直连 repository）
│   ├── schemas.py          # 请求/响应 Pydantic（与 v1 契约一致）
│   ├── deps.py             # FastAPI Depends
│   └── endpoints/          # 7 个路由模块
tests/                       # 按层组织
├── domain/                  # 纯单元测试（零 I/O，models + services）
├── application/             # 用例层测试（假仓库）
├── infrastructure/          # 集成测试（内存 SQLite，database + repositories）
├── tasks/                   # 后台任务流水线回归测试
└── web_api/                 # FastAPI 依赖契约测试
```

依赖方向：`web_api` → `application` → `domain` ← `infrastructure`（application 通过 `domain/ports` 的 Protocol 依赖抽象，不直接依赖具体实现）

---

## 与 v1 的关系

- 旧项目 `/home/invocation/copixiv` 保持不变，互不干扰
- API 路径、参数、响应格式完全兼容，前端无需改动
- 数据库 schema 不变，可直接复用旧 DB 文件
- 配置文件 `config.yaml` 格式不变
