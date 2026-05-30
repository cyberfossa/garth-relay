"""Application settings using environ-config."""

import base64
import os

import environ
import structlog

logger = structlog.get_logger()

_DEV_ENCRYPTION_KEY = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
_DEV_JWT_SECRET = "garth-relay-dev-jwt-secret-CHANGE-ME"
_DEV_CSRF_SECRET = "garth-relay-dev-csrf-secret-CHANGE-ME"


@environ.config(prefix="APP")
class AppConfig:
    """Application configuration from environment variables."""

    debug: bool = environ.bool_var(default=False)
    log_level: str = environ.var(default="INFO")
    gcp_project_id: str = environ.var(default="")
    encryption_key: str = environ.var(default="")
    jwt_secret_key: str = environ.var(default="")
    jwt_algorithm: str = environ.var(default="HS256")
    csrf_secret: str = environ.var(default="")
    google_client_id: str = environ.var(default="")
    google_client_secret: str = environ.var(default="")
    google_oauth_redirect_uri: str = environ.var(default="http://localhost:8080/auth/callback")


def get_config() -> AppConfig:
    """Get application configuration from environment variables.

    Returns:
        AppConfig instance with all settings loaded from APP_* env vars.
    """
    config = environ.to_config(AppConfig)

    if not config.encryption_key:
        logger.warning("APP_ENCRYPTION_KEY not set, using auto-generated dev key (NOT for production)")
        object.__setattr__(config, "encryption_key", _DEV_ENCRYPTION_KEY)

    if not config.jwt_secret_key:
        logger.warning("APP_JWT_SECRET_KEY not set, using dev default (NOT for production)")
        object.__setattr__(config, "jwt_secret_key", _DEV_JWT_SECRET)

    if not config.csrf_secret:
        logger.warning("APP_CSRF_SECRET not set, using dev default (NOT for production)")
        object.__setattr__(config, "csrf_secret", _DEV_CSRF_SECRET)

    return config
