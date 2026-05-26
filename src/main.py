"""FastAPI application for garth-relay."""

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from src.logging_setup import setup_logging

setup_logging()
logger = structlog.get_logger()


def _create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        logger.info("Application starting up")
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
