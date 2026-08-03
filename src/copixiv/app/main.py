"""copixiv entry point — build container, create FastAPI app, run server.

Used by the ``copixiv`` console script (``pyproject.toml``), by
``python -m copixiv.app.main``, and via the repo-root ``main.py`` shim
(for ``python main.py`` / ``uvicorn main:app``).
"""

import os
import subprocess
import sys

from copixiv.app.logger import setup_logging
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
    """
    try:
        result = subprocess.run(
            ["ss", "-tlnp"], capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if f":{port}" not in line:
                continue
            print(
                f"Port {port} is already in use:\n  {line.strip()}\n"
                "Stop the conflicting process first, or choose another port "
                "via the PORT environment variable.",
                file=sys.stderr,
            )
            sys.exit(1)
    except Exception:
        # ss unavailable — let uvicorn report the bind failure instead.
        pass


def main() -> None:
    """Run the uvicorn server (used by the ``copixiv`` console script)."""
    import uvicorn

    port = int(os.environ.get("PORT", "9000"))
    ensure_port_free(port)

    uvicorn.run(
        "copixiv.app.main:app",
        host="0.0.0.0",
        port=port,
        reload=True,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
