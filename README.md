# copixiv

Pixiv 小说管理器：轻松抓取、管理和导出 Pixiv 中文小说。

## 功能特性

- **批量入库**：爬取指定作者作品、排行榜作品或搜索结果下载入库
- **便捷搜索**：关键词搜索涵盖标题、作者、系列名、标签，也支持指定字段搜索
- **个人喜好**：支持收藏指定作品，追更指定作者；支持高亮标记喜好的标签
- **厌恶标签排除**：带厌恶标签的小说从浏览/搜索/计数/全选中隐藏（全局开关，默认开启），可随时「查看被排除」并在视图内改筛选与排序
- **定时任务**：支持定时执行爬取或维护任务，支持将任务结果发送至TG；同一任务函数（**不同显示名也算**）已有 pending/running 时入队返回 409，任务进度在「任务管理」页实时展示（`progress` 字段）
- **Pixiv 账号池**：支持多pixiv账号并发爬取，带负载均衡及防封号冷却；可在「账号管理」指定一个账号作为**追更账号**（`is_follow`），追更/关注更新任务固定使用它

## 快速开始

### 环境要求

- Python 3.12+
- Node.js 20.19+（仅前端需要）
- Pixiv 账号（需手动获取refresh token）

### 1. 初始化

```bash
git clone https://github.com/zzzsl0125/copixiv.git
cd copixiv
cp pixiv_token.py.example pixiv_token.py
cp config.example.yaml config.yaml
```

config.yaml 需先行修改，pixiv refresh_token 可留待后续在 webui 内修改。

配置为新格式：`notifications` 是一个**通知后端列表**（`type: telegram|webhook`，
多余字段留空，空数组 `[]` 关闭全部通知），`proxy.url` 单键同时用于 HTTP/HTTPS。
遗留旧键（`telegram` / `notifiers.enabled` / `webhook` / `frontend` 段、旧
`proxy.http`/`proxy.https` 及对应 env）会在启动时报错并给出迁移提示——请按新格式
更新，示例见 `config.example.yaml`。

### 2. 启动后端

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install .
python main.py
```

### 3. 启动前端

```bash
cd frontend
npm install
npm run build
npm run preview
```

打开 `http://localhost:5173` 即可开始使用


## 小说入库

1. 在 **侧边栏 - 账号管理** 内添加可用的 pixiv 账号。
2. 在 **侧边栏 - 任务管理** 内添加所需的任务并运行。 

## API

| 前缀 | 说明 |
|------|------|
| `/api/novels` | 小说列表、计数、下载、收藏、特别关注、删除、批量打包、批量操作（删除/加减标签）、厌恶标签排除（blocked-ids 查看被排除 / sort-ids 集合排序） |
| `/api/tasks` | 任务方法、定时任务管理、立即运行、执行历史 |
| `/api/system` | 系统配置（含运行时设置 exclude_blocked_tag_novels，默认开启） |
| `/api/tag-preferences` | 标签偏好（喜爱/屏蔽） |
| `/api/tag-aliases` | 标签别名、相似标签建议 |
| `/api/search-history` | 搜索历史 |
| `/api/tokens` | Pixiv 刷新令牌管理 |
| `/api/failed-novels` | 失败台账：查看/清除/重置计数/重试 |

> v2 API 已解冻：响应细节可随版本自由演进（与前端 bundle 同步维护）。
> `/api/system/config` 只返回 `batch_download_naming` 与 `exclude_blocked_tag_novels`；
> 旧的 `default_min_like`/`default_min_text` 已移除，前端默认筛选值内置在客户端
> 源码（min_like=500 / min_text=3000）。

## 架构

模块化单体：`app.py` 组合根 + `core`（纯 Python 核心）+ 适配层
（`db` / `pixiv` / `storage` / `notify`）+ `tasks` 任务内核 + 按功能切片的
`features`。目录职责、依赖约定与硬规则见 `docs/MODULARITY.md`。

## 项目结构

```
src/copixiv/
├── app.py          # 组合根：create_app / lifespan / 中间件 / 异常映射 / 启动项 / main()
├── config.py       # 配置模型与加载
├── deps.py         # FastAPI 依赖
├── log.py          # 日志
├── core/           # 纯 Python：models / services / exceptions / draft
├── db/             # engine / uow / write_lock / backup / models / constants / base
├── pixiv/          # pixivpy3 防腐层（唯一厂商边界）
├── storage/        # file_storage / image_downloader / epub
├── notify/         # telegram / webhook / composite / factory
├── tasks/          # kernel / api / novels / batch / maintenance / history_repo
└── features/       # accounts / authors / failures / novels / system / tags（api + repo）
alembic/            # 数据库迁移
database/           # SQLite 数据库
deploy/             # systemd 等部署文件
frontend/           # Vue 3 前端
scripts/            # 运维/验证脚本
tests/              # 测试
```
