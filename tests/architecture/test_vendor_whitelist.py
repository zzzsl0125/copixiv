"""厂商边界测试 —— pixivpy3 只准出现在少数适配文件里。

pixivpy3 是本项目唯一的外部厂商依赖，它的位置是**硬边界**。本模块用
AST 扫描整个 ``src/copixiv``，任何新模块 import pixivpy3 都会立刻失败。

（分层 import 矩阵已随精简重构删除，本文件只保留此一条 vendor 白名单。）
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "copixiv"

# pixivpy3 厂商白名单（相对 src/copixiv 的路径）
PIXIVPY3_WHITELIST = {
    "pixiv/patch.py",
    "pixiv/account.py",
    # 异常层次：必须继承 pixivpy3.PixivError 才能被既有 except 链捕获
    "pixiv/errors.py",
}


def _module_files():
    for py_file in sorted(SRC.rglob("*.py")):
        if "__pycache__" in py_file.parts:
            continue
        yield py_file


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
