#!/usr/bin/env bash
# 一键验证：Rust 单测 → 后端 lint/类型/分层/OpenAPI 契约/测试 → 前端 codegen 校验 + 构建。
# 只读验证，不触碰 systemd 部署状态与工作区源码；部署步骤见 README 第 4 节。
#
# 用法：
#   bash scripts/run_verification.sh
set -uo pipefail

cd "$(dirname "$0")/.."
export PATH="$HOME/.cargo/bin:$PATH"

TMP_OPENAPI="$(mktemp /tmp/copixiv-openapi.XXXXXX.json)"
TMP_CODEGEN="$(mktemp /tmp/copixiv-api.generated.XXXXXX.ts)"
trap 'rm -f "$TMP_OPENAPI" "$TMP_CODEGEN"' EXIT

fail() { echo "!! $1"; exit 1; }

echo "== 1/7 Rust 单测（cargo test） =="
if command -v cargo >/dev/null 2>&1; then
  PYO3_PYTHON="$PWD/.venv/bin/python" cargo test --manifest-path crates/copixiv_rs/Cargo.toml || fail "cargo test"
  echo "cargo test OK"
else
  fail "cargo not found (expected Rust toolchain in PATH or ~/.cargo/bin)"
fi

echo
echo "== 2/7 后端 lint（ruff） =="
.venv/bin/ruff check src tests scripts || fail "ruff"
echo "ruff OK"

echo
echo "== 3/7 后端类型（mypy） =="
.venv/bin/mypy src/copixiv || fail "mypy"
echo "mypy OK"

echo
echo "== 4/7 后端分层契约（import-linter） =="
.venv/bin/lint-imports || fail "import-linter"
echo "import-linter OK"

echo
echo "== 5/7 OpenAPI 契约一致性（export_openapi） =="
.venv/bin/python -m copixiv.export_openapi > "$TMP_OPENAPI" || fail "export_openapi"
diff -u frontend/openapi.json "$TMP_OPENAPI" || fail "openapi.json out of date"
echo "openapi OK"

echo
echo "== 6/7 后端测试（pytest） =="
.venv/bin/pytest tests -v || fail "pytest"
echo "pytest OK"

echo
echo "== 7/7 前端 codegen 一致性 + 类型检查 + 构建 =="
(
  cd frontend
  ./node_modules/.bin/openapi-typescript openapi.json -o "$TMP_CODEGEN" || exit 1
  diff -u src/types/api.generated.ts "$TMP_CODEGEN" || exit 1
  npm run build
) || fail "frontend"
echo "前端 OK"

echo
echo "全部验证通过 ✅"
