#!/usr/bin/env bash
# build_pg_bigm.sh — 把 pg_bigm 编译进 pgserver 自带的那套 PostgreSQL。
#
# 为什么需要它：迁移 0007 起，关键词搜索底座是 pg_bigm 的 bigram GIN 索引，
# 而 **pgserver 的 wheel 只带 plpgsql 与 vector，不含 pg_bigm**（0.1.4 实测：
# extension/ 下 35 个条目，无 bigm）。缺了它，`CREATE EXTENSION pg_bigm` 直接
# 失败 —— 建库迁移、tests/adapters/test_migrations.py 的 fresh-DB 用例、
# conftest 的建库全都会挂，所以 CI 与任何新机器都必须先跑本脚本。
#
# 幂等：已装好就直接退出（exit 0），可以无脑放在安装步骤后面。
#
# 用法：
#   bash scripts/build_pg_bigm.sh              # 装进 pgserver 的 pginstall
#   PG_BIGM_VERSION=v1.2-20250903 bash scripts/build_pg_bigm.sh
#
# 依赖：gcc / make / git（或 curl）。目标 PG 必须带 server 头文件与 PGXS ——
# pgserver 的 wheel 两者都有，但**发行版 PostgreSQL 的 -dev/-server 包常常只给
# pg_config 不给头文件**。所以装了系统 PG 的机器上可能仍需发行版的
# postgresql-16-pg-bigm 包，而不是走这里。本脚本优先认 pgserver 自带的那套。
#
# ⚠️ pginstall 是 pgserver 的一部分：`pip install -U pgserver` 会整个替换它，
# pg_bigm.so 随之消失 → 写 novel 会失败。升级 pgserver 后要重跑本脚本。
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

# 固定到 tag 对应的提交 SHA（tag 理论上可被移动，SHA 不会）。
PG_BIGM_VERSION="${PG_BIGM_VERSION:-v1.2-20250903}"
PG_BIGM_SHA="${PG_BIGM_SHA:-735dceba0ecdd8ac1aaaaa207226a7102b6bbd71}"
PG_BIGM_REPO="${PG_BIGM_REPO:-https://github.com/pgbigm/pg_bigm.git}"

# 复用 pg_dev.py 的定位逻辑：优先「当前解释器里的 pgserver」，回退仓库 .venv。
# CI 把依赖装进 runner 解释器（没有 repo-local .venv），本地开发用 .venv —— 两种
# 布局都要认，否则本脚本在 CI 上会指向一个不存在的路径。
if [ -n "${PG_BIN_DIR:-}" ]; then
  BIN_DIR="$PG_BIN_DIR"
else
  BIN_DIR="$(python - <<'PY'
import importlib.util
import sys
from pathlib import Path

spec = importlib.util.find_spec("pgserver")
if spec is not None and spec.origin:
    candidate = Path(spec.origin).resolve().parent / "pginstall" / "bin"
    if candidate.exists():
        print(candidate)
        sys.exit(0)
sys.exit(1)
PY
)" || {
    echo "!! 找不到 pgserver 自带的 PostgreSQL。" >&2
    echo "   CI/新机器：先 pip install \".[dev]\"（含 pgserver）。" >&2
    echo "   系统 PG：本脚本不适用 —— 请装发行版的 pg_bigm 包" >&2
    echo "   （Arch: postgresql-pg-bigm / Debian: postgresql-16-pg-bigm）。" >&2
    exit 1
  }
fi

PG_CONFIG="$BIN_DIR/pg_config"
[ -x "$PG_CONFIG" ] || { echo "!! 缺少 $PG_CONFIG" >&2; exit 1; }

SHAREDIR="$("$PG_CONFIG" --sharedir)"
PKGLIBDIR="$("$PG_CONFIG" --pkglibdir)"
EXT_DIR="$SHAREDIR/extension"
SO_DIR="$PKGLIBDIR"

echo "== pg_bigm 构建前检查 =="
echo "   pg_config    : $PG_CONFIG"
echo "   PostgreSQL   : $("$PG_CONFIG" --version)"
echo "   extension dir: $EXT_DIR"
echo "   目标版本     : $PG_BIGM_VERSION ($PG_BIGM_SHA)"

