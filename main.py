"""Thin shim for ``python main.py`` / ``uvicorn main:app``.

The real entry point lives in ``copixiv.app.main`` (also exposed as the
``copixiv`` console script in ``pyproject.toml``).  This shim is the only
place where the app is built at import time — importing
``copixiv.app.main`` itself has no side effects (see
docs/MODULARITY.md §M10).
"""

from copixiv.app.main import create_app, main

app = create_app()  # noqa: F401 — re-exported for uvicorn main:app

if __name__ == "__main__":
    main()
