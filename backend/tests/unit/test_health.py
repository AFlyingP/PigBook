import pytest
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.main import create_app


@pytest.mark.asyncio
async def test_health_endpoint() -> None:
    app = create_app()
    settings = get_settings()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/healthz")
        assert response.status_code == 200
        data = response.json()
        assert data == {"status": "ok", "version": settings.RELEASE_SHA}
        assert set(data.keys()) == {"status", "version"}


@pytest.mark.asyncio
async def test_health_request_id_header_generated() -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/healthz")
        assert response.status_code == 200
        request_id = response.headers.get("X-Request-ID")
        assert request_id is not None
        assert len(request_id) > 0


@pytest.mark.asyncio
async def test_health_request_id_header_echoed() -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    custom_id = "custom-inbound-request-id-12345"
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/healthz", headers={"X-Request-ID": custom_id})
        assert response.status_code == 200
        assert response.headers.get("X-Request-ID") == custom_id


@pytest.mark.asyncio
async def test_health_different_generated_request_ids() -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res1 = await client.get("/healthz")
        res2 = await client.get("/healthz")
        id1 = res1.headers.get("X-Request-ID")
        id2 = res2.headers.get("X-Request-ID")
        assert id1 is not None and id2 is not None
        assert id1 != id2


@pytest.mark.asyncio
async def test_unknown_api_path_returns_json_404() -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/unknown")
        assert response.status_code == 404
        content_type = response.headers.get("content-type", "")
        assert "application/json" in content_type
        data = response.json()
        assert "detail" in data
