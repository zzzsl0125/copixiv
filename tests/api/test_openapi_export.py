"""OpenAPI export contract — the schema the frontend codegen depends on.

``build_openapi()`` must produce the same paths/models as the real app
without constructing the container (no config.yaml, no DB, no startup);
the export is the single source of truth for ``frontend/src/types``.
"""

from copixiv.export_openapi import build_openapi


def test_build_openapi_includes_main_paths():
    schema = build_openapi()
    paths = schema["paths"]

    for path in (
        "/api/novels/",
        "/api/novels/count",
        "/api/tasks/methods",
        "/api/tasks/history",
        "/api/tasks/scheduled",
        "/api/library/uploads",
        "/api/tokens/",
        "/api/tag-aliases/",
        "/api/system/config",
        "/api/failed-novels",
    ):
        assert path in paths, f"missing path in exported schema: {path}"


def test_build_openapi_includes_key_schemas():
    schema = build_openapi()
    schemas = schema["components"]["schemas"]

    for name in (
        "NovelBase",
        "NovelListResponse",
        "NovelCountResponse",
        "TaskMethod",
        "ScheduledTaskResponse",
        "TaskHistoryResponse",
        "UploadResponse",
        "TokenResponse",
        "TagAliasResponse",
    ):
        assert name in schemas, f"missing schema in exported schema: {name}"


def test_build_openapi_is_stable():
    """Two builds serialize identically — codegen drift must be meaningful."""
    assert build_openapi() == build_openapi()
