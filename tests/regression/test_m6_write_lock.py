"""M6 复现：API 写路径必须通过 ``get_write_uow`` 写入边界。

（M10 的 application 层纯度检查已随精简重构删除；全局写锁已随
postgres-migration 移除——``get_write_uow`` 现在只是 ``db_write()``
标记，不在 M6 断言全局互斥。）
"""

import ast
from pathlib import Path

import pytest


def test_write_endpoints_declare_write_uow():
    """期望：所有写端点声明 Depends(get_write_uow) 而非 get_uow。

    读端点保持 get_uow。用 AST 全量扫描 endpoints 目录（而非字符串匹配），
    新增端点文件也在检查范围内。
    """
    src_root = Path(__file__).resolve().parents[2] / "src" / "copixiv"
    # web_api no longer exists — write endpoints now live in features + tasks.
    scan_dirs = [
        src_root / "tasks",
        src_root / "features",
    ]

    # Map: endpoint file (without .py) → function name → does its signature
    # contain Depends(get_write_uow)?
    write_uow_users: dict[str, set[str]] = {}
    for py_file in sorted(
        p for d in scan_dirs for p in d.rglob("*.py")
    ):
        if py_file.name == "__init__.py":
            continue
        tree = ast.parse(py_file.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            defaults = [
                d for d in node.args.defaults + node.args.kw_defaults
                if isinstance(d, ast.AST)
            ]
            for default in defaults:
                for call in ast.walk(default):
                    if (
                        isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Name)
                        and call.func.id == "Depends"
                        and call.args
                        and isinstance(call.args[0], ast.Name)
                        and call.args[0].id == "get_write_uow"
                    ):
                        write_uow_users.setdefault(
                            py_file.name, set(),
                        ).add(node.name)

    # 每个写端点的签名必须包含 Depends(get_write_uow)
    for fn in (
        "toggle_favourite", "toggle_special_follow", "delete_novel",
        "create_scheduled_task", "update_scheduled_task",
        "delete_scheduled_task", "reorder_scheduled_tasks",
        "create_token", "update_token", "delete_token", "reorder_tokens",
        "create_tag_preference", "update_tag_preference",
        "delete_tag_preference", "reorder_tag_preferences",
        "create_tag_alias", "delete_tag_alias",
        "clear_search_history", "delete_search_history",
    ):
        found = any(fn in fns for fns in write_uow_users.values())
        assert found, f"写端点 {fn} 未声明 Depends(get_write_uow)"
