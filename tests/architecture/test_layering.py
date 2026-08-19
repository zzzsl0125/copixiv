"""架构边界测试 —— 执行 docs/MODULARITY.md §2 的硬规则。

两组规则，全部基于 AST 扫描 ``src/copixiv``：

1. **分层 import 矩阵**（§2.1）：每层只能 import 矩阵允许的
   copixiv 顶层包。
2. **厂商白名单**（§2.2）：``pixivpy3`` 只准出现在
   ``infrastructure/pixiv/patch.py`` 与 ``account.py``。

写锁与 application 纯度由 tests/regression/ 钉死（§2.3 / §2.4）；
具体实现类的 import 位置是**约定**而非规则（§3，无执法测试）。

任何新模块违规都会在这里立刻失败——边界靠测试钉死，不靠自觉。
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "copixiv"

# 平台模块（docs §M0）：任何层都可 import，不属于任何层。
SHARED = {"log"}
LAYERS = {"domain", "application", "infrastructure", "tasks", "web_api", "app"}

# docs/MODULARITY.md §2.1 的分层矩阵
ALLOWED: dict[str, set[str]] = {
    "domain": {"domain"} | SHARED,
    "application": {"domain", "application"} | SHARED,
    "infrastructure": {"domain", "infrastructure"} | SHARED,
    "tasks": {"domain", "application", "infrastructure", "tasks"} | SHARED,
    "web_api": {
        "domain", "application", "infrastructure", "tasks", "web_api",
    } | SHARED,
    # app = 组合根，允许依赖一切
    "app": LAYERS | SHARED,
}

# docs/MODULARITY.md §2.2 厂商白名单（相对 src/copixiv 的路径）
PIXIVPY3_WHITELIST = {
    "infrastructure/pixiv/patch.py",
    "infrastructure/pixiv/account.py",
    # 异常层次：必须继承 pixivpy3.PixivError 才能被既有 except 链捕获
    "infrastructure/pixiv/errors.py",
}


def _module_files():
    for py_file in sorted(SRC.rglob("*.py")):
        if "__pycache__" in py_file.parts:
            continue
        yield py_file


def _layer_of(py_file: Path) -> str | None:
    """Top-level layer of a module; None for platform/root modules."""
    rel = py_file.relative_to(SRC)
    parts = rel.parts
    if len(parts) < 2 or parts[0] not in LAYERS:
        return None
    return parts[0]


def _copixiv_imports(py_file: Path):
    """Yield ``(top_level_name, lineno)`` for copixiv.* imports.

    Relative imports (``from .x``) are skipped — they are intra-package
    by construction.
    """
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("copixiv."):
                    yield alias.name.split(".")[1], node.lineno
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "copixiv":
                for alias in node.names:
                    yield alias.name.split(".")[0], node.lineno
            elif node.module.startswith("copixiv."):
                yield node.module.split(".")[1], node.lineno


def _pixivpy3_imports(py_file: Path):
    """Yield ``(module, lineno)`` for any pixivpy3 import."""
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "pixivpy3" or alias.name.startswith("pixivpy3."):
                    yield alias.name, node.lineno
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("pixivpy3")
        ):
            yield node.module, node.lineno


# ---------------------------------------------------------------------------
# §2.1 分层 import 矩阵
# ---------------------------------------------------------------------------


def test_layer_import_matrix():
    violations = []
    for py_file in _module_files():
        layer = _layer_of(py_file)
        if layer is None:
            continue  # 平台/根模块豁免
        for top, lineno in _copixiv_imports(py_file):
            if top not in LAYERS and top not in SHARED:
                violations.append(
                    f"{py_file.relative_to(SRC)}:{lineno}: "
                    f"未知的 copixiv 顶层模块 '{top}'"
                )
            elif top not in ALLOWED[layer]:
                violations.append(
                    f"{py_file.relative_to(SRC)}:{lineno}: "
                    f"层 '{layer}' 不允许 import 顶层模块 '{top}'"
                )
    assert violations == [], "分层矩阵违规:\n" + "\n".join(violations)


# ---------------------------------------------------------------------------
# §2.2 pixivpy3 厂商白名单
# ---------------------------------------------------------------------------


def test_pixivpy3_vendor_whitelist():
    violations = []
    for py_file in _module_files():
        rel = str(py_file.relative_to(SRC))
        for module, lineno in _pixivpy3_imports(py_file):
            if rel not in PIXIVPY3_WHITELIST:
                violations.append(
                    f"{rel}:{lineno}: imports {module} —— "
                    f"pixivpy3 只允许出现在 {sorted(PIXIVPY3_WHITELIST)}"
                )
    assert violations == [], "厂商边界违规:\n" + "\n".join(violations)
