"""copixiv entry point — build container, create FastAPI app, run server.

Used by the ``copixiv`` console script (``pyproject.toml``), by
``python -m copixiv.app.main``, and via the repo-root ``main.py`` shim
(for ``python main.py`` / ``uvicorn main:app``).
"""

import os
import subprocess
import sys

from copixiv.app.logger import logger, setup_logging
from copixiv.app.container import Container

# Configure logging before anything else so all log output is captured.
setup_logging()

# Module-level app for uvicorn/gunicorn import.  Building the container at
# import time (instead of inside a lifespan) keeps ``uvicorn
# copixiv.app.main:app`` working without double-creation when the reloader
# spawns a child process.
container = Container()
container.build()
app = container.create_app()


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
            "copixiv.app.main:app",
            host="0.0.0.0",
            port=port,
            reload=os.environ.get("COPIXIV_RELOAD") == "1",
            log_config=None,
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
