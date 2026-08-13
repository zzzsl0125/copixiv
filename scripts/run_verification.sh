#!/usr/bin/env bash
# 一键验证：后端全量测试 + 前端单测 + 前端构建。
# 只读验证，不触碰 systemd 部署状态；部署步骤见 README 第 5 节。
#
# 用法：
#   bash scripts/run_verification.sh
set -uo pipefail

cd "$(dirname "$0")/.."

echo "== 1/3 后端全量测试 =="
if [ -x .venv/bin/python ]; then
  .venv/bin/python -m pytest tests/ -v 2>&1 | tee /tmp/pytest-full.log
  PYTEST_RC=${PIPESTATUS[0]}
else
  python -m pytest tests/ -v 2>&1 | tee /tmp/pytest-full.log
  PYTEST_RC=${PIPESTATUS[0]}
fi
if [ "$PYTEST_RC" -ne 0 ]; then
  echo "!! pytest 失败（exit=$PYTEST_RC），完整输出在 /tmp/pytest-full.log，请把尾部贴给 agent"
  exit 1
fi
echo "pytest OK"

echo
echo "== 2/3 前端单测（vitest） =="
(cd frontend && npm run test) || { echo "!! vitest 失败"; exit 1; }
echo "vitest OK"

echo
echo "== 3/3 前端构建（vue-tsc 类型检查 + vite build） =="
(cd frontend && npm run build) || { echo "!! build 失败"; exit 1; }
echo "build OK"

echo
echo "全部验证通过 ✅"
echo "下一步（部署 + FTS 回填）请按 README 第 5/6 节执行，并把结果回报。"
