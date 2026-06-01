"""Bulk sync routes — manual sync trigger and measurements list with HTMX."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.templating import Jinja2Templates

from src.auth.google_oauth2 import GoogleOAuth2Service
from src.auth.session import get_current_user
from src.config import AppConfig
from src.crypto.token_encryptor import TokenEncryptor
from src.db.firestore_client import FirestoreClient
from src.models.oauth_models import OAuthToken
from src.services.garmin_client import (
    GarminClient,
    GarminRateLimitError,
    GarminSessionExpiredError,
)
from src.services.google_health_client import (
    GoogleHealthAPIClient,
    GoogleScopeRevokedError,
    GoogleTokenExpiredError,
    Measurement,
)
from src.services.sync_orchestrator import SyncOrchestrator

logger = structlog.get_logger()

_REAUTH_HTML = (
    '<p style="color: var(--pico-del-color);">Google authentication failed. '
    'Please <a href="/dashboard">reconnect Google Health</a>.</p>'
)

_GARMIN_EXPIRED_HTML = (
    '<p style="color: var(--pico-color-yellow-450);">'
    "\u26a0\ufe0f Garmin session expired \u2014 "
    '<a href="/connect-garmin">reconnect</a> to see sync status.</p>'
)


async def _refresh_google_token(
    oauth_service: GoogleOAuth2Service,
    db_client: FirestoreClient,
    user_id: str,
    refresh_token: str | None,
) -> str | None:
    refreshed = await oauth_service.refresh_access_token(refresh_token or "")
    if not refreshed:
        return None
    expires_at = datetime.now(UTC) + timedelta(hours=1)
    _ = db_client.save_oauth_token(user_id, "google", refreshed.access_token, refresh_token or "", expires_at)
    return refreshed.access_token


def _build_measurements_rows(
    measurements: list[Measurement],
    synced_flags: list[bool] | None = None,
    start_index: int = 0,
    next_offset_days: int = 60,
    days: int = 30,
) -> str:
    rows: list[str] = []
    for index, m in enumerate(measurements):
        weight_kg = m.get("weight_kg")
        body_fat_pct = m.get("body_fat_pct")
        timestamp = m["timestamp"]

        ts_iso = timestamp.isoformat() if hasattr(timestamp, "isoformat") else str(timestamp)

        weight_display = f"{weight_kg:.1f}" if weight_kg is not None else "\u2014"
        body_fat_display = f"{body_fat_pct:.1f}" if body_fat_pct else "\u2014"

        data_bf = f"{body_fat_pct}" if body_fat_pct else ""
        data_weight = f"{weight_kg}" if weight_kg is not None else ""

        is_synced = synced_flags[index] if synced_flags else False
        row_id = start_index + index

        if is_synced:
            tr_open = (
                f'<tr id="row-{row_id}" data-timestamp="{ts_iso}" '
                f'data-weight="{data_weight}" data-body-fat="{data_bf}" '
                f'class="synced" aria-disabled="true" style="opacity:0.5">'
            )
            first_td = "<td><small>✓ Synced</small></td>"
        else:
            tr_open = f'<tr id="row-{row_id}" data-timestamp="{ts_iso}" data-weight="{data_weight}" data-body-fat="{data_bf}">'
            first_td = f'<td><input type="checkbox" name="selected" value="{row_id}"></td>'

        rows.append(
            "".join(
                [
                    tr_open,
                    first_td,
                    f'<td class="timestamp" data-utc="{ts_iso}">{ts_iso}</td>',
                    f"<td>{weight_display}</td>",
                    f"<td>{body_fat_display}</td>",
                    "</tr>",
                ]
            )
        )

    table_rows = "\n".join(rows)
    load_more_button = (
        '<tr id="load-more-row">'
        '<td colspan="4" style="text-align: center; padding: 1rem;">'
        f'<button hx-get="/bulk-sync/measurements?days={days}&offset_days={next_offset_days}" '
        'hx-target="#load-more-row" hx-swap="outerHTML" hx-indicator="#load-more-spinner" '
        'style="margin: 0;">Load more</button>'
        "</td></tr>"
    )
    return f"{table_rows}\n{load_more_button}"


def _build_measurements_table(
    measurements: list[Measurement],
    synced_flags: list[bool] | None = None,
    start_index: int = 0,
    next_offset_days: int = 30,
    days: int = 30,
) -> str:
    rows: list[str] = []
    for index, m in enumerate(measurements):
        weight_kg = m.get("weight_kg")
        body_fat_pct = m.get("body_fat_pct")
        timestamp = m["timestamp"]

        ts_iso = timestamp.isoformat() if hasattr(timestamp, "isoformat") else str(timestamp)

        weight_display = f"{weight_kg:.1f}" if weight_kg is not None else "\u2014"
        body_fat_display = f"{body_fat_pct:.1f}" if body_fat_pct else "\u2014"

        data_bf = f"{body_fat_pct}" if body_fat_pct else ""
        data_weight = f"{weight_kg}" if weight_kg is not None else ""

        is_synced = synced_flags[index] if synced_flags else False
        row_id = start_index + index

        if is_synced:
            tr_open = (
                f'<tr id="row-{row_id}" data-timestamp="{ts_iso}" '
                f'data-weight="{data_weight}" data-body-fat="{data_bf}" '
                f'class="synced" aria-disabled="true" style="opacity:0.5">'
            )
            first_td = "<td><small>✓ Synced</small></td>"
        else:
            tr_open = f'<tr id="row-{row_id}" data-timestamp="{ts_iso}" data-weight="{data_weight}" data-body-fat="{data_bf}">'
            first_td = f'<td><input type="checkbox" name="selected" value="{row_id}"></td>'

        rows.append(
            "".join(
                [
                    tr_open,
                    first_td,
                    f'<td class="timestamp" data-utc="{ts_iso}">{ts_iso}</td>',
                    f"<td>{weight_display}</td>",
                    f"<td>{body_fat_display}</td>",
                    "</tr>",
                ]
            )
        )

    table_rows = "\n".join(rows)
    load_more_button = (
        '<tr id="load-more-row">'
        '<td colspan="4" style="text-align: center; padding: 1rem;">'
        f'<button hx-get="/bulk-sync/measurements?days={days}&offset_days={next_offset_days}" '
        'hx-target="#load-more-row" hx-swap="outerHTML" hx-indicator="#load-more-spinner" '
        'style="margin: 0;">Load more</button>'
        "</td></tr>"
    )

    return (
        "<table>\n"
        "<thead><tr><th></th><th>Date / Time</th><th>Weight (kg)</th><th>Body Fat (%)</th></tr></thead>\n"
        '<tbody id="measurements-body">\n'
        f"{table_rows}\n"
        f"{load_more_button}\n"
        "</tbody>\n"
        "</table>"
    )


def _parse_timestamp(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    return datetime.fromisoformat(normalized)


def _format_value(value: str, precision: int = 1) -> str:
    if value == "":
        return "\u2014"
    return f"{float(value):.{precision}f}"


def _build_synced_row_html(row_index: str, timestamp: str, weight_kg: str, body_fat_pct: str) -> str:
    return (
        f'<tr id="row-{row_index}" class="synced" aria-disabled="true" style="opacity:0.5">'
        "<td><small>✓ Synced</small></td>"
        f'<td class="timestamp" data-utc="{timestamp}">{timestamp}</td>'
        f"<td>{_format_value(weight_kg)}</td>"
        f"<td>{_format_value(body_fat_pct)}</td>"
        "</tr>"
    )


def _build_failed_row_html(row_index: str, timestamp: str, weight_kg: str, body_fat_pct: str, error: str) -> str:
    return (
        f'<tr id="row-{row_index}" data-timestamp="{timestamp}" data-weight="{weight_kg}" data-body-fat="{body_fat_pct}">'
        f'<td><input type="checkbox" name="selected" value="{row_index}" checked></td>'
        f'<td class="timestamp" data-utc="{timestamp}">{timestamp}</td>'
        f"<td>{_format_value(weight_kg)}</td>"
        f"<td>{_format_value(body_fat_pct)}</td>"
        f'<td style="color:var(--pico-del-color)">✗ {error}</td>'
        "</tr>"
    )


def _normalize_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def _has_matching_weight(
    garmin_weights: list[dict[str, object]],
    measurement_timestamp: datetime,
    tolerance: timedelta,
) -> bool:
    measurement_utc = _normalize_utc(measurement_timestamp)
    for garmin_weight in garmin_weights:
        garmin_ts_raw = garmin_weight.get("timestamp_utc")
        if not isinstance(garmin_ts_raw, datetime):
            continue
        garmin_utc = _normalize_utc(garmin_ts_raw)
        if abs(garmin_utc - measurement_utc) <= tolerance:
            return True
    return False


def _sync_error_message(result_message: str) -> str:
    if result_message == "garmin_session_expired":
        return "Garmin session expired"
    if result_message == "garmin_rate_limited":
        return "Rate limited \u2014 try again later"
    if result_message:
        return result_message.replace("_", " ")
    return "Sync failed"


async def _upload_single_record(
    sync_orchestrator: SyncOrchestrator,
    user_id: str,
    parsed_weight: float,
    parsed_body_fat: float | None,
    parsed_timestamp: datetime,
) -> tuple[str | None, str | None]:
    try:
        result = await sync_orchestrator.upload_measurement(
            user_id,
            parsed_weight,
            parsed_body_fat,
            parsed_timestamp,
            source="manual_bulk",
        )
        return result.status, result.message
    except GarminSessionExpiredError:
        return None, "Garmin session expired"
    except GarminRateLimitError:
        return None, "Rate limited \u2014 try again later"
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected sync-record failure for user %s", user_id)
        return None, f"Sync failed: {exc}"


def _create_bulk_sync_page_handler(
    templates: Jinja2Templates,
    db_client: FirestoreClient,
    config: AppConfig,
):
    async def bulk_sync_page(request: Request):
        try:
            user_id = await get_current_user(request, config.jwt_secret_key, config.jwt_algorithm)
        except HTTPException:
            return RedirectResponse(url="/login", status_code=302)

        google_token = db_client.get_oauth_token(user_id, "google")
        garmin_session = db_client.has_garmin_session(user_id)

        return templates.TemplateResponse(
            request,
            "bulk-sync.html",
            {
                "request": request,
                "google_connected": google_token is not None,
                "garmin_connected": garmin_session,
                "error_message": None,
            },
        )

    return bulk_sync_page


def _create_bulk_sync_measurements_handler(
    db_client: FirestoreClient,
    google_client: GoogleHealthAPIClient,
    garmin_client: GarminClient | None,
    encryptor: TokenEncryptor | None,
    oauth_service: GoogleOAuth2Service,
    config: AppConfig,
):
    async def bulk_sync_measurements(request: Request, days: int = 30, offset_days: int = 0):
        try:
            user_id = await get_current_user(request, config.jwt_secret_key, config.jwt_algorithm)
        except HTTPException:
            return RedirectResponse(url="/login", status_code=302)

        google_token = db_client.get_oauth_token(user_id, "google")
        if not google_token:
            error_msg = '<p style="color: var(--pico-del-color);">Google Health not connected.</p>'
            return HTMLResponse(error_msg)

        access_token = google_token.access_token
        if google_token.expires_at.replace(tzinfo=UTC) <= datetime.now(UTC):
            logger.info("Google token expired for user %s, refreshing", user_id)
            access_token = await _refresh_google_token(oauth_service, db_client, user_id, google_token.refresh_token)
            if not access_token:
                return HTMLResponse(_REAUTH_HTML)

        until_ts = datetime.now(UTC) - timedelta(days=offset_days)
        since_ts = until_ts - timedelta(days=days)

        measurements = await _fetch_with_retry(
            google_client, oauth_service, db_client, user_id, google_token, access_token, since_ts, until_ts
        )
        if measurements is None:
            return HTMLResponse(_REAUTH_HTML)

        measurements.sort(key=lambda m: m["timestamp"], reverse=True)

        if not measurements:
            no_data_html = (
                '<tr id="load-more-row"><td colspan="4" style="text-align: center; padding: 1rem;">No older data available.</td></tr>'
                if offset_days > 0
                else f"<p>No measurements found in the last {days} days</p>"
            )
            return HTMLResponse(no_data_html)

        garmin_warning, synced_flags = await _compare_with_garmin(
            db_client,
            garmin_client,
            encryptor,
            user_id,
            measurements,
            until_ts,
            days,
            offset_days,
        )

        if offset_days == 0:
            table_html = _build_measurements_table(
                measurements, synced_flags=synced_flags, next_offset_days=offset_days + days, days=days
            )
            html_content = garmin_warning + table_html
        else:
            start_index = offset_days
            html_content = _build_measurements_rows(
                measurements,
                synced_flags=synced_flags,
                start_index=start_index,
                next_offset_days=offset_days + days,
                days=days,
            )

        return HTMLResponse(html_content)

    return bulk_sync_measurements


def _create_bulk_sync_record_handler(sync_orchestrator: SyncOrchestrator, config: AppConfig):
    async def bulk_sync_sync_record(request: Request):
        try:
            user_id = await get_current_user(request, config.jwt_secret_key, config.jwt_algorithm)
        except HTTPException:
            return RedirectResponse(url="/login", status_code=302)

        form = await request.form()
        timestamp = str(form.get("timestamp", "")).strip()
        weight_kg = str(form.get("weight_kg", "")).strip()
        body_fat_pct = str(form.get("body_fat_pct", "")).strip()
        row_index = str(form.get("row_index", "")).strip()
        await form.close()

        if not timestamp or not weight_kg or not row_index:
            error_html = _build_failed_row_html(
                row_index or "unknown",
                timestamp,
                weight_kg,
                body_fat_pct,
                "Missing required sync fields",
            )
            return HTMLResponse(error_html, status_code=400)

        try:
            parsed_timestamp = _parse_timestamp(timestamp)
            parsed_weight = float(weight_kg)
            parsed_body_fat = float(body_fat_pct) if body_fat_pct else None
        except ValueError:
            return HTMLResponse(
                _build_failed_row_html(row_index, timestamp, weight_kg, body_fat_pct, "Invalid measurement format"),
                status_code=400,
            )

        result_status, result_message = await _upload_single_record(
            sync_orchestrator,
            user_id,
            parsed_weight,
            parsed_body_fat,
            parsed_timestamp,
        )
        if result_status == "success":
            return HTMLResponse(_build_synced_row_html(row_index, timestamp, weight_kg, body_fat_pct))

        message = _sync_error_message(result_message or "")
        return HTMLResponse(_build_failed_row_html(row_index, timestamp, weight_kg, body_fat_pct, message))

    return bulk_sync_sync_record


async def _compare_with_garmin(
    db_client: FirestoreClient,
    _garmin_client: GarminClient,
    encryptor: TokenEncryptor,
    user_id: str,
    measurements: list[Measurement],
    until_ts: datetime,
    days: int,
    offset_days: int,
) -> tuple[str, list[bool] | None]:
    garmin_session = db_client.has_garmin_session(user_id)
    if not garmin_session:
        return _GARMIN_EXPIRED_HTML, None

    try:
        client = GarminClient.create_for_user(user_id, db_client.db, encryptor)
        garmin_weights = await client.fetch_existing_weights(end_date=until_ts.date(), days=days + offset_days)
    except GarminSessionExpiredError:
        logger.warning("Garmin session expired for user %s during bulk-sync comparison", user_id)
        return _GARMIN_EXPIRED_HTML, None

    tolerance = timedelta(minutes=5)
    synced_flags = [_has_matching_weight(garmin_weights, m["timestamp"], tolerance) for m in measurements]
    return "", synced_flags


def create_bulk_sync_router(
    templates: Jinja2Templates,
    db_client: FirestoreClient,
    google_client: GoogleHealthAPIClient,
    garmin_client: GarminClient | None,
    sync_orchestrator: SyncOrchestrator,
    oauth_service: GoogleOAuth2Service,
    config: AppConfig,
    encryptor: TokenEncryptor | None = None,
) -> APIRouter:
    """Create and configure bulk sync router."""
    bulk_sync_router = APIRouter(tags=["bulk_sync"])

    bulk_sync_router.add_api_route(
        "/bulk-sync",
        _create_bulk_sync_page_handler(templates, db_client, config),
        methods=["GET"],
    )
    bulk_sync_router.add_api_route(
        "/bulk-sync/measurements",
        _create_bulk_sync_measurements_handler(
            db_client,
            google_client,
            garmin_client,
            encryptor,
            oauth_service,
            config,
        ),
        methods=["GET"],
    )
    bulk_sync_router.add_api_route(
        "/bulk-sync/sync-record",
        _create_bulk_sync_record_handler(sync_orchestrator, config),
        methods=["POST"],
    )

    return bulk_sync_router


async def _fetch_with_retry(
    google_client: GoogleHealthAPIClient,
    oauth_service: GoogleOAuth2Service,
    db_client: FirestoreClient,
    user_id: str,
    google_token: OAuthToken,
    access_token: str,
    since_ts: datetime,
    until_ts: datetime,
) -> list[Measurement] | None:
    try:
        return await google_client.fetch_all_measurements(access_token, since_ts.isoformat(), until_ts.isoformat())
    except GoogleTokenExpiredError:
        logger.warning("Google token expired during fetch for user %s, attempting refresh", user_id)
    except GoogleScopeRevokedError:
        return None

    refreshed_token = await _refresh_google_token(oauth_service, db_client, user_id, google_token.refresh_token)
    if not refreshed_token:
        return None

    try:
        return await google_client.fetch_all_measurements(refreshed_token, since_ts.isoformat(), until_ts.isoformat())
    except (GoogleTokenExpiredError, GoogleScopeRevokedError):
        return None
