"""FastAPI application for garth-relay."""

from contextlib import asynccontextmanager
from urllib.parse import urlparse

import structlog
from fastapi import FastAPI

from src.auth.google_oauth2 import GoogleOAuth2Config, GoogleOAuth2Service
from src.config import get_config
from src.crypto import TokenEncryptor
from src.db import FirestoreClient
from src.logging_setup import setup_logging
from src.middleware import CSRFMiddleware, SecurityHeadersMiddleware
from src.routes.auth import create_auth_router
from src.routes.bulk_sync import create_bulk_sync_router
from src.routes.connections import create_connections_router
from src.routes.pages import create_pages_router
from src.routes.polling import create_polling_router
from src.routes.webhooks import create_webhooks_router
from src.services.garmin_client import GarminClient
from src.services.google_health_client import GoogleHealthAPIClient
from src.services.sync_orchestrator import SyncOrchestrator
from src.templates_config import create_templates

setup_logging()
logger = structlog.get_logger()


def _create_app() -> FastAPI:  # noqa: PLR0915
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

        _app.state.garmin_client = GarminClient()
        logger.info("GarminClient initialized")

        if _app.state.db is not None:
            _app.state.sync_orchestrator = SyncOrchestrator(
                google_client=_app.state.google_health_client,
                db_client=_app.state.db,
                encryptor=_app.state.token_encryptor,
            )
            logger.info("SyncOrchestrator initialized")
        else:
            _app.state.sync_orchestrator = None

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
    auth_router = create_auth_router(config=config, oauth_service=oauth_service, db_client=db_client)
    application.include_router(auth_router)

    encryptor = TokenEncryptor(master_key=config.encryption_key)
    parsed_redirect_uri = urlparse(config.google_oauth_redirect_uri)
    connections_base_url = (
        f"{parsed_redirect_uri.scheme}://{parsed_redirect_uri.netloc}" if parsed_redirect_uri.netloc else ""
    )
    google_connections_redirect_uri = (
        f"{connections_base_url}/connections/google/callback"
        if connections_base_url
        else config.google_oauth_redirect_uri
    )
    sync_orchestrator: SyncOrchestrator | None = None

    # Connections router (Google + Garmin)
    connections_router = create_connections_router(
        templates=templates,
        db_client=db_client,
        encryptor=encryptor,
        jwt_secret=config.jwt_secret_key,
        jwt_algorithm=config.jwt_algorithm,
        google_client_id=config.google_client_id,
        google_client_secret=config.google_client_secret,
        google_redirect_uri=google_connections_redirect_uri,
        app_base_url=connections_base_url,
    )
    application.include_router(connections_router)

    # Polling router (Cloud Scheduler + manual sync)
    google_health_client = GoogleHealthAPIClient()
    if db_client:
        sync_orchestrator = SyncOrchestrator(
            google_client=google_health_client,
            db_client=db_client,
            encryptor=encryptor,
        )
        polling_router = create_polling_router(
            db_client=db_client,
            sync_orchestrator=sync_orchestrator,
            config=config,
        )
        application.include_router(polling_router)

    # Bulk sync router (auth-gated)
    if db_client:
        sync_orchestrator = SyncOrchestrator(
            google_client=google_health_client,
            db_client=db_client,
            encryptor=encryptor,
        )
        bulk_sync_router = create_bulk_sync_router(
            templates=templates,
            db_client=db_client,
            google_client=google_health_client,
            garmin_client=None,
            sync_orchestrator=sync_orchestrator,
            oauth_service=oauth_service,
            config=config,
            encryptor=encryptor,
        )
        application.include_router(bulk_sync_router)

    # Webhooks router (stub, CSRF exempt)
    webhooks_router = create_webhooks_router()
    application.include_router(webhooks_router)

    return application


app = _create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
