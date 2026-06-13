"""Webhook endpoints for Google Health API."""

import base64
import json
import time

import httpx
import structlog
import tink
from fastapi import APIRouter, BackgroundTasks, Request, Response
from tink import cleartext_keyset_handle, signature

from src.db.firestore_client import FirestoreClient
from src.services.sync_orchestrator import SyncOrchestrator

logger = structlog.get_logger()

# Register Tink signature primitives globally
signature.register()


class KeysetStore:
    cache: str | None = None
    last_fetched: float = 0.0


_keyset_cache_duration = 86400.0  # 24 hours


async def _get_public_keyset() -> str:
    """Retrieve and cache the Google Health API webhooks public keyset."""
    now = time.time()
    if KeysetStore.cache is None or (now - KeysetStore.last_fetched) > _keyset_cache_duration:
        logger.info("Fetching Google Health API public keyset")
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get("https://www.gstatic.com/googlehealthapi/webhooks/webhooks_public_keyset.json")
            resp.raise_for_status()
            KeysetStore.cache = resp.text
            KeysetStore.last_fetched = now
    return KeysetStore.cache or ""


def _verify_handshake(auth_header: str | None, webhook_secret: str) -> Response:
    """Perform webhook challenge verification handshake checks."""
    if not webhook_secret:
        logger.error("APP_GOOGLE_HEALTH_WEBHOOK_SECRET is not configured, rejecting verification")
        return Response(status_code=401)

    if auth_header:
        provided = auth_header.strip()
        if provided.startswith("Bearer "):
            provided = provided[7:].strip()
        if provided == webhook_secret:
            logger.info("Successfully passed webhook handshake verification (authorized)")
            return Response(status_code=201)

    logger.info("Rejecting webhook handshake verification (unauthorized test)")
    return Response(status_code=401)


def _verify_signature(sig_header: str, raw_body: bytes, keyset_json: str) -> Response | None:
    """Verify that incoming notifications are signed by Google Health API."""
    try:
        sig_bytes = base64.b64decode(sig_header)
        reader = tink.JsonKeysetReader(keyset_json)
        keyset_handle = cleartext_keyset_handle.read(reader)
        verifier = keyset_handle.primitive(signature.PublicKeyVerify)
        verifier.verify(sig_bytes, raw_body)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Signature verification failed", error=str(exc))
        return Response(content="Invalid signature", status_code=401)


def create_webhooks_router(  # noqa: C901
    db_client: FirestoreClient | None,
    sync_orchestrator: SyncOrchestrator | None,
    webhook_secret: str,
) -> APIRouter:
    """Create and configure webhooks router.

    Args:
        db_client: Firestore client.
        sync_orchestrator: Sync orchestrator service.
        webhook_secret: Configured shared secret for webhook verification.

    Returns:
        APIRouter: Configured router with webhook endpoints.
    """
    router = APIRouter(prefix="/webhooks", tags=["webhooks"])

    @router.post("/google-health")
    async def google_health_webhook(  # noqa: C901, PLR0912
        request: Request,
        background_tasks: BackgroundTasks,
    ) -> Response:
        """Receive Google Health API webhook for weight measurements.

        Handles verification challenge and real-time sync notifications.
        """
        try:
            raw_body = await request.body()
            payload = json.loads(raw_body) if raw_body else {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to parse webhook body as JSON", error=str(exc))
            return Response(content="Invalid JSON", status_code=400)

        # ── 1. Handshake Challenge ───────────────────────────────────────────
        is_verification = False
        if isinstance(payload, dict) and payload.get("type") == "verification":
            is_verification = True
        elif (
            isinstance(payload, list)
            and len(payload) > 0
            and isinstance(payload[0], dict)
            and payload[0].get("type") == "verification"
        ):
            is_verification = True

        if is_verification:
            auth_header = request.headers.get("Authorization")
            return _verify_handshake(auth_header, webhook_secret)

        # ── 2. Signature Verification ────────────────────────────────────────
        sig_header = request.headers.get("X-HEALTHAPI-SIGNATURE") or request.headers.get("GOOGLE-HEALTH-API-SIGNATURE")
        if not sig_header:
            logger.warning(
                "Received notification without signature header (checked X-HEALTHAPI-SIGNATURE and GOOGLE-HEALTH-API-SIGNATURE)",
                headers=dict(request.headers),
            )
            return Response(content="Missing signature", status_code=401)

        try:
            keyset_json = await _get_public_keyset()
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to retrieve public keyset", error=str(exc))
            return Response(content="Key retrieval failed", status_code=500)

        sig_error_response = _verify_signature(sig_header, raw_body, keyset_json)
        if sig_error_response:
            return sig_error_response

        # ── 3. Zpracování notifikací ─────────────────────────────────────────
        notifications = payload if isinstance(payload, list) else [payload]

        for item in notifications:
            if not isinstance(item, dict):
                continue
            data = item.get("data", {})
            health_user_id = data.get("healthUserId")
            data_type = data.get("dataType")

            if health_user_id and data_type and data_type in ("weight", "body-fat") and db_client and sync_orchestrator:
                user_id = db_client.get_user_id_by_health_user_id(health_user_id)
                if user_id:
                    logger.info(
                        "Received Google Health change notification, scheduling sync task",
                        user_id=user_id,
                        health_user_id=health_user_id,
                        data_type=data_type,
                    )
                    background_tasks.add_task(sync_orchestrator.sync_user, user_id)
                else:
                    logger.warning(
                        "Received webhook for unmatched Google Health user ID", health_user_id=health_user_id
                    )
            elif not health_user_id or not data_type:
                logger.warning("Notification missing healthUserId or dataType", data=data)
            elif data_type not in ("weight", "body-fat"):
                logger.debug("Skipping unsupported data type notification", data_type=data_type)
            else:
                logger.warning("Database or SyncOrchestrator not configured, skipping sync processing")

        return Response(status_code=204)

    return router
