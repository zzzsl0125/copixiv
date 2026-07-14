"""copixiv v2 entry point — build container, create FastAPI app, run server."""

import os
import signal
import subprocess

from copixiv.app.logger import setup_logging
from copixiv.app.container import Container

# Configure logging before anything else so all log output is captured.
setup_logging()


def kill_port(port: int) -> None:
    """Kill any process listening on *port*."""
    # Try fuser first
    try:
        subprocess.run(
            ["fuser", "-k", f"{port}/tcp"],
            capture_output=True, timeout=5,
        )
    except Exception:
        pass

    # Fallback: ss + kill
    try:
        result = subprocess.run(
            ["ss", "-tlnp"], capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if f":{port}" not in line:
                continue
            for part in line.split():
                if "pid=" in part:
                    pid = int(part.split("pid=")[1].split(",")[0])
                    os.kill(pid, signal.SIGKILL)
    except Exception:
        pass


# Module-level app for gunicorn/uvicorn import
# Avoid double-creation when uvicorn reload spawns child
container = Container()
container.build()
app = container.create_app()


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "9000"))
    kill_port(port)

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=True,
        log_config=None,
        access_log=False,
    )
