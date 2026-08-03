"""Thin shim for ``python main.py`` / ``uvicorn main:app``.

The real entry point lives in ``copixiv.app.main`` (also exposed as the
``copixiv`` console script in ``pyproject.toml``).
"""

from copixiv.app.main import app  # noqa: F401  — re-exported for uvicorn main:app
from copixiv.app.main import main

if __name__ == "__main__":
    main()
