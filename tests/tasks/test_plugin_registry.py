"""Task discovery + declarative manifest tests (docs/MODULARITY.md §M8).

Pin the built-in discovery path and the declarative manifest contract:

1. built-in discovery — the kernel imports the in-tree task modules;
2. manifests — ``describe_tasks`` exposes the pydantic args model's JSON
   Schema (``model_json_schema()``);
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
    arguments = methods["novel_search"]["arguments"]

    # The descriptor carries Pydantic's full JSON Schema, untouched.
    assert isinstance(arguments, dict)
    assert arguments == spec.args_model.model_json_schema()
    assert arguments["title"] == "NovelSearchArgs"
    assert arguments["type"] == "object"

    properties = arguments["properties"]
    assert properties["keyword"]["type"] == "string"
    assert properties["keyword"]["default"] == "R-18"
    assert properties["minlike"]["type"] == "integer"
    assert properties["minlike"]["default"] == 500
    assert properties["force"]["type"] == "boolean"
    assert properties["force"]["default"] is False

    # Required fields come from the schema, not from "no default".
    assert methods["novel_fetch"]["arguments"]["required"] == ["id"]

    # Nullable / list fields keep Pydantic's anyOf / array shape.
    batch_export = methods["batch_export"]["arguments"]
    assert batch_export["properties"]["novel_ids"]["type"] == "array"
    assert batch_export["properties"]["zip_name"]["anyOf"] == [
        {"type": "string"}, {"type": "null"},
    ]

    # A parameter-less task describes no arguments at all.
    assert methods["rebuild_fts"]["arguments"] is None


def test_args_model_validates_params():
    from copixiv.tasks.novels import NovelFetchArgs

    args = NovelFetchArgs.model_validate({"id": "123"})  # coerces str → int
    assert args.id == 123
    assert args.redownload is True

    with pytest.raises(Exception):
        NovelFetchArgs.model_validate({"id": "not-a-number"})
