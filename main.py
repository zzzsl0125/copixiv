"""Thin shim for ``python main.py`` — the app is built exactly once by
the uvicorn factory (see :func:`copixiv.app.main`).  ``uvicorn main:app``
is no longer supported (it would build at import time); use ``python main.py``
or the ``copixiv`` console script."""

from copixiv.app import create_app, main  # noqa: F401（保留 re-export 兼容）

if __name__ == "__main__":
    main()
