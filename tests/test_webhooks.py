"""Tests for webhook routes."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.routes.webhooks import create_webhooks_router


@pytest.fixture
def webhooks_app():
    app = FastAPI()
    router = create_webhooks_router()
    app.include_router(router)
    return app


@pytest.fixture
def client(webhooks_app):
    return TestClient(webhooks_app)


class TestGoogleHealthWebhook:
    def test_returns_200_not_implemented(self, client):
        response = client.post("/webhooks/google-health", json={"test": "data"})
        assert response.status_code == 200
        assert response.json() == {"status": "not_implemented"}

    def test_handles_empty_body(self, client):
        response = client.post("/webhooks/google-health", content=b"not json")
        assert response.status_code == 200
        assert response.json()["status"] == "not_implemented"

    def test_handles_valid_json(self, client):
        payload = {"dataTypeName": "com.google.weight", "userId": "user123"}
        response = client.post("/webhooks/google-health", json=payload)
        assert response.status_code == 200
