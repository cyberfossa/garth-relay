"""OIDC token validation for Google ID tokens."""

from fastapi import HTTPException, Request
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token


async def verify_oidc_token(request: Request, audience: str) -> str:
    """Verify an OIDC bearer token from the Authorization header.

    Args:
        request: FastAPI request
        audience: Expected audience (client ID)

    Returns:
        Email from the verified token
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing OIDC token")

    token = auth_header[len("Bearer "):]

    try:
        transport = google_requests.Request()
        decoded = id_token.verify_oauth2_token(token, transport, audience=audience)

        if decoded.get("iss") not in ("https://accounts.google.com", "accounts.google.com"):
            raise HTTPException(status_code=401, detail="Invalid token issuer")

        return decoded.get("email", "")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid OIDC token") from exc
