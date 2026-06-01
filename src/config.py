"""Application settings using environ-config."""

import environ


@environ.config(prefix="APP")
class AppConfig:
    """Application configuration from environment variables."""

    debug: bool = environ.bool_var(default=False)
    log_level: str = environ.var(default="INFO")


def get_config() -> AppConfig:
    """Get application configuration from environment variables.

    Returns:
        AppConfig instance with all settings loaded from APP_* env vars.
    """
    return environ.to_config(AppConfig)
