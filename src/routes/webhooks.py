"""Webhook endpoints for Google Health API (stub)."""

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = structlog.get_logger()


def create_webhooks_router() -> APIRouter:
    """Create and configure webhooks router.

    Returns:
        APIRouter: Configured router with webhook endpoints.
    """
    router = APIRouter(prefix="/webhooks", tags=["webhooks"])

    @router.post("/google-health")
    async def google_health_webhook(request: Request) -> JSONResponse:
        """Receive Google Health API webhook for weight measurements.

        TODO: Implement Google Health webhook handler for real-time sync
        """
        try:
            body = await request.json()
            logger.info("Received webhook", body=body)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to parse webhook body", error=str(exc))

        return JSONResponse({"status": "not_implemented"}, status_code=200)

    return router
