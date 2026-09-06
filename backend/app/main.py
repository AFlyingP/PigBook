import uuid
from typing import Awaitable, Callable

from fastapi import FastAPI, Request, Response

from app.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    is_docs_enabled = settings.APP_ENV in ("local", "test")

    app = FastAPI(
        title="CommonsBook",
        version=settings.RELEASE_SHA,
        openapi_url="/openapi.json" if is_docs_enabled else None,
        docs_url="/docs" if is_docs_enabled else None,
        redoc_url="/redoc" if is_docs_enabled else None,
    )

    @app.middleware("http")
    async def request_id_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        inbound_id = request.headers.get("X-Request-ID")
        request_id = inbound_id if inbound_id else str(uuid.uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.get("/healthz", response_model=None)
    async def healthz() -> dict[str, str]:
        current_settings = get_settings()
        return {"status": "ok", "version": current_settings.RELEASE_SHA}

    # Static frontend file mount is registered here during production packaging deployment.

    return app


app: FastAPI = create_app()
