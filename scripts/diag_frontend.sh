#!/usr/bin/env bash
# 前端启动失败诊断：一次性收集环境、构建报错、systemd 日志。
# 用法：bash scripts/diag_frontend.sh   （输出贴回给 agent）
set -uo pipefail

cd /home/invocation/copixiv-v2/frontend

echo "== node / npm 版本 =="
node --version || echo "node missing"
npm --version || echo "npm missing"

echo
echo "== 5173 端口占用 =="
ss -tlnp 2>/dev/null | grep 5173 || echo "5173 空闲（无占用进程）"

echo
echo "== npm run build（手动复现构建错误）=="
npm run build 2>&1 | tee /tmp/frontend-build.log
BUILD_RC=${PIPESTATUS[0]}
echo "build 退出码: $BUILD_RC"

echo
echo "== 手动 preview 冒烟（5 秒后自动杀掉）=="
timeout 5 npm run preview > /tmp/frontend-preview.log 2>&1
PREVIEW_RC=$?
cat /tmp/frontend-preview.log
echo "preview 退出码: $PREVIEW_RC (124=被 timeout 杀掉=启动成功)"

echo
echo "== systemd 单元状态与日志 =="
systemctl status copixiv-frontend.service --no-pager 2>&1 | head -n 20
journalctl -xeu copixiv-frontend.service -n 40 --no-pager 2>&1 | tail -n 40

echo
echo "== 汇总 =="
echo "build=$BUILD_RC preview_smoke=$PREVIEW_RC"
if [ "$BUILD_RC" -ne 0 ]; then
  echo "→ 问题在构建阶段，看上方 npm run build 输出（/tmp/frontend-build.log）"
elif [ "$PREVIEW_RC" -eq 124 ]; then
  echo "→ 构建与 preview 都正常，问题在 systemd 环境（看 journal 段）"
else
  echo "→ preview 手动也失败，看 /tmp/frontend-preview.log"
fi
