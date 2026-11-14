#!/usr/bin/env bash
# 一键验证：后端 lint/类型/分层/测试 → 前端类型检查 + 构建。
# 只读验证，不触碰 systemd 部署状态；部署步骤见 README 第 5 节。
#
# 用法：
#   bash scripts/run_verification.sh
set -uo pipefail

cd "$(dirname "$0")/.."

fail() { echo "!! $1"; exit 1; }

echo "== 1/5 后端 lint（ruff） =="
.venv/bin/ruff check src tests scripts || fail "ruff"
echo "ruff OK"

echo
echo "== 2/5 后端类型（mypy） =="
.venv/bin/mypy src/copixiv || fail "mypy"
echo "mypy OK"

echo
echo "== 3/5 后端分层契约（import-linter） =="
.venv/bin/lint-imports || fail "import-linter"
echo "import-linter OK"

echo
echo "== 4/5 后端测试（pytest） =="
.venv/bin/pytest tests -v || fail "pytest"
echo "pytest OK"

echo
echo "== 5/5 前端类型检查 + 构建 =="
(cd frontend && npm run build) || fail "build"
echo "前端 OK"

echo
echo "全部验证通过 ✅"
