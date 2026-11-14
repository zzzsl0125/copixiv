# Copixiv Frontend

Pixiv 小说管理器前端：Vue 3 + TypeScript + Vite + Tailwind CSS v4。

## 开发

```bash
npm install
npm run dev        # 开发服务器，默认 http://localhost:5173
npm run typecheck  # vue-tsc 类型检查
npm run build      # vue-tsc 类型检查 + vite build
npm run codegen    # 由 openapi.json 生成 src/types/api.generated.ts
```

开发服务器的 `/api` 请求由 Vite 代理到 `http://127.0.0.1:9000`（见 `vite.config.ts`），前端始终使用同源 `/api` 路径。

## 生产部署

前端由后端（`copixiv.service`，`:9000`）直接静态托管 `frontend/dist`（`copixiv.app.mount_frontend`）。Node 仅在构建阶段使用：

```bash
cd frontend && npm run build   # 产出 dist/，之后不需要重启后端
```

- 构建是部署前步骤，不放在 systemd 的 `ExecStartPre` 中
- 页面与 API 同源同端口（`:9000`），不再运行独立的 `vite preview` 进程

常用排障命令：

```bash
journalctl -u copixiv -n 100      # 后端（含静态托管）日志
bash ../scripts/diag_frontend.sh  # 构建 + 伺服诊断
```

## 后端契约

- 小说列表：`GET /api/novels/`，响应 `{novels, cursor, has_excluded}`，`is_favourite` / `is_special_follow` / `has_epub` 为整数（`has_epub`：0 无 / 1 生成中 / 2 可用 / 4 缺源文件），`source` 为 `'pixiv' | 'local'`；列表与编辑契约（`NovelBase` / `NovelUpdateRequest` / `EditNovelModal`）不再包含 `caption`
- 批量辅助：`POST /api/novels/by-ids`（按选中 ID 顺序返回卡片投影）、`POST /api/novels/sort-ids`（支持 `id` / `like` / `view` / `text` / `create_time`）、`POST /api/novels/match-ids` 与 `GET /api/novels/{ids,count}` 均按当前筛选语义支持 `exclude_blocked`
- Token 列表：`GET /api/tokens/` 返回掩码 token（`****xxxx`），编辑时不要把掩码写回

## 可选 API Key

后端 `config.yaml` 的 `security.api_key` 默认关闭。若启用，构建时注入 key：

```bash
VITE_API_KEY=你的key npm run build
```

所有 Axios 请求会通过 `src/api/client.ts` 统一附加 `X-API-Key` 头；单篇下载和批量下载链接会自动附带 `?api_key=` 查询参数。

## 排障

- **页面 404 / 还是旧版**：确认 `frontend/dist/index.html` 存在且已重新 `npm run build`；`curl -I http://127.0.0.1:9000/` 应返回 `200 text/html`
- **页面能开但数据空白**：确认后端在 9000 端口运行，`curl http://127.0.0.1:9000/api/system/config` 应返回 JSON
- **CORS**：生产环境同源同端口；开发服务器（`:5173`）白名单默认允许 `localhost` / `127.0.0.1` 的 `5173` / `4173`
