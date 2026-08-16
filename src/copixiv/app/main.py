"""copixiv entry point — application factory + uvicorn launcher.

**Importing this module has no side effects** (see docs/MODULARITY.md
§M10): the container is built only when :func:`create_app` is called.
Entry paths:

- ``copixiv`` console script → :func:`main` (runs uvicorn with the factory)
- ``python -m copixiv.app.main`` → :func:`main`
- repo-root ``main.py`` shim → builds ``app`` for ``python main.py`` /
  ``uvicorn main:app``
- tests / embedding → ``from copixiv.app.main import create_app``
"""

import os
import subprocess
import sys

from copixiv.log import logger, setup_logging, InterceptHandler

# uvicorn re-applies its own logging config on startup (dictConfig), which
# would wipe the InterceptHandler that setup_logging() installed on the
# "uvicorn" loggers — that's why "Uvicorn running on ..." never reached
# backend.log before.  Pass an explicit config that routes uvicorn's
# loggers back through loguru instead of stderr.
UVICORN_LOG_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "loguru": {"()": InterceptHandler},
    },
    "loggers": {
        "uvicorn": {"handlers": ["loguru"], "level": "INFO", "propagate": False},
        "uvicorn.error": {"handlers": ["loguru"], "level": "INFO", "propagate": False},
        "uvicorn.access": {"handlers": ["loguru"], "level": "INFO", "propagate": False},
    },
}


def create_app(config_path: str | None = None):
    """Build the container and return a fully configured FastAPI app.

    ``config_path`` selects an alternate config.yaml (defaults to the
    shared application config).  Construction — logging setup, DB
    migrations, account loading — happens here, never at import time.
    """
    from copixiv.app.container import Container

    # Configure logging before anything else so all log output is captured.
    setup_logging()

    container = Container(config_path)
    container.build()
    return container.create_app()


def ensure_port_free(port: int) -> None:
    """Check that *port* is not in use; exit with a clear error if it is.

    Replaces the old ``kill_port`` behaviour (which force-killed whatever
    process was listening on the port) — killing unrelated processes is
    dangerous, so we now fail fast with actionable information instead.

    The message goes through loguru (not stderr) so it lands in
    ``log/backend.log`` as well as the journal — stderr-only messages
    are invisible in the file logs and make restart loops hard to read.
    """
    try:
        result = subprocess.run(
            ["ss", "-tlnp"], capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if f":{port}" not in line:
                continue
            logger.error(
                f"Port {port} is already in use:\n  {line.strip()}\n"
                "Stop the conflicting process first, or choose another port "
                "via the PORT environment variable."
            )
            sys.exit(1)
    except Exception:
        # ss unavailable — let uvicorn report the bind failure instead.
        pass


def main() -> None:
    """Run the uvicorn server (used by the ``copixiv`` console script).

    ``reload`` is opt-in via ``COPIXIV_RELOAD=1`` — it is a development
    convenience only.  Under systemd the reloader is actively harmful:
    with watchfiles absent uvicorn falls back to StatReload, which polls
    every ``*.py`` under the working directory (including the whole
    ``.venv`` tree) and turns any transient file touch into a restart
    storm that fights the unit's own ``Restart=`` policy.  Deployments
    must run without it; use ``systemctl restart`` instead.
    """
    import uvicorn

    port = int(os.environ.get("PORT", "9000"))
    ensure_port_free(port)

    try:
        uvicorn.run(
            "copixiv.app.main:create_app",
            factory=True,
            host="0.0.0.0",
            port=port,
            reload=os.environ.get("COPIXIV_RELOAD") == "1",
            log_config=UVICORN_LOG_CONFIG,
            access_log=False,
        )
    except SystemExit:
        raise
    except Exception:
        # Surface unexpected uvicorn exits through loguru so the reason
        # reaches log/backend.log as well as the journal.
        logger.exception("uvicorn exited unexpectedly")
        raise


if __name__ == "__main__":
    main()
