"""本地书上传播的写路径隔离守卫（docs/LIBRARY_DESIGN.md §4 硬边界）。

**本地行永不进 ``_upsert_rows``。** 抓取路径的写仓库
（``features/novels/repo.py`` 的 ``_upsert_rows`` / ``upsert_novels``）无条件
覆盖白名单只服务 pixiv 刷新；「我的上传」切片必须只做 INSERT。

本文件用 AST 扫 ``features/library/``：出现 ``_upsert_rows`` /
``upsert_novels`` 名字（调用或属性访问）即失败，import 抓取写仓库同样失败。
行为级验证另见 ``tests/features/test_library_ingest.py::
test_local_row_untouched_by_pixiv_upsert``（写法与
``tests/architecture/test_vendor_whitelist.py`` 一致）。
"""

import ast
from pathlib import Path

LIBRARY_DIR = (
    Path(__file__).resolve().parents[2] / "src" / "copixiv" / "features" / "library"
)

# 抓取刷新路径的入口 —— 上传切片一个都不许碰。
FORBIDDEN_NAMES = {"_upsert_rows", "upsert_novels"}
FORBIDDEN_MODULES = {"copixiv.features.novels.repo"}


def _module_files():
    for py_file in sorted(LIBRARY_DIR.rglob("*.py")):
        if "__pycache__" in py_file.parts:
            continue
        yield py_file


def _violations(py_file: Path) -> list[str]:
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            found.append(f"{node.lineno}: 引用 {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_NAMES:
            found.append(f"{node.lineno}: 引用 .{node.attr}")
        elif isinstance(node, ast.ImportFrom) and node.module in FORBIDDEN_MODULES:
            found.append(f"{node.lineno}: import {node.module}")
    return found


def test_library_slice_never_touches_the_pixiv_upsert_path():
    violations = []
    for py_file in _module_files():
        for violation in _violations(py_file):
            rel = py_file.relative_to(LIBRARY_DIR.parent.parent.parent)
            violations.append(f"{rel}:{violation}")
    assert violations == [], (
        "「我的上传」写路径只许 INSERT，不得进 _upsert_rows/upsert_novels：\n"
        + "\n".join(violations)
    )
