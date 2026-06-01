"""JWT session management for app-level authentication."""

from datetime import UTC, datetime, timedelta

import structlog
from fastapi import HTTPException, Request, Response
from jose import JWTError, jwt

logger = structlog.get_logger()


def create_jwt(
    user_id: str,
    email: str,
    name: str,
    secret: str,
    algorithm: str = "HS256",
    expiry_hours: int = 24,
) -> str:
    payload = {
        "sub": user_id,
        "email": email,
        "name": name,
        "exp": datetime.now(UTC) + timedelta(hours=expiry_hours),
    }
    return jwt.encode(payload, secret, algorithm=algorithm)


# Alias for public API compatibility
create_session_token = create_jwt


def decode_jwt(token: str, secret: str, algorithm: str = "HS256") -> dict[str, str | int]:
    return jwt.decode(token, secret, algorithms=[algorithm])


async def get_current_user(request: Request, jwt_secret: str, jwt_algorithm: str = "HS256") -> str:
    token = request.cookies.get("session")
    if not token:
        raise HTTPException(status_code=302, headers={"Location": "/auth/login"})
    try:
        payload = decode_jwt(token, jwt_secret, jwt_algorithm)
        return str(payload["sub"])
    except JWTError:
        raise HTTPException(status_code=302, headers={"Location": "/auth/login"})


def set_session_cookie(response: Response, token: str, *, debug: bool = False) -> None:
    """Set session cookie. secure=True unless debug mode (local HTTP dev)."""
    response.set_cookie(
        key="session",
        value=token,
        httponly=True,
        secure=not debug,
        samesite="lax",
        max_age=86400,
    )


def clear_session_cookie(response: Response, *, debug: bool = False) -> None:
    """Clear session cookie."""
    response.delete_cookie(
        key="session",
        httponly=True,
        secure=not debug,
        samesite="lax",
    )
