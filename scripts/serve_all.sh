#!/usr/bin/env bash
# copixiv 一体化启动器 —— 让 **一个 systemd 单元** 同时托管后端与前端。
#
# 背景：systemd 的一个 service 只能有一个主进程，而 copixiv 的生产形态是两个：
#   * FastAPI 后端      main.py        :9000
#   * 前端静态服务器    vite preview   :5173（并把 /api 反向代理到 :9000）
# 后端不托管 frontend/dist（只对小说文件用 FileResponse），所以两个进程都要有。
#
# 这里的做法是一个极小的 supervisor：
#   * 两个子进程都后台启动，本脚本自己是 systemd 的主进程；
#   * 任一子进程退出 -> 立即以该退出码结束，交给 unit 的 Restart=always 拉起；
#   * 收到 TERM/INT（systemctl stop）-> 转发给两个子进程后退出；
#   * 剩下的进程组清理交给 systemd（unit 里 KillMode=control-group），
#     这样 npm/vite 这类会再派生孙进程的情况也不会留孤儿。
#
# 数据库不在这里管生命周期：与原有 deploy 单元一致，由 ExecStartPre 调
# scripts/pg_dev.py start（幂等，已在运行则立即返回 0）。
#
# 脚本自定位（按自身路径推导仓库根），所以 unit 里不需要 WorkingDirectory。

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
FRONTEND="$ROOT/frontend"

log() { printf '[serve_all] %s\n' "$*"; }

if [ ! -x "$PY" ]; then
  log "FATAL: venv python not found at $PY"
  exit 1
fi

# 前端直接用 node 跑 vite：少一层 npm，进程树干净，SIGTERM 能直达服务器。
# （node_modules/.bin/vite 就是指向 vite/bin/vite.js 的符号链接。）
if [ -f "$FRONTEND/node_modules/vite/bin/vite.js" ]; then
  FRONTEND_CMD=(node "$FRONTEND/node_modules/vite/bin/vite.js" preview)
else
  FRONTEND_CMD=(npm run preview)
fi

BACKEND_PID=""
FRONTEND_PID=""

stop_all() {
  trap - TERM INT EXIT
  log "stopping children (backend=${BACKEND_PID:-none} frontend=${FRONTEND_PID:-none})"
  for pid in "$BACKEND_PID" "$FRONTEND_PID"; do
    [ -n "$pid" ] && kill -TERM "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap stop_all TERM INT EXIT

cd "$ROOT" || exit 1

log "start backend: $PY -u main.py"
"$PY" -u main.py &
BACKEND_PID=$!

log "start frontend: ${FRONTEND_CMD[*]} (cwd=$FRONTEND)"
( cd "$FRONTEND" && exec "${FRONTEND_CMD[@]}" ) &
FRONTEND_PID=$!

# 任一子进程退出即整体退出：systemd 按 Restart=always 重新拉起，
# 并按 KillMode=control-group 把 cgroup 里剩下的进程收掉。
wait -n "$BACKEND_PID" "$FRONTEND_PID"
RC=$?
log "a child exited (rc=$RC) — taking the unit down"
exit "$RC"
