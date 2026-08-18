#!/usr/bin/env bash
# deploy_backend.sh — 把当前仓库代码部署到 copixiv-backend 服务。
#
# 后端没有 build 步骤；需要的是「重装 venv 包 + 清理孤儿 pyc + 重启服务」。
# 参考事故：venv 里装着旧版 copixiv（8-17 03:17），src/ 已更新但没重装/重启，
# 定时任务触发时旧 scheduler 查不到任务函数 → "Unknown task function"。
#
# 用法（需要 root/sudo，因为要 systemctl restart）：
#   sudo ./scripts/deploy_backend.sh          # 默认 editable：以后 src/ 改动只需重启
#   sudo ./scripts/deploy_backend.sh --plain  # 退化为普通重装（与 README 的 pip install . 一致）
#
# 依赖：repo 根目录下有 .venv（装好 copixiv 依赖）。

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PY="$REPO/.venv/bin/python"
EDITABLE=1
if [[ "${1:-}" == "--plain" ]]; then
  EDITABLE=0
fi

echo "==> [1/4] 重装 copixiv 包到 venv"
if (( EDITABLE )); then
  "$VENV_PY" -m pip install -e "$REPO"
else
  "$VENV_PY" -m pip install "$REPO"
fi

echo "==> [2/4] 清理孤儿 .pyc（源码已删除、仅剩字节码的模块会被 Python 误加载）"
find "$REPO/src" "$REPO/.venv/lib/python3.12/site-packages" \
  -type d -name __pycache__ -path "*copixiv*" 2>/dev/null | while IFS= read -r dir; do
    shopt -s nullglob
    for pyc in "$dir"/*.cpython-312.pyc; do
      base="$(basename "$pyc" .cpython-312.pyc)"
      if [[ ! -e "$dir/../$base.py" ]]; then
        echo "    removing orphan pyc: $pyc"
        rm -f "$pyc"
      fi
    done
  done

echo "==> [3/4] 重启后端服务"
systemctl restart copixiv-backend

echo "==> [4/4] 自检（重启后应出现 copixiv.tasks.manager，而不是旧 copixiv.tasks.scheduler）"
for i in $(seq 1 15); do
  if systemctl is-active --quiet copixiv-backend && \
     journalctl -u copixiv-backend --since "30 seconds ago" --no-pager 2>/dev/null | grep -q "TaskManagerSystem started"; then
    break
  fi
  sleep 1
done

if journalctl -u copixiv-backend --since "1 minute ago" --no-pager 2>/dev/null | grep -q "copixiv.tasks.scheduler"; then
  echo "!! 警告：启动日志仍出现旧的 copixiv.tasks.scheduler —— 部署可能未生效！"
  exit 1
fi
echo "OK: 后端已重启，调度器为新 TaskManagerSystem（$(journalctl -u copixiv-backend --since '1 minute ago' --no-pager | grep -c 'TaskManagerSystem started') 次启动记录）"

# 查看最近注册的 cron 任务，确认三个启用任务都在
journalctl -u copixiv-backend --since "1 minute ago" --no-pager 2>/dev/null \
  | grep "Registered cron job" | tail -10 || true