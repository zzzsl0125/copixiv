#!/usr/bin/env bash
# Coverage gate — turns "which modules have no tests" into a repeatable number.
#
# Why a script: the numbers are only useful if everyone re-runs them the same way.
# 2026-10-24 的模块审查发现，靠端点测试数粗估会把 `tasks/novels.py` 判成"够测了"，
# 而它实际只有 31%（123 行未覆盖）。
#
# Baseline (2026-10-24, fast suite): TOTAL 89%.
# Threshold 85% leaves room for splitting modules without rewriting their tests.
#
# Usage:  scripts/check_coverage.sh              # fast suite, 85% gate
#         scripts/check_coverage.sh -k library   # one slice
#         COVERAGE_SLOW=1 scripts/check_coverage.sh   # include -m slow
set -euo pipefail

cd "$(dirname "$0")/.."

ARGS=(.venv/bin/python -m pytest -q
      --cov=src/copixiv --cov-report=term-missing --cov-fail-under=85)
if [ "${COVERAGE_SLOW:-0}" != "1" ]; then
    ARGS+=(-m "not slow")
fi

echo "== coverage gate =="
"${ARGS[@]}" "$@" 2>&1 | tail -n 70
