"""Jinja2 templates configuration."""

from jinja2 import StrictUndefined
from starlette.requests import Request
from starlette.templating import Jinja2Templates


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
    return templates
