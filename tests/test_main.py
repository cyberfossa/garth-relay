import pytest
from httpx import ASGITransport, AsyncClient

from src.main import app


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


class TestHealthEndpoint:
    async def test_health_returns_ok(self, client):
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    async def test_health_post_not_allowed(self, client):
        response = await client.post("/health")
        assert response.status_code == 405

    async def test_nonexistent_route_returns_404(self, client):
        response = await client.get("/nonexistent")
        assert response.status_code == 404
