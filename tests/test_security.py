import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from src.middleware.csrf import CSRFMiddleware
from src.middleware.security import SecurityHeadersMiddleware


def _create_test_app():
    test_app = FastAPI()
    test_app.add_middleware(SecurityHeadersMiddleware)
    test_app.add_middleware(CSRFMiddleware)

    @test_app.get("/health")
    async def health():
        return {"status": "ok"}

    @test_app.post("/submit")
    async def submit():
        return PlainTextResponse("ok")

    @test_app.post("/polling/poll")
    async def poll():
        return PlainTextResponse("polled")

    @test_app.post("/auth/callback")
    async def auth_callback():
        return PlainTextResponse("callback")

    @test_app.post("/webhooks/google-health")
    async def webhook():
        return PlainTextResponse("webhook")

    return test_app


@pytest.fixture
def client():
    return TestClient(_create_test_app())


class TestCSRFProtection:
    def test_post_with_valid_csrf_succeeds(self, client):
        get_response = client.get("/health")
        csrf_token = get_response.cookies.get("csrf_token")
        assert csrf_token is not None

        response = client.post(
            "/submit",
            data={"csrf_token": csrf_token},
            cookies={"csrf_token": csrf_token},
        )
        assert response.status_code == 200
        assert response.text == "ok"

    def test_post_without_csrf_returns_403(self, client):
        get_response = client.get("/health")
        csrf_token = get_response.cookies.get("csrf_token")

        response = client.post("/submit", cookies={"csrf_token": csrf_token})
        assert response.status_code == 403
        assert "CSRF" in response.text

    def test_post_with_wrong_csrf_returns_403(self, client):
        get_response = client.get("/health")
        csrf_token = get_response.cookies.get("csrf_token")

        response = client.post(
            "/submit",
            data={"csrf_token": "wrong-token-value"},
            cookies={"csrf_token": csrf_token},
        )
        assert response.status_code == 403

    def test_get_without_csrf_succeeds(self, client):
        response = client.get("/health")
        assert response.status_code == 200

    def test_polling_poll_exempt(self, client):
        response = client.post("/polling/poll")
        assert response.status_code == 200

    def test_auth_callback_exempt(self, client):
        response = client.post("/auth/callback")
        assert response.status_code == 200

    def test_webhooks_exempt(self, client):
        response = client.post("/webhooks/google-health")
        assert response.status_code == 200

    def test_csrf_cookie_set_on_response(self, client):
        response = client.get("/health")
        csrf_token = response.cookies.get("csrf_token")
        assert csrf_token is not None
        assert len(csrf_token) > 20

        set_cookie_headers = [
            v for k, v in response.headers.multi_items() if k.lower() == "set-cookie" and "csrf_token" in v
        ]
        assert len(set_cookie_headers) >= 1
        assert "samesite=strict" in set_cookie_headers[0].lower()


class TestSecurityHeaders:
    def test_x_content_type_options(self, client):
        response = client.get("/health")
        assert response.headers.get("X-Content-Type-Options") == "nosniff"

    def test_x_frame_options(self, client):
        response = client.get("/health")
        assert response.headers.get("X-Frame-Options") == "DENY"

    def test_strict_transport_security(self, client):
        response = client.get("/health")
        assert response.headers.get("Strict-Transport-Security") == "max-age=31536000"

    def test_content_security_policy(self, client):
        response = client.get("/health")
        assert (
            response.headers.get("Content-Security-Policy")
            == "default-src 'self'; script-src 'self' 'unsafe-inline' https://unpkg.com; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data:"
        )

    def test_referrer_policy(self, client):
        response = client.get("/health")
        assert response.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"

    def test_all_security_headers_present(self, client):
        response = client.get("/health")
        expected = {
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Strict-Transport-Security": "max-age=31536000",
            "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline' https://unpkg.com; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data:",
            "Referrer-Policy": "strict-origin-when-cross-origin",
        }
        for header, value in expected.items():
            assert response.headers.get(header) == value, f"{header} mismatch"
