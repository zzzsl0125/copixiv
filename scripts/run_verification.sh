#!/usr/bin/env bash
# 一键验证：Rust 格式/Clippy/单测 → 后端 lint/类型/分层/OpenAPI/测试与覆盖率/迁移一致性 → 前端 codegen/lint/单测/构建。
# 只读验证，不触碰生产库与工作区源码；alembic check 固定指向测试库（copixiv_test）。
#
# 用法：
#   bash scripts/run_verification.sh
set -uo pipefail

cd "$(dirname "$0")/.."
export PATH="$HOME/.cargo/bin:$PATH"
VENV_BIN="${VENV_BIN:-$PWD/.venv/bin}"
TEST_DB_URL="${COPIXIV_TEST_DATABASE_URL:-postgresql+psycopg2://postgres@127.0.0.1:5433/copixiv_test}"

TMP_OPENAPI="$(mktemp /tmp/copixiv-openapi.XXXXXX.json)"
TMP_CODEGEN="$(mktemp /tmp/copixiv-api.generated.XXXXXX.ts)"
trap 'rm -f "$TMP_OPENAPI" "$TMP_CODEGEN"' EXIT

fail() { echo "!! $1"; exit 1; }

echo "== 1/8 Rust 格式、Clippy 与单测（cargo fmt / clippy / test） =="
if command -v cargo >/dev/null 2>&1; then
  cargo fmt --manifest-path crates/copixiv_rs/Cargo.toml --check || fail "cargo fmt"
  PYO3_PYTHON="$VENV_BIN/python" cargo clippy --manifest-path crates/copixiv_rs/Cargo.toml --all-targets -- -D warnings || fail "cargo clippy"
  PYO3_PYTHON="$VENV_BIN/python" cargo test --manifest-path crates/copixiv_rs/Cargo.toml || fail "cargo test"
  echo "Rust OK"
else
  fail "cargo not found (expected Rust toolchain in PATH or ~/.cargo/bin)"
fi

echo
echo "== 2/8 后端 lint（ruff） =="
"$VENV_BIN/ruff" check src tests scripts || fail "ruff"
"$VENV_BIN/ruff" format --check src tests scripts || fail "ruff format"
echo "ruff OK"

echo
echo "== 3/8 后端类型（mypy） =="
"$VENV_BIN/mypy" src/copixiv || fail "mypy"
echo "mypy OK"

echo
echo "== 4/8 后端分层契约（import-linter） =="
"$VENV_BIN/lint-imports" || fail "import-linter"
echo "import-linter OK"

echo
echo "== 5/8 OpenAPI 契约一致性（export_openapi） =="
"$VENV_BIN/python" -m copixiv.export_openapi > "$TMP_OPENAPI" || fail "export_openapi"
diff -u frontend/openapi.json "$TMP_OPENAPI" || fail "openapi.json out of date"
echo "openapi OK"

echo
echo "== 6/8 后端测试与覆盖率（pytest） =="
"$VENV_BIN/pytest" tests -v --cov=copixiv --cov-report=term-missing:skip-covered || fail "pytest"
echo "pytest OK"

echo
echo "== 7/8 数据库迁移一致性（alembic check on test DB） =="
"$VENV_BIN/alembic" -x "url=$TEST_DB_URL" check || fail "alembic check"
echo "alembic check OK"

echo
echo "== 8/8 前端 codegen 一致性 + lint + 单测 + 类型检查 + 构建 =="
(
  cd frontend
  ./node_modules/.bin/openapi-typescript openapi.json -o "$TMP_CODEGEN" || exit 1
  diff -u src/types/api.generated.ts "$TMP_CODEGEN" || exit 1
  npm run lint || exit 1
  npm run test || exit 1
  npm run build
) || fail "frontend"
echo "前端 OK"

echo
echo "全部验证通过 ✅"
