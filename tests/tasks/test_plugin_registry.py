"""Task-plugin discovery tests (docs/MODULARITY.md §M8 发现链路).

Pin the two discovery paths and the declarative manifest contract:

1. built-in fallback — the kernel discovers the in-tree task modules;
2. entry-point discovery — a third-party package (simulated by a fake
   entry point) installs its task with zero core edits;
3. manifests — ``describe_tasks`` derives argument metadata purely from
   the Pydantic args model.
"""

import importlib.metadata

import pytest

from copixiv.tasks.registry import (
    DEFAULT_TASK_MODULES,
    ENTRY_POINT_GROUP,
    describe_tasks,
    discover_tasks,
    get_spec,
    unregister,
)

DEMO_MODULE = "tests.plugins.copixiv_task_demo"


class _FakeEntryPoint:
    """Minimal stand-in for importlib.metadata.EntryPoint."""

    def __init__(self, name: str, value: str):
        self.name = name
        self.value = value


@pytest.fixture(autouse=True)
def _clean_demo_task():
    """Remove the demo task after each test so it cannot leak into other
    modules (the registry is process-global)."""
    yield
    unregister("demo_task")


def test_builtin_fallback_discovers_in_tree_tasks():
    discover_tasks()

    for name in ("novel_fetch", "batch_operation", "rebuild_fts"):
        assert get_spec(name) is not None, f"{name} 未被发现"

    # The built-in list is the single source of truth for in-tree modules.
    assert set(DEFAULT_TASK_MODULES) == {
        "copixiv.tasks.novel_tasks",
        "copixiv.tasks.batch_tasks",
        "copixiv.tasks.maintenance",
    }


def test_entry_point_discovers_third_party_task(monkeypatch):
    """A third-party package declares an entry point → task registers
    itself; no core edit needed."""
    monkeypatch.setattr(
        importlib.metadata,
        "entry_points",
        lambda **kwargs: [_FakeEntryPoint("demo", DEMO_MODULE)]
        if kwargs.get("group") == ENTRY_POINT_GROUP
        else [],
    )

    discover_tasks()

    spec = get_spec("demo_task")
    assert spec is not None
    assert spec.args_model is not None


def test_describe_tasks_derives_arguments_from_args_model():
    discover_tasks()

    spec = get_spec("novel_search")
    assert spec is not None and spec.args_model is not None

    methods = {m["name"]: m for m in describe_tasks()}
    novel_search = methods["novel_search"]

    by_name = {a["name"]: a for a in novel_search["arguments"]}
    assert by_name["keyword"] == {
        "name": "keyword", "type": "str", "default": "R-18", "required": False,
    }
    assert by_name["minlike"] == {
        "name": "minlike", "type": "int", "default": 500, "required": False,
    }

    # A parameter-less task describes zero arguments.
    assert methods["rebuild_fts"]["arguments"] == []


def test_args_model_validates_params():
    from copixiv.tasks.novel_tasks import NovelFetchArgs

    args = NovelFetchArgs.model_validate({"id": "123"})  # coerces str → int
    assert args.id == 123
    assert args.redownload is True

    with pytest.raises(Exception):
        NovelFetchArgs.model_validate({"id": "not-a-number"})
