"""Export the FastAPI OpenAPI schema (no DB / no container build)."""

from __future__ import annotations

import json

from fastapi import FastAPI

from copixiv.app import _mount_routers


def build_openapi() -> dict:
    """Build the API schema from the routers alone.

    Deliberately bypasses :func:`copixiv.app.create_app`: the export must not
    load ``config.yaml``, connect to PostgreSQL, run migrations, or trigger
    app startup.  Only the route declarations (and their Pydantic models)
    matter for the generated client types.
    """
    app = FastAPI(title="Novel Database API")
    _mount_routers(app)
    return app.openapi()


def main() -> None:
    schema = build_openapi()
    print(json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
