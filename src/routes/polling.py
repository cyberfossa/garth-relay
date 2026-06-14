"""Polling routes — Cloud Scheduler trigger and manual sync with HTMX feedback."""

from __future__ import annotations

from html import escape
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from src.auth.session import get_current_user
from src.config import AppConfig
from src.db.firestore_client import FirestoreClient
from src.services.sync_orchestrator import SyncOrchestrator

logger = structlog.get_logger()

SYNC_MESSAGE_MAP: dict[str, tuple[str, str]] = {
    "sync_completed": ("success", "Synced: {uploaded} new measurements → Garmin ({skipped} skipped — already exists)"),
    "no_measurements": ("info", "No new measurements to sync (all {total} already in Garmin)"),
    "no_new_data": ("info", "No new measurements to sync (all {total} already in Garmin)"),
    "garmin_session_expired": ("error", "Garmin session expired — please reconnect"),
    "garmin_rate_limited": ("error", "Garmin rate limit reached, try again later"),
    "missing_garmin_session": ("error", "Garmin is not connected — please connect first"),
    "missing_google_token": ("error", "Google Health is not connected"),
    "google_refresh_failed": ("error", "Could not refresh Google Health token"),
    "google_auth_error": ("error", "Google Health authentication error"),
    "google_scope_revoked": ("error", "Google Health access was revoked — please reconnect"),
    "unexpected_error": ("error", "An unexpected error occurred"),
    "missing_omron_tokens": ("error", "Omron Connect is not connected"),
    "omron_session_expired": ("error", "Omron Connect session expired — please reconnect"),
    "omron_auth_failed": ("error", "Omron Connect authentication failed"),
    "no_bpm_devices": ("info", "No blood pressure devices registered in Omron Connect"),
    "omron_device_fetch_failed": ("error", "Failed to fetch Omron devices"),
    "omron_measurement_fetch_failed": ("error", "Failed to fetch Omron measurements"),
    "garmin_fetch_failed": ("error", "Failed to fetch Garmin blood pressures"),
    "garmin_upload_failed": ("error", "Failed to upload blood pressures to Garmin"),
}


def _sync_result_html(result_type: str, message: str) -> str:
    return f'<div class="notice {escape(result_type)}">{escape(message)}</div>'


def _format_sync_message(message_key: str, uploaded: int = 0, skipped: int = 0, total: int = 0) -> tuple[str, str]:
    entry = SYNC_MESSAGE_MAP.get(message_key)
    if not entry:
        return ("error", f"Sync finished: {message_key}")
    result_type, template = entry
    return result_type, template.format(uploaded=uploaded, skipped=skipped, total=total)


def _sync_logs_table_html(sync_logs: list[dict[str, Any]]) -> str:
    if not sync_logs:
        return '<p class="muted">No sync history yet.</p>'

    rows: list[str] = []
    for log in sync_logs:
        timestamp = log.get("timestamp", "")
        ts_display = timestamp.strftime("%Y-%m-%d %H:%M") if hasattr(timestamp, "strftime") else str(timestamp)
        status = escape(str(log.get("status", "")))
        weight = log.get("weight_kg")
        weight_display = f"{weight:.1f} kg" if weight is not None else "\u2014"
        error_msg = log.get("error_message") or ""
        error_display = escape(str(error_msg)) if error_msg else ""

        status_badge = f'<mark class="{status}">{status}</mark>'
        detail = error_display if error_display else weight_display

        rows.append(f"<tr><td>{ts_display}</td><td>{status_badge}</td><td>{detail}</td></tr>")

    table_rows = "\n".join(rows)
    return f"""<table role="grid">
<thead><tr><th>Time</th><th>Status</th><th>Detail</th></tr></thead>
<tbody>
{table_rows}
</tbody>
</table>"""


async def _require_user_or_htmx_redirect(request: Request, config: AppConfig) -> str:
    try:
        return await get_current_user(request, config.jwt_secret_key, config.jwt_algorithm)
    except HTTPException:
        if request.headers.get("HX-Request") == "true":
            raise _htmx_auth_error("Session expired — please log in again") from None
        raise


class _HtmxAuthError(Exception):
    def __init__(self, response: HTMLResponse):
        super().__init__()
        self.response: HTMLResponse = response


