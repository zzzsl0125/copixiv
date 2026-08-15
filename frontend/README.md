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

生产由 systemd 单元 `copixiv-frontend.service` 管理：

- 每次启动前执行 `npm run build`（`ExecStartPre`），确保 `dist/` 与源码一致
- 使用 `npm run preview` 在 `0.0.0.0:5173` 提供构建产物
- `vite preview` 的 `preview.proxy` 同样把 `/api` 转发到 `127.0.0.1:9000`

常用操作：

```bash
sudo systemctl restart copixiv-frontend   # 重启并重新构建
journalctl -u copixiv-frontend -n 100     # 查看日志
ss -tlnp | grep 5173                      # 检查端口占用
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

所有 Axios 请求会通过 `src/api/client.ts` 统一附加 `X-API-Key` 头；单篇下载也已走同一客户端（Blob 下载），不会绕过鉴权。

## 排障

- **5173 端口被旧进程占用**：`pkill -f vite` 后重启服务（vite preview 无 `strictPort`，占用会导致启动失败）
- **页面能开但数据空白**：确认后端 `copixiv-backend.service` 在 9000 端口运行，`curl http://127.0.0.1:5173/api/system/config` 应有 JSON
- **CORS**：生产访问走同源 `/api` 代理，不依赖浏览器跨域；后端白名单默认允许 `localhost/127.0.0.1:5173/4173`
