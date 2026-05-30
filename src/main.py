"""FastAPI application for garth-relay."""

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from src.auth.google_oauth2 import GoogleOAuth2Config, GoogleOAuth2Service
from src.config import get_config
from src.services.google_health_client import GoogleHealthAPIClient
from src.crypto import TokenEncryptor
from src.db import FirestoreClient
from src.logging_setup import setup_logging
from src.middleware import CSRFMiddleware, SecurityHeadersMiddleware
from src.routes.auth import create_auth_router
from src.routes.pages import create_pages_router
from src.templates_config import create_templates

setup_logging()
logger = structlog.get_logger()


def _create_app() -> FastAPI:
    config = get_config()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        logger.info("Application starting up")
        if config.gcp_project_id:
            _app.state.db = FirestoreClient(project_id=config.gcp_project_id)
            logger.info("Firestore client initialized", project_id=config.gcp_project_id)
        else:
            _app.state.db = None
            logger.warning("No GCP project ID configured, Firestore disabled")

        _app.state.token_encryptor = TokenEncryptor(master_key=config.encryption_key)
        logger.info("TokenEncryptor initialized")

        _app.state.google_health_client = GoogleHealthAPIClient()
        logger.info("GoogleHealthAPIClient initialized")

        yield
        logger.info("Application shutting down")

    application = FastAPI(
        title="garth-relay",
        description="Garth Relay service",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Middleware (order: security first, then CSRF)
    application.add_middleware(SecurityHeadersMiddleware)
    application.add_middleware(CSRFMiddleware)

    @application.get("/health")
    async def health_check():
        return {"status": "ok"}

    # Pages router
    templates = create_templates()
    db_client = None
    if config.gcp_project_id:
        db_client = FirestoreClient(project_id=config.gcp_project_id)

    pages_router = create_pages_router(templates, db_client, config)
    application.include_router(pages_router)

    google_config = GoogleOAuth2Config(
        client_id=config.google_client_id,
        client_secret=config.google_client_secret,
        redirect_uri=config.google_oauth_redirect_uri,
    )
    oauth_service = GoogleOAuth2Service(google_config)
    auth_router = create_auth_router(config=config, oauth_service=oauth_service)
    application.include_router(auth_router)

    return application


app = _create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
