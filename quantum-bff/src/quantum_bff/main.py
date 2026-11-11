"""Compose Star BFF and competition routes without modifying the upstream repository."""

from contextlib import asynccontextmanager

from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from star_bff.main import create_app as create_star_app

from .api import router
from .client import OperatorClient
from .config import Settings


def create_app(settings=None, quantum_settings=None, *, transport=None, world_channels=None, operator_transport=None):
    quantum_settings = quantum_settings or Settings()
    app = create_star_app(settings, transport=transport, world_channels=world_channels)
    app.title = "Quantum Competition Console"
    operator = OperatorClient(quantum_settings, transport=operator_transport)
    app.state.quantum = operator
    app.state.quantum_settings = quantum_settings
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        try:
            async with original_lifespan(application):
                yield
        finally:
            await operator.close()

    app.router.lifespan_context = lifespan
    app.include_router(router)

    @app.middleware("http")
    async def response_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "private, no-store"
        return response

    if (quantum_settings.frontend_dir / "index.html").is_file():
        @app.get("/", include_in_schema=False)
        async def index():
            return FileResponse(quantum_settings.frontend_dir / "index.html", headers={"Cache-Control": "no-cache"})

        app.mount("/console", StaticFiles(directory=quantum_settings.frontend_dir), name="console")
    return app
