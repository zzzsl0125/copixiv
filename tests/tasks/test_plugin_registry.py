"""Task discovery + declarative manifest tests (docs/MODULARITY.md §M8).

Pin the built-in discovery path and the declarative manifest contract:

1. built-in discovery — the kernel imports the in-tree task modules;
2. manifests — ``describe_tasks`` derives argument metadata purely from
   the Pydantic args model;
3. runtime validation — params are validated against the args model at
   execution time.

There is deliberately no third-party plugin path (no entry-point group):
docs/MODULARITY.md §6.
"""

import pytest

from copixiv.tasks.kernel import (
    DEFAULT_TASK_MODULES,
    describe_tasks,
    discover_tasks,
    get_spec,
)


def test_builtin_discovery_registers_in_tree_tasks():
    discover_tasks()

    for name in ("novel_fetch", "batch_operation", "rebuild_fts"):
        assert get_spec(name) is not None, f"{name} 未被发现"

    # The built-in list is the single source of truth for in-tree modules.
    assert set(DEFAULT_TASK_MODULES) == {
        "copixiv.tasks.novels",
        "copixiv.tasks.batch",
        "copixiv.tasks.maintenance",
    }


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
    from copixiv.tasks.novels import NovelFetchArgs

    args = NovelFetchArgs.model_validate({"id": "123"})  # coerces str → int
    assert args.id == 123
    assert args.redownload is True

    with pytest.raises(Exception):
        NovelFetchArgs.model_validate({"id": "not-a-number"})
