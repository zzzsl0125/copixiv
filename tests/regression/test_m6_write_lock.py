"""M6 复现：API 写路径必须落在写事务边界里。

**写端点只有两种合法形态**（2026-09-22 收口）：

① 声明 ``Depends(get_write_uow)`` —— 请求级事务边界，今天 21 个写端点全走这条；
② 自己用 ``run_write_transaction`` 开写事务 —— 给"抓取 / 网络必须在写事务之外"
   的端点留的口子。这类端点必须登记在 :data:`SELF_MANAGED_WRITE_ENDPOINTS`，
   并由 :func:`test_self_managed_write_endpoints_are_declared` 三条断言证明形态②
   **真的成立**（用例模块里确有 ``run_write_transaction`` 调用）——否则这个清单
   就只是绕过事务边界的后门。

（M10 的 application 层纯度检查已随精简重构删除；全局写锁已随
postgres-migration 移除——``get_write_uow`` 现在只是 ``db_write()``
标记，不在 M6 断言全局互斥。）
"""

import ast
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "copixiv"

# 形态②的自管写事务端点：端点名 → (用例模块, 为什么它不能走 get_write_uow)。
# 用例模块是相对 ``src/copixiv/`` 的路径：测试会 AST 扫它、确认里面**真的**有
# ``run_write_transaction`` 调用。两件事写进同一个条目，是为了不给"清单"和
# "用例路径"各留一份、任由它们漂移。
SELF_MANAGED_WRITE_ENDPOINTS: dict[str, tuple[str, str]] = {
    "sync_novel": (
        "features/novels/sync_novel.py",
        "抓取在写事务之外：写事务由用例的 run_write_transaction 自己开"
        "（见 sync_novel.py 模块 docstring）",
    ),
}

# 路由装饰器的方法名（``@router.post(...)``）：用来认定"这个函数是端点"。
# ``@app.exception_handler(...)`` 一类的非路由装饰器不在其中。
_HTTP_METHODS = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "api_route"}
)


def _declares_get_write_uow(node) -> bool:
    """函数签名里是否有 ``= Depends(get_write_uow)``。"""
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
                return True
    return False


def _is_route_endpoint(node) -> bool:
    """函数是否被路由装饰器挂着（``@router.post(...)``）。"""
    return any(
        isinstance(deco, ast.Call)
        and isinstance(deco.func, ast.Attribute)
        and deco.func.attr in _HTTP_METHODS
        for deco in node.decorator_list
    )


def _scan_endpoint_functions():
    """AST 全量扫 ``tasks/`` + ``features/``（而非字符串匹配）。

    Returns:
        ``(route_endpoints, write_uow_users)`` —— 前者是"端点函数名 → 定义它的
        文件"（新端点文件自动进检查范围），后者是"文件 → 声明了
        ``Depends(get_write_uow)`` 的函数名"。
    """
    # web_api no longer exists — write endpoints now live in features + tasks.
    scan_dirs = [
        SRC_ROOT / "tasks",
        SRC_ROOT / "features",
    ]

    route_endpoints: dict[str, set[str]] = {}
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
            if _is_route_endpoint(node):
                route_endpoints.setdefault(node.name, set()).add(py_file.name)
            if _declares_get_write_uow(node):
                write_uow_users.setdefault(py_file.name, set()).add(node.name)

    return route_endpoints, write_uow_users


def _calls_run_write_transaction(py_file: Path) -> bool:
    """模块里是否有 ``run_write_transaction(...)`` **调用**。

    只认调用节点：光留着一条没用上的 import 不算"自己开写事务"。
    """
    tree = ast.parse(py_file.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "run_write_transaction":
            return True
        if isinstance(func, ast.Attribute) and func.attr == "run_write_transaction":
            return True
    return False


def test_write_endpoints_declare_write_uow():
    """期望：形态①的写端点声明 Depends(get_write_uow) 而非 get_uow。

    读端点保持 get_uow。用 AST 全量扫描（而非字符串匹配），新增端点文件也在
    检查范围内。形态②（自管写事务，如 ``sync_novel``）见
    :func:`test_self_managed_write_endpoints_are_declared`。
    """
    _, write_uow_users = _scan_endpoint_functions()

    # 每个写端点的签名必须包含 Depends(get_write_uow)
    for fn in (
        "toggle_favourite", "toggle_special_follow", "delete_novel",
        "update_novel_metadata",
        "create_scheduled_task", "update_scheduled_task",
        "delete_scheduled_task", "reorder_scheduled_tasks",
        "create_token", "update_token", "delete_token", "reorder_tokens",
        "create_tag_preference", "update_tag_preference",
        "delete_tag_preference", "reorder_tag_preferences",
        "create_tag_alias", "delete_tag_alias",
        "clear_search_history", "delete_search_history",
        "upload_novels",
    ):
        found = any(fn in fns for fns in write_uow_users.values())
        assert found, f"写端点 {fn} 未声明 Depends(get_write_uow)"


def test_self_managed_write_endpoints_are_declared():
    """形态②的例外必须被**证明**，不是被声明——三条断言，每条都能红。

    1. 清单里的函数名确实是扫得到的**路由端点**——端点改名 / 删掉，扫描的
       结果里就没有它，清单腐烂立刻报警；
    2. 这些端点**没有**声明 ``get_write_uow`` —— 两种形态互斥：形态②存在的
       理由正是"抓取 / 网络不能在写事务里"，顺手补一个请求级写事务就把它
       关回去了；
    3. 它们的用例模块里**确实有** ``run_write_transaction`` 调用 —— 这是
       "形态②真的成立"的证据。没有这一条，这个清单就是绕过事务边界的后门。
    """
    route_endpoints, write_uow_users = _scan_endpoint_functions()
    declared_write_uow = {
        fn for fns in write_uow_users.values() for fn in fns
    }

    for fn, (use_case_module, reason) in SELF_MANAGED_WRITE_ENDPOINTS.items():
        assert reason.strip(), f"自管写事务清单里的 {fn} 必须写清理由"

        assert fn in route_endpoints, (
            f"自管写事务清单里的 {fn} 不是扫得到的路由端点"
            f"（扫描范围：src/copixiv/{{tasks,features}} 里被 @router.* 装饰的"
            f"函数）——端点改名或删掉了？清单跟着改，别让它腐烂"
        )

        assert fn not in declared_write_uow, (
            f"{fn} 同时声明了 Depends(get_write_uow)：写端点的两种形态互斥。"
            f"形态②（自管写事务）的端点正是要把抓取 / 网络留在事务之外，"
            f"补上请求级写事务就把它关进去了"
        )

        module_path = SRC_ROOT / use_case_module
        assert module_path.is_file(), (
            f"{fn} 的用例模块不存在：{module_path}——用例搬家了？"
        )
        assert _calls_run_write_transaction(module_path), (
            f"{fn} 声称自己开写事务，但 {use_case_module} 里没有 "
            f"run_write_transaction 调用：要么在用例里补上，要么它根本不该"
            f"待在这个清单里"
        )