def _htmx_auth_error(message: str) -> _HtmxAuthError:
    return _HtmxAuthError(
        HTMLResponse(
            _sync_result_html("error", message),
            status_code=401,
            headers={"HX-Redirect": "/login"},
        )
    )


def create_polling_router(  # noqa: C901
    db_client: FirestoreClient,
    sync_orchestrator: SyncOrchestrator,
    config: AppConfig,
) -> APIRouter:
    """Create and configure polling router."""
    router = APIRouter(prefix="/polling", tags=["polling"])

    @router.post("/poll")
    async def poll(request: Request):
        """Cloud Scheduler endpoint — triggers sync for all users.

        This endpoint is NOT auth-gated (called by Cloud Scheduler).
        CSRF exempt via middleware config.
        """
        logger.info("poll triggered by Cloud Scheduler")

        try:
            summary = await sync_orchestrator.sync_all_users()
            logger.info(
                "poll complete",
                synced=summary.synced,
                skipped=summary.skipped,
                errors=summary.errors,
                total=summary.total,
                duration_seconds=summary.duration_seconds,
            )
            return JSONResponse(
                {
                    "status": "ok",
                    "synced": summary.synced,
                    "skipped": summary.skipped,
                    "errors": summary.errors,
                    "total": summary.total,
                    "duration_seconds": summary.duration_seconds,
                }
            )
        except Exception:
            logger.exception("poll failed")
            return JSONResponse({"status": "error", "detail": "Poll failed"}, status_code=500)

    @router.post("/sync-now")
    async def sync_now(request: Request):
        try:
            user_id = await _require_user_or_htmx_redirect(request, config)
        except _HtmxAuthError as exc:
            return exc.response

        logger.info("sync_now triggered", user_id=user_id)

        notices: list[str] = []

        try:
            result = await sync_orchestrator.sync_user(user_id)
            result_type, message = _format_sync_message(
                result.message,
                uploaded=result.uploaded,
                skipped=result.skipped,
                total=result.total,
            )
            notices.append(_sync_result_html(result_type, f"Váha: {message}"))
        except Exception:
            logger.exception("Unexpected error in sync_now weight sync for user %s", user_id)
            notices.append(_sync_result_html("error", "Váha: Došlo k neočekávané chybě"))

        try:
            profile = db_client.get_user_profile(user_id)
            if profile and getattr(profile, "omron_sync_enabled", False):
                omron_result = await sync_orchestrator.sync_omron_user(user_id)
                omron_type, omron_message = _format_sync_message(
                    omron_result.message,
                    uploaded=omron_result.uploaded,
                    skipped=omron_result.skipped,
                    total=omron_result.total,
                )
                notices.append(_sync_result_html(omron_type, f"Krevní tlak: {omron_message}"))
        except Exception:
            logger.exception("Unexpected error in sync_now Omron sync for user %s", user_id)
            notices.append(_sync_result_html("error", "Krevní tlak: Došlo k neočekávané chybě"))

        return HTMLResponse("\n".join(notices))

    @router.get("/sync-logs")
    async def sync_logs(request: Request):
        try:
            user_id = await _require_user_or_htmx_redirect(request, config)
        except _HtmxAuthError as exc:
            return exc.response

        recent_syncs = db_client.get_recent_syncs(user_id, limit=10)
        return HTMLResponse(_sync_logs_table_html(recent_syncs))

    @router.post("/sync-all")
    async def sync_all(request: Request):
        try:
            _ = await _require_user_or_htmx_redirect(request, config)
        except _HtmxAuthError as exc:
            return exc.response

        logger.info("sync_all triggered (admin)")

        try:
            summary = await sync_orchestrator.sync_all_users()
            html = (
                f'<div class="notice success">'
                f"Poll complete: {summary.synced} synced, {summary.skipped} skipped, "
                f"{summary.errors} errors out of {summary.total} users "
                f"({summary.duration_seconds:.1f}s)"
                f"</div>"
            )
        except Exception:
            logger.exception("sync_all failed")
            html = _sync_result_html("error", "Poll failed — check server logs")

        return HTMLResponse(html)

    return router
