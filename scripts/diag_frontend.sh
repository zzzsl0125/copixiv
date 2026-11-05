#!/usr/bin/env bash
# 前端诊断：构建产物是否正常 + 后端是否在伺服它。
#
# 2026-11-02 起前端由后端进程直接伺服（`copixiv.app.mount_frontend`），
# 没有 vite preview、没有 :5173、没有独立的 systemd 前端单元。
# 用法：bash scripts/diag_frontend.sh   （输出贴回给 agent）
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/frontend" || exit 1

echo "== node / npm 版本 =="
node --version || echo "node missing"
npm --version || echo "npm missing"

echo
echo "== 构建产物 =="
if [ -f dist/index.html ]; then
  echo "dist/index.html 存在，资源 $(find dist/assets -type f 2>/dev/null | wc -l) 个"
  ls -lh dist/index.html
else
  echo "dist/index.html 不存在 —— 需要 cd frontend && npm run build"
fi

echo
echo "== npm run build（手动复现构建错误）=="
npm run build 2>&1 | tee /tmp/frontend-build.log
BUILD_RC=${PIPESTATUS[0]}
echo "build 退出码: $BUILD_RC"

echo
echo "== 后端伺服冒烟（:9000）=="
curl -sS -o /dev/null -w 'GET /          -> %{http_code} %{content_type}\n' http://127.0.0.1:9000/ || echo "GET / 失败"
curl -sS -o /dev/null -w 'GET /api/...   -> %{http_code} %{content_type}\n' http://127.0.0.1:9000/api/system/config || echo "GET /api 失败"

echo
echo "== systemd 单元状态与日志 =="
systemctl status copixiv.service --no-pager 2>&1 | head -n 15
journalctl -xeu copixiv.service -n 30 --no-pager 2>&1 | tail -n 30

echo
echo "== 汇总 =="
echo "build=$BUILD_RC"
if [ "$BUILD_RC" -ne 0 ]; then
  echo "→ 问题在构建阶段，看 /tmp/frontend-build.log"
else
  echo "→ 构建正常；若页面 404/旧版，看上面 GET / 的状态码与 unit 日志"
fi
