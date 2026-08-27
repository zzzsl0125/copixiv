"""AccessLogMiddleware tests — the middleware must record the REAL status
code (including the one produced by exception handlers), keeping
log/access.log in sync with actual responses.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from copixiv.log import capture_logs
from copixiv.app import AccessLogMiddleware


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AccessLogMiddleware)

    @app.get("/ok")
    async def ok():
        return {"ok": True}

    @app.get("/boom")
    async def boom():
        raise RuntimeError("boom")

    @app.exception_handler(RuntimeError)
    async def _handler(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content={"detail": "boom"})

    return app


def _access_lines(log_text: str) -> list[str]:
    # The middleware logs "METHOD path → status elapsed" lines.
    return [line for line in log_text.splitlines() if " → " in line]


def test_logs_real_status_codes():
    app = _build_app()

    import asyncio

    async def scenario():
        # capture_logs' reader must be consumed inside the context.
        with capture_logs() as get_logs:
            with TestClient(app, raise_server_exceptions=False) as client:
                assert client.get("/ok").status_code == 200
                assert client.get("/boom").status_code == 503
            return get_logs()

    lines = _access_lines(asyncio.run(scenario()))
    assert any("GET /ok → 200" in line for line in lines), lines
    # 503 comes from the exception handler — a BaseHTTPMiddleware-style
    # implementation would have logged 500 or nothing.
    assert any("GET /boom → 503" in line for line in lines), lines


def test_non_http_scope_passes_through():
    """WebSocket/lifespan scopes must not produce access logs."""
    import asyncio

    app = _build_app()
    received = {}

    async def fake_app(scope, receive, send):
        received["scope_type"] = scope["type"]

    middleware = AccessLogMiddleware(fake_app)

    async def scenario():
        await middleware(
            {"type": "lifespan"}, None, None,
        )

    asyncio.run(scenario())
    assert received == {"scope_type": "lifespan"}
