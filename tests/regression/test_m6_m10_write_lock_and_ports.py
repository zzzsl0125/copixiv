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

    读端点保持 get_uow。抽查关键写端点的源码（静态契约测试）。
    """
    src_root = Path(__file__).resolve().parents[2] / "src" / "copixiv" / "web_api"
    texts = {
        "novels": (src_root / "endpoints" / "novels.py").read_text(),
        "tasks": (src_root / "endpoints" / "tasks.py").read_text(),
        "tokens": (src_root / "endpoints" / "tokens.py").read_text(),
        "tag_preferences": (src_root / "endpoints" / "tag_preferences.py").read_text(),
        "tag_aliases": (src_root / "endpoints" / "tag_aliases.py").read_text(),
        "search_history": (src_root / "endpoints" / "search_history.py").read_text(),
    }
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
        found = any(f"def {fn}" in t and "get_write_uow" in
                    t.split(f"def {fn}")[1].split("):")[0]
                    for t in texts.values())
        assert found, f"写端点 {fn} 未声明 Depends(get_write_uow)"


# M10 ------------------------------------------------------------


def test_application_layer_does_not_import_infrastructure():
    """期望：application 层不 import 具体基础设施（port 依赖倒置）。

    唯一允许的例外（已在模块注释中说明的妥协）：
    - record.py 在函数内延迟 import SqlUnitOfWork（BackgroundTasks
      回调无法触达组合根）。
    """
    app_root = Path(__file__).resolve().parents[2] / "src" / "copixiv" / "application"
    allowed = {
        "record.py": {"copixiv.infrastructure.database.uow"},
        "download_novel.py": set(),
        "resolve_names.py": {"copixiv.app.logger"},
    }
    violations = []
    for py_file in app_root.rglob("*.py"):
        tree = ast.parse(py_file.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("copixiv.infrastructure"):
                    allowed_mods = allowed.get(py_file.name, set())
                    if node.module not in allowed_mods:
                        violations.append(
                            f"{py_file.name}:{node.lineno} imports {node.module}"
                        )
    assert violations == [], (
        "application 层泄漏基础设施依赖:\n" + "\n".join(violations)
    )
