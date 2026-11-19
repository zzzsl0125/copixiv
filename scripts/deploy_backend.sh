#!/usr/bin/env bash
# deploy_backend.sh — 构建后端/Rust 扩展并重启 copixiv systemd 服务。
#
# 以普通用户执行（构建与清理阶段不碰 root，避免污染 .venv 文件属主），
# 仅在重启服务时通过 sudo 调用 systemctl。
#
# 用法：
#   ./scripts/deploy_backend.sh          # 默认 editable：以后 src/ 改动只需重启
#   ./scripts/deploy_backend.sh --plain  # 普通安装（与 pip install . 一致）

set -euo pipefail

if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
  echo "!! 请勿以 root/sudo 运行整个脚本（会导致 .venv 产生 root 属主文件）。请以普通用户运行。" >&2
  exit 1
fi

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_BIN="$REPO/.venv/bin"
VENV_PY="$VENV_BIN/python"
UNIT_NAME="copixiv.service"
EDITABLE=1
if [[ "${1:-}" == "--plain" ]]; then
  EDITABLE=0
fi

export PATH="$HOME/.cargo/bin:$PATH"

echo "==> [1/5] 安装/刷新 copixiv Python 包到 .venv"
if (( EDITABLE )); then
  "$VENV_PY" -m pip install --no-deps -e "$REPO"
else
  "$VENV_PY" -m pip install --no-deps "$REPO"
fi

echo "==> [2/5] 编译并安装 Rust 检索引擎 (copixiv_rs --release)"
if [[ ! -x "$VENV_BIN/maturin" ]]; then
  echo "!! 找不到 $VENV_BIN/maturin，请先安装开发依赖（pip install -e '.[dev]'）。" >&2
  exit 1
fi
"$VENV_BIN/maturin" develop --release --manifest-path "$REPO/crates/copixiv_rs/Cargo.toml"

echo "==> [3/5] 清理孤儿 .pyc"
find "$REPO/src" "$REPO/.venv/lib" \
  -type d -name __pycache__ -path "*copixiv*" 2>/dev/null | while IFS= read -r dir; do
    shopt -s nullglob
    for pyc in "$dir"/*.pyc; do
      base="$(basename "$pyc")"
      mod="${base%%.*}"
      if [[ ! -e "$dir/../$mod.py" && ! -d "$dir/../$mod" ]]; then
        echo "    removing orphan pyc: $pyc"
        rm -f "$pyc"
      fi
    done
  done

echo "==> [4/5] 重启后端服务 ($UNIT_NAME)"
START_TS="$(date '+%Y-%m-%d %H:%M:%S')"
sudo systemctl restart "$UNIT_NAME"

echo "==> [5/5] 等待服务就绪（检查 $START_TS 之后的启动日志）"
READY=0
for _ in $(seq 1 30); do
  if systemctl is-active --quiet "$UNIT_NAME" && \
     journalctl -u "$UNIT_NAME" --since "$START_TS" --no-pager 2>/dev/null | grep -q "TaskManagerSystem started"; then
    READY=1
    break
  fi
  sleep 1
done

if (( ! READY )); then
  echo "!! 超时：服务未在 30 秒内打印 TaskManagerSystem started，请查看 journalctl -xeu $UNIT_NAME" >&2
  exit 1
fi

echo "OK: 后端与 Rust 扩展已就绪。"
journalctl -u "$UNIT_NAME" --since "$START_TS" --no-pager 2>/dev/null \
  | grep "Registered cron job" | tail -10 || true
