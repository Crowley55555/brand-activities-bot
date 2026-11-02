from __future__ import annotations

from httpx import ASGITransport, AsyncClient


async def test_app_imports_cleanly() -> None:
    import app.main  # noqa: F401


async def test_health_endpoint_returns_ok() -> None:
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
