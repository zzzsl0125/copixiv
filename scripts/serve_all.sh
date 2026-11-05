#!/usr/bin/env bash
# copixiv 启动器 —— **兼容旧 unit 的薄壳**。
#
# 2026-11-02 之前，这个脚本是一个双进程 supervisor：FastAPI 后端（:9000）+
# `node vite preview`（:5173，反代 /api 到 9000）。前端由独立 Node 进程伺服，
# 实测 141 MiB RSS / 118 MiB PSS —— 只为 716 KB 静态文件，且每次 unit 启动
# 还要跑一遍 `npm run build`（unit 峰值 893 MiB）。
#
# 现在后端自己伺服 `frontend/dist`（`copixiv.app.mount_frontend`）：一个进程、
# 一个端口（:9000）。新 unit 直接 `ExecStart=.../python -u main.py`，不再经过
# 本脚本；它保留下来只是为了让**尚未重新安装的旧 unit**（ExecStart 仍指向
# 这里）在代码更新后不会启动失败——旧 unit 若还在跑 `npm run build`，那是
# /etc 里 unit 文件的事，重新安装 deploy/copixiv.service 即可去掉。
#
# 数据库不在这里管生命周期：由 unit 的 ExecStartPre 调 scripts/pg_dev.py start
# （幂等，已在运行则立即返回 0）。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"

if [ ! -x "$PY" ]; then
  printf '[serve_all] FATAL: venv python not found at %s\n' "$PY" >&2
  exit 1
fi

cd "$ROOT" || exit 1

# exec：让 systemd 直接跟踪 Python，SIGTERM 不需要再转发一层。
exec "$PY" -u main.py
