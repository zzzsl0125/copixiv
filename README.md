# copixiv

Pixiv 小说管理器：轻松抓取、管理和导出 Pixiv 中文小说，并支持本地 TXT/EPUB 上传入库。

## 功能特性

- **批量入库与本地上传**：支持爬取指定作者、排行榜或搜索结果入库；也支持上传本地 TXT/EPUB 并编辑元数据（作者 / 系列 / 标签 / 简介）
- **便捷搜索**：PostgreSQL `pg_bigm` 索引加速标题、作者、系列名与标签的真子串关键词搜索，支持指定字段过滤
- **个人喜好**：支持收藏指定作品、追更指定作者，以及标签偏好（喜爱 / 厌恶）与标签别名聚合映射
- **厌恶标签排除**：带厌恶标签的小说从浏览/搜索/计数/全选中隐藏（全局开关，默认开启），可随时「查看被排除」并在视图内调整筛选与排序
- **定时任务**：内置任务队列与 cron 调度，支持进度追踪（`progress`）及 Telegram 任务通知；支持重复入队按 FIFO 顺序执行
- **Pixiv 账号池**：支持多 Pixiv 账号并发爬取、热重载、负载均衡与冷却，可指定追更账号（`is_follow`）

## 快速开始

### 环境要求

- Python 3.12+
- Node.js 20.19+（仅前端构建需要）
- PostgreSQL（带 `pg_bigm` 扩展；本地开发推荐用 `scripts/pg_dev.py` 管理的自带 `pgserver` 实例，端口 `5433`）
- Pixiv 账号（需获取 `refresh_token`）

### 1. 初始化配置

```bash
git clone https://github.com/zzzsl0125/copixiv.git
cd copixiv
cp config.example.yaml config.yaml
```

`config.yaml` 需先行配置（`refresh_token` 可留待后续在 Web UI 的「账号管理」内维护）：

- `notifications` 为通知后端列表（如 `type: telegram` + `token` / `chat_id`，空列表 `[]` 关闭通知）
- `proxy.url` 单键同时用于 HTTP/HTTPS

### 2. 启动 PostgreSQL 与后端

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install .            # 运行时依赖
# 本地开发 / 静态检查建议：pip install ".[dev]"

# 启动本地 PostgreSQL（数据目录 data/pg/pgdata，端口 5433，日志 log/pg.log）
python scripts/pg_dev.py start
# 如需编译 pg_bigm 扩展，参见 scripts/build_pg_bigm.sh

# 启动应用（自动应用 Alembic 迁移 0001–0015）
python main.py
```

### 3. 构建前端（可选）

后端（`copixiv.app.mount_frontend`）会直接静态托管 `frontend/dist`，因此只需构建一次，无需单独运行 `vite preview`：

```bash
cd frontend
npm install
npm run build
```

打开 `http://localhost:9000` 即可使用（API 与页面同进程同端口）。前端源码修改后重新执行 `npm run build` 即可生效，无需重启后端；`frontend/dist` 不存在时后端仅提供 API 服务。

### 4. Systemd 部署

仓库提供统一的单单元部署文件 `deploy/copixiv.service`（`KillMode=process`，重用后台 PostgreSQL 实例）与更新脚本 `scripts/deploy_backend.sh`：

```bash
sudo cp deploy/copixiv.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now copixiv.service
```

## 小说入库

1. 在 **侧边栏 - 账号管理** 内添加可用的 Pixiv 账号。
2. 在 **侧边栏 - 任务管理** 内配置或运行抓取任务；本地小说可在 **我的上传** 中直接上传。

## API

| 前缀 | 说明 |
|------|------|
| `/api/novels` | 小说列表、计数、下载、收藏、特别关注、单本编辑/同步/删除、批量导出/批量操作（含 `sort-ids` / `match-ids` / `blocked-ids`） |
| `/api/library` | 本地 TXT / EPUB 上传与入库 |
| `/api/tasks` | 任务方法、定时任务管理、立即运行、执行历史 |
| `/api/system` | 系统配置（含运行时设置 `exclude_blocked_tag_novels`） |
| `/api/tag-preferences` | 标签偏好（喜爱/厌恶）与排序 |
| `/api/tag-aliases` | 标签别名映射与相似标签建议 |
| `/api/search-history` | 搜索历史 |
| `/api/tokens` | Pixiv 刷新令牌管理 |
| `/api/failed-novels` | 失败台账查看、清除、重置计数与重试 |

## 架构与质量门禁

模块化单体：组合根（`app.py` / `middleware.py` / `error_handlers.py` / `lifespan.py`）+ 纯 Python 核心（`core/`）+ 适配层（`db/` / `pixiv/` / `storage/` / `notify/`）+ 任务内核（`tasks/`）+ 按功能切片的 `features/`。边界与静态门禁详见 `docs/MODULARITY.md`。

```text
src/copixiv/
├── app.py              # FastAPI 工厂、路由挂载与前端托管
├── middleware.py       # API Key 鉴权与请求日志中间件
├── error_handlers.py   # 域异常 → HTTP 状态码映射
├── lifespan.py         # 数据库/账号池/任务系统/备份生命周期管理
├── config.py           # 配置模型与加载
├── deps.py             # FastAPI DI 依赖
├── log.py              # Loguru 日志配置
├── core/               # 纯 Python：models / services / exceptions / draft / naming / ids / language / calendar / formats
├── db/                 # PostgreSQL 唯一适配：engine / uow / write_lock / backup / models / constants / base
├── pixiv/              # pixivpy3 防腐层（唯一厂商边界）
├── storage/            # file_storage / image_downloader / zip / atomic / assets / epub
├── notify/             # telegram / factory
├── tasks/              # registry / executor / cron / manager / kernel(兼容导出) / history_repo / schemas / api / novels / batch / maintenance
└── features/           # accounts / authors / failures / library / novels(含 repo/) / system / tags
alembic/                # 数据库迁移 (0001–0015)
deploy/                 # systemd 单元文件 (copixiv.service)
frontend/               # Vue 3 + TypeScript + Tailwind CSS v4 前端
scripts/                # pg_dev.py / build_pg_bigm.sh / deploy_backend.sh / run_verification.sh 等脚本
tests/                  # 单元测试 (unit/)、PG 数据库测试 (db/) 与集成测试 (integration/)
```
