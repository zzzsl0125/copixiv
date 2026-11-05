# Copixiv Frontend

Pixiv 小说管理器前端：Vue 3 + TypeScript + Vite + Tailwind CSS v4 + Vitest。

## 开发

```bash
npm install
npm run dev        # 开发服务器，默认 http://localhost:5173
npm run test       # vitest run
npm run build      # vue-tsc 类型检查 + vite build
```

开发服务器的 `/api` 请求由 Vite 代理到 `http://127.0.0.1:9000`（见 `vite.config.ts`），前端始终使用同源 `/api` 路径。

## 生产部署

前端不再由独立进程伺服：后端（`copixiv.service`，:9000）直接提供 `frontend/dist`，
见 `copixiv.app.mount_frontend`。Node 只在**构建**时用到：

```bash
cd frontend && npm run build   # 产出 dist/，之后不需要重启后端
```

- 构建是部署步骤，不在 systemd 的 `ExecStartPre` 里（避免每次启动都跑 vue-tsc + vite）
- 页面与 API 同源同端口（`:9000`），没有 `vite preview`、没有 `:5173`
- `npm run dev`（:5173）仍可用于开发，`/api` 由 Vite 代理到 `127.0.0.1:9000`

常用操作：

```bash
journalctl -u copixiv -n 100      # 后端（含静态托管）日志
bash ../scripts/diag_frontend.sh  # 构建 + 伺服诊断
```

## 后端契约

- 小说列表：`GET /api/novels/`，响应 `{novels, cursor}`，`is_favourite/is_special_follow/has_epub` 为整数，`has_epub`：0 无 / 1 生成中 / 2 可用
- 批量下载：`POST /api/novels/batch-download`，`limit` 上限 500
- Token 列表：`GET /api/tokens/` 返回掩码 token（`****xxxx`），编辑时不要把掩码写回

## 可选 API Key

后端 `config.yaml` 的 `security.api_key` 默认关闭。若启用，构建时注入 key：

```bash
VITE_API_KEY=你的key npm run build
```

所有 Axios 请求会通过 `src/api/client.ts` 统一附加 `X-API-Key` 头。

单篇下载与导出文件使用浏览器直连的附件 URL（`Content-Disposition: attachment`），
不依赖 `blob:` 对象 URL —— 在部分内置浏览器 / WebView（不支持 blob 下载）中也能正常触发下载，
且弹窗显示的是服务器返回的真实文件名（如小说名）。开启 API Key 时，这些下载 URL 会附带
`?api_key=` 参数（后端中间件在缺失 `X-API-Key` 头时也会读取该参数），从而不绕过鉴权。

## 排障

- **页面 404 / 还是旧版**：确认 `frontend/dist/index.html` 存在且已重新 `npm run build`；`curl -I http://127.0.0.1:9000/` 应为 200 `text/html`
- **页面能开但数据空白**：确认后端在 9000 端口运行，`curl http://127.0.0.1:9000/api/system/config` 应有 JSON
- **CORS**：生产同源同端口，不依赖浏览器跨域；开发服务器（:5173）的白名单默认允许 `localhost/127.0.0.1:5173/4173`
- **旧 unit 残留**：若 `/etc/systemd/system/copixiv-frontend.service` 还在，`sudo systemctl disable --now copixiv-frontend` 后删除
