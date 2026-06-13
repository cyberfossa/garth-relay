import importlib.metadata
import os
from unittest.mock import patch

from src.templates_config import create_templates, get_app_version, get_git_sha


def test_get_app_version_success():
    with patch("importlib.metadata.version", return_value="1.2.3"):
        assert get_app_version() == "1.2.3"


def test_get_app_version_fallback():
    with patch("importlib.metadata.version", side_effect=importlib.metadata.PackageNotFoundError):
        assert get_app_version() == "0.1.0"


def test_get_git_sha_from_env_app_version_sha():
    with patch.dict(os.environ, {"APP_VERSION_SHA": "abcdef123456"}):
        assert get_git_sha() == "abcdef1"


def test_get_git_sha_from_env_git_sha():
    with patch.dict(os.environ, {"GIT_SHA": "1234567890"}):
        assert get_git_sha() == "1234567"


def test_get_git_sha_from_git_cmd():
    with patch.dict(os.environ, {}, clear=True):
        with patch("subprocess.check_output", return_value="f1e2d3c\n"):
            assert get_git_sha() == "f1e2d3c"


def test_get_git_sha_fallback():
    with patch.dict(os.environ, {}, clear=True):
        with patch("subprocess.check_output", side_effect=Exception("git not installed")):
            assert get_git_sha() == ""


def test_create_templates_injects_globals():
    templates = create_templates()
    assert "app_version" in templates.env.globals
    assert "app_git_sha" in templates.env.globals