# ---- 幂等检查：control + .so 都在就算装好了 -------------------------------
if [ -f "$EXT_DIR/pg_bigm.control" ] && [ -f "$SO_DIR/pg_bigm.so" ]; then
  echo "== 已安装，跳过（幂等）=="
  exit 0
fi
# 只装了一半（比如上次构建中断）时继续往下走，重新装齐。

# ---- 头文件与 PGXS 是否齐备（发行版 -dev 包常常缺）----------------------
# 用 pg_config 自己报的路径，不要靠 sharedir 反推目录层级。
INCLUDE_SERVER="$("$PG_CONFIG" --includedir-server)"
if [ ! -f "$INCLUDE_SERVER/postgres.h" ]; then
  echo "!! 目标 PostgreSQL 没有 server 头文件（$INCLUDE_SERVER/postgres.h），无法编译扩展。" >&2
  echo "   若这是系统 PG，请改用发行版的 pg_bigm 包" >&2
  echo "   （Arch: postgresql-pg-bigm / Debian: postgresql-16-pg-bigm）。" >&2
  exit 1
fi
PGXS="$("$PG_CONFIG" --pgxs)"
[ -f "$PGXS" ] || { echo "!! 缺少 PGXS（$PGXS），无法用 PGXS 构建。" >&2; exit 1; }

command -v make >/dev/null || { echo "!! 缺少 make" >&2; exit 1; }
command -v gcc >/dev/null || command -v cc >/dev/null || { echo "!! 缺少 C 编译器" >&2; exit 1; }

# ---- 取源码（浅取指定 SHA，避免依赖 tag 不可移动 + 省流量）---------------
WORK="${PG_BIGM_SRC_DIR:-$ROOT/.spike/pgbuild}"
SRC="$WORK/pg_bigm"
if [ ! -f "$SRC/Makefile" ]; then
  echo "== 拉取源码到 $SRC =="
  rm -rf "$SRC"
  mkdir -p "$SRC"
  if command -v git >/dev/null; then
    # GitHub 允许按 SHA 浅取（allow-reachable-sha1-in-want）。
    git -C "$SRC" init -q
    git -C "$SRC" remote add origin "$PG_BIGM_REPO"
    if ! git -C "$SRC" fetch -q --depth 1 origin "$PG_BIGM_SHA" 2>/dev/null; then
      echo "   按 SHA 浅取失败，退回按 tag 取（$PG_BIGM_VERSION）"
      git -C "$SRC" fetch -q --depth 1 origin "refs/tags/$PG_BIGM_VERSION"
      git -C "$SRC" checkout -q FETCH_HEAD
    else
      git -C "$SRC" checkout -q FETCH_HEAD
    fi
  else
    echo "!! 没有 git；请手动把 pg_bigm $PG_BIGM_VERSION 解到 $SRC" >&2
    exit 1
  fi
fi
[ -f "$SRC/Makefile" ] || { echo "!! 源码不完整：$SRC" >&2; exit 1; }

# ---- 编译并安装到目标 PG --------------------------------------------------
# pg_bigm 依赖 contrib 的 ``.ph``（PostgreSQL 头部安装），PGXS 构建会自动带上。
# NLS 关掉：只影响报错信息的本地化，省掉 gettext 的构建期依赖。
echo "== 编译 =="
make -C "$SRC" USE_PGXS=1 PG_CONFIG="$PG_CONFIG" clean >/dev/null 2>&1 || true
make -C "$SRC" USE_PGXS=1 PG_CONFIG="$PG_CONFIG"
echo "== 安装到 $SO_DIR / $EXT_DIR =="
make -C "$SRC" USE_PGXS=1 PG_CONFIG="$PG_CONFIG" install

# ---- 验证 ----------------------------------------------------------------
[ -f "$SO_DIR/pg_bigm.so" ] || { echo "!! 安装后仍找不到 pg_bigm.so" >&2; exit 1; }
[ -f "$EXT_DIR/pg_bigm.control" ] || { echo "!! 安装后仍找不到 pg_bigm.control" >&2; exit 1; }
echo "== 完成：$("$PG_CONFIG" --version) 已具备 pg_bigm =="
echo "   提示：已运行的实例无需重启即可 CREATE EXTENSION；"
echo "   pip install -U pgserver 之后要重跑本脚本。"
