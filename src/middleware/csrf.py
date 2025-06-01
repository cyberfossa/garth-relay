"""CSRF protection middleware using double-submit cookie pattern."""

import secrets

from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class CSRFMiddleware:
    """CSRF protection middleware using double-submit cookie pattern.

    Validates CSRF tokens in POST, PUT, DELETE requests against cookies.
    Exempts health checks and webhook endpoints.
    """

    EXEMPT_PATHS = {
        "/health",
        "/polling/poll",
        "/auth/callback",
        "/auth/connect-google-health/callback",
        "/webhooks/google-health",
    }

    def __init__(self, app: ASGIApp) -> None:
        """Initialize middleware.

        Args:
            app: ASGI application instance.
        """
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Process HTTP request with CSRF validation.

        Args:
            scope: ASGI scope dict.
            receive: ASGI receive callable.
            send: ASGI send callable.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        csrf_token = request.cookies.get("csrf_token") or secrets.token_urlsafe(32)
        scope.setdefault("state", {})["csrf_token"] = csrf_token

        if request.method in {"POST", "PUT", "DELETE"} and request.url.path not in self.EXEMPT_PATHS:
            body = await request.body()
            form = await request.form()
            form_token = form.get("csrf_token")
            await form.close()
            if not form_token or form_token != csrf_token:
                response = PlainTextResponse("CSRF token missing or invalid", status_code=403)
                await response(scope, receive, send)
                return

            async def receive_with_body() -> Message:
                return {"type": "http.request", "body": body, "more_body": False}

            scope["_csrf_receive"] = receive_with_body
            receive = receive_with_body

        set_cookie_value = f"csrf_token={csrf_token}; Path=/; SameSite=Lax"

        async def send_with_cookie(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers: list[tuple[bytes, bytes]] = list(message.get("headers", []))
                headers.append((b"set-cookie", set_cookie_value.encode()))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_cookie)
