"""Google OAuth2 flow handling."""

import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
import structlog
from jose import jwt

logger = structlog.get_logger()


@dataclass
class GoogleOAuth2Config:
    """Google OAuth2 configuration."""

    client_id: str
    client_secret: str
    redirect_uri: str
    scope: str = "openid email profile https://www.googleapis.com/auth/fitness.body.read"


@dataclass
class GoogleTokenResponse:
    """Token response from Google OAuth2."""

    access_token: str
    expires_in: int
    token_type: str = "Bearer"
    refresh_token: str | None = None
    id_token: str | None = None


class GoogleOAuth2Service:
    """Handle Google OAuth2 authorization flow."""

    def __init__(self, config: GoogleOAuth2Config):
        """Initialize Google OAuth2 service.

        Args:
            config: Google OAuth2 configuration
        """
        self.config = config
        self.authorization_endpoint = "https://accounts.google.com/o/oauth2/v2/auth"
        self.token_endpoint = "https://oauth2.googleapis.com/token"

    def generate_authorization_url(self) -> tuple[str, str]:
        """Generate Google authorization URL with CSRF state.

        Returns:
            Tuple of (auth_url, state_parameter)
        """
        state = secrets.token_urlsafe(32)

        params = {
            "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_uri,
            "response_type": "code",
            "scope": self.config.scope,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }

        auth_url = f"{self.authorization_endpoint}?{urlencode(params)}"
        return auth_url, state

    async def exchange_code_for_token(self, code: str) -> GoogleTokenResponse | None:
        """Exchange authorization code for access token.

        Args:
            code: Authorization code from callback

        Returns:
            GoogleTokenResponse or None if failed
        """
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self.token_endpoint,
                    data={
                        "client_id": self.config.client_id,
                        "client_secret": self.config.client_secret,
                        "code": code,
                        "grant_type": "authorization_code",
                        "redirect_uri": self.config.redirect_uri,
                    },
                )

                if response.status_code != 200:
                    logger.error("Token exchange failed", status=response.status_code, body=response.text)
                    return None

                token_data = response.json()
                return GoogleTokenResponse(
                    access_token=token_data["access_token"],
                    expires_in=token_data.get("expires_in", 3600),
                    token_type=token_data.get("token_type", "Bearer"),
                    refresh_token=token_data.get("refresh_token"),
                    id_token=token_data.get("id_token"),
                )
        except Exception:
            logger.exception("Token exchange error")
            return None

    async def refresh_access_token(self, refresh_token: str) -> GoogleTokenResponse | None:
        """Refresh expired access token.

        Args:
            refresh_token: Refresh token

        Returns:
            New GoogleTokenResponse or None if failed
        """
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self.token_endpoint,
                    data={
                        "client_id": self.config.client_id,
                        "client_secret": self.config.client_secret,
                        "refresh_token": refresh_token,
                        "grant_type": "refresh_token",
                    },
                )

                if response.status_code != 200:
                    logger.error("Token refresh failed", status=response.status_code, body=response.text)
                    return None

                token_data = response.json()
                return GoogleTokenResponse(
                    access_token=token_data["access_token"],
                    expires_in=token_data.get("expires_in", 3600),
                    token_type=token_data.get("token_type", "Bearer"),
                    refresh_token=token_data.get("refresh_token"),
                )
        except Exception:
            logger.exception("Token refresh error")
            return None


def validate_id_token(id_token_str: str, client_id: str) -> dict[str, str] | None:
    """Decode Google id_token and validate audience.

    Signature verification skipped — token received directly from Google's
    token endpoint over HTTPS (transport provides authenticity).

    Args:
        id_token_str: The ID token JWT string
        client_id: Expected audience (our client ID)

    Returns:
        Dict with sub, email, name or None if invalid
    """
    try:
        claims = jwt.decode(
            id_token_str,
            "",
            options={"verify_signature": False, "verify_at_hash": False},
            audience=client_id,
        )
        return {
            "sub": claims["sub"],
            "email": claims.get("email", ""),
            "name": claims.get("name", ""),
        }
    except Exception:
        logger.exception("Failed to decode id_token")
        return None
