"""M6/M10 复现：API 写路径纳入全局写锁 + application 层架构纯度。"""

import ast
import asyncio
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from copixiv.infrastructure.database.models import Base
from copixiv.infrastructure.database.write_lock import _db_write_lock
from copixiv.web_api import deps


# M6 -------------------------------------------------------------


@pytest.fixture()
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    return factory()


def test_get_write_uow_holds_global_lock(session):
    """期望：get_write_uow yield 期间持有全局写锁。"""
    results = {}

    async def scenario():
        async for _uow in deps.get_write_uow(db=session):
            results["locked"] = _db_write_lock.locked()

    asyncio.run(scenario())
    assert results["locked"] is True


def test_write_endpoints_declare_write_uow():
    """期望：所有写端点声明 Depends(get_write_uow) 而非 get_uow。

    读端点保持 get_uow。用 AST 全量扫描 endpoints 目录（而非字符串匹配），
    新增端点文件也在检查范围内。
    """
    src_root = Path(__file__).resolve().parents[2] / "src" / "copixiv" / "web_api"
    endpoints_dir = src_root / "endpoints"

    # Map: endpoint file (without .py) → function name → does its signature
    # contain Depends(get_write_uow)?
    write_uow_users: dict[str, set[str]] = {}
    for py_file in sorted(endpoints_dir.glob("*.py")):
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


# M10 ------------------------------------------------------------


def test_application_layer_does_not_import_infrastructure():
    """期望：application 层不 import 具体基础设施（port 依赖倒置）。

    无例外：record.py 曾经延迟 import SqlUnitOfWork（BackgroundTasks
    回调无法触达组合根的妥协），现已改为注入 UoW 工厂（组合边缘
    由 web_api 端点构造具体类，见 docs/MODULARITY.md §3.1/§3.3）。
    """
    app_root = Path(__file__).resolve().parents[2] / "src" / "copixiv" / "application"
    violations = []
    for py_file in app_root.rglob("*.py"):
        tree = ast.parse(py_file.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("copixiv.infrastructure"):
                    violations.append(
                        f"{py_file.name}:{node.lineno} imports {node.module}"
                    )
    assert violations == [], (
        "application 层泄漏基础设施依赖:\n" + "\n".join(violations)
    )
