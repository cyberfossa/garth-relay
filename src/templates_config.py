"""Jinja2 templates configuration."""

import importlib.metadata
import os
import subprocess

from jinja2 import StrictUndefined
from starlette.requests import Request
from starlette.templating import Jinja2Templates


def get_app_version() -> str:
    """Get the version of the garth-relay package.

    Returns:
        The version string, or "0.1.0" as a fallback.
    """
    try:
        return importlib.metadata.version("garth-relay")
    except importlib.metadata.PackageNotFoundError:
        return "0.1.0"


def get_git_sha() -> str:
    """Get the short git SHA of the current commit.

    Returns:
        The 7-character git SHA, or an empty string if not available.
    """
    # 1. Try to read from environment variable (injected during deployment)
    env_sha = os.environ.get("APP_VERSION_SHA") or os.environ.get("GIT_SHA")
    if env_sha:
        return env_sha[:7]

    # 2. Try to run git CLI (fallback for local development)
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        if sha:
            return sha
    except Exception:
        pass

    return ""


def create_templates(directory: str = "src/templates") -> Jinja2Templates:
    """Create Jinja2Templates instance with StrictUndefined.

    Args:
        directory: Path to templates directory.

    Returns:
        Configured Jinja2Templates instance.
    """
    templates = Jinja2Templates(directory=directory)
    templates.env.undefined = StrictUndefined

    def csrf_hidden_field(request: Request) -> str:
        token = getattr(request.state, "csrf_token", "") or request.cookies.get("csrf_token", "")
        return f'<input type="hidden" name="csrf_token" value="{token}">'

    templates.env.globals["csrf_hidden_field"] = csrf_hidden_field  # pyright: ignore[reportArgumentType]
    templates.env.globals["app_version"] = get_app_version()
    templates.env.globals["app_git_sha"] = get_git_sha()
    return templates
