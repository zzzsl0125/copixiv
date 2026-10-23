"""跨切片私有引用的守卫 —— "公开的规则住在自己家里，而不是别人的私有函数里"。

这条规则不是我凭空立的：审查 2026-10-24 时全仓有 **5 处**从一个切片 import
另一个切片的私有函数，每一处都是同一种信号——**某个公开规则被放在了错误的
owner 上**。

| 私有引用 | 真实问题 | 处理 |
|---|---|---|
| `tasks/novels.py` → `novels.ingest._month_ranges` | 纯日期工具住在按小说切片的抓取管线里 | 移到 `core/calendar.py` |
| `tasks/novels.py` → `novels.ingest._filter_chinese_novels` | 只被任务层使用的列表封装 | 移到 `core/language.py` |
| `library/api.py` → `novels.update_metadata._author_id` | 三个**不同切片**共用的校验规则 | 移到 `core/ids.py` |
| `novels/batch_edit_metadata.py` → 同上 | 同上 | 同上 |
| `export_openapi.py` → `app._mount_routers` | 它有第二个**合法**调用方，下划线在撒谎 | 改名 `mount_routers` |

两种正确的出口都在这条规则里：

* 规则真的通用 → 搬到 ``core/``（或放到一个公开名字上）；
* 它确实是本切片的内部细节 → 就别让别的切片用它。

**允许的例外**：同一个切片 / 包内部（``features/novels`` 自己的两个文件之间、
``tasks/maintenance`` 包内的模块之间）用私有名是正常的封装，不在这里执法。

执法方式与 ``test_library_isolation.py`` / ``test_vendor_whitelist.py`` 一致：
AST 扫描 ``src/copixiv``，任何跨 owner 的私有 import 立刻失败。
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "copixiv"


def _owner(py_file: Path, root: Path | None = None) -> str:
    """Which slice a file belongs to.

    ``features/novels/*`` → ``features.novels``; ``tasks/maintenance/*`` →
    ``tasks.maintenance``; top-level modules → their own name.  Two files with
    the same owner may share private names.
    """
    parts = py_file.relative_to(root or SRC).parts
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0].removesuffix(".py")
    if parts[0] in ("features", "tasks") and len(parts) > 2:
        return f"{parts[0]}.{parts[1]}"
    return parts[0]


def _imported_owner(module: str) -> str:
    parts = module.split(".")[1:]          # drop the leading ``copixiv``
    if not parts:
        return ""
    if parts[0] in ("features", "tasks") and len(parts) > 2:
        return f"{parts[0]}.{parts[1]}"
    return parts[0]


def _violations(py_file: Path, root: Path | None = None) -> list[str]:
    root = root or SRC
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    owner = _owner(py_file, root)
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        if not module.startswith("copixiv."):
            continue
        if _imported_owner(module) == owner:
            continue                       # same slice: private is fine
        for alias in node.names:
            name = alias.name
            if name.startswith("_") and not name.startswith("__"):
                rel = py_file.relative_to(root)
                found.append(f"{rel}:{node.lineno} -> {module}.{name}")
    return found


def test_no_slice_imports_another_slices_private_names():
    violations: list[str] = []
    for py_file in sorted(SRC.rglob("*.py")):
        if "__pycache__" in py_file.parts:
            continue
        violations.extend(_violations(py_file))

    assert violations == [], (
        "跨切片导入了私有名字。要么把规则搬到 core/（或在 owner 上改成公开名），"
        "要么别让别的切片用它：\n" + "\n".join(violations)
    )


def test_the_guard_itself_catches_a_planted_violation(tmp_path):
    """守卫必须先证明自己会红 —— 否则它就是一条永远绿的摆设。"""
    planted = tmp_path / "features" / "other" / "api.py"
    planted.parent.mkdir(parents=True)
    planted.write_text(
        "from copixiv.features.novels.update_metadata import _author_id\n",
        encoding="utf-8",
    )

    assert _violations(planted, tmp_path) == [
        "features/other/api.py:1 -> "
        "copixiv.features.novels.update_metadata._author_id"
    ]
