"""FastAPI application for garth-relay."""

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from src.config import get_config
from src.db import FirestoreClient
from src.logging_setup import setup_logging

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
        yield
        logger.info("Application shutting down")

    application = FastAPI(
        title="garth-relay",
        description="Garth Relay service",
        version="0.1.0",
        lifespan=lifespan,
    )

    @application.get("/health")
    async def health_check():
        return {"status": "ok"}

    return application


app = _create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
