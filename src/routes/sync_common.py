"""Shared utility functions for sync routes (weight, blood pressure, etc.)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def _normalize_utc(ts):
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def format_measurement_timestamp(dt):
    """Format a datetime for display in sync tables."""
    return dt.isoformat() if hasattr(dt, "isoformat") else str(dt)


def compare_measurements_with_garmin(measurements, garmin_weights, tolerance_minutes=5):
    """Compare measurements against Garmin weights, returning synced flags.

    Args:
        measurements: List of measurement dicts with 'timestamp' key.
        garmin_weights: List of Garmin weight dicts with 'timestamp_utc' key.
        tolerance_minutes: Max time difference to consider a match.

    Returns:
        List of booleans indicating which measurements are already synced.
    """
    tolerance = timedelta(minutes=tolerance_minutes)
    synced_flags = []
    for m in measurements:
        measurement_utc = _normalize_utc(m["timestamp"])
        found = False
        for garmin_weight in garmin_weights:
            garmin_ts_raw = garmin_weight.get("timestamp_utc")
            if not isinstance(garmin_ts_raw, datetime):
                continue
            garmin_utc = _normalize_utc(garmin_ts_raw)
            if abs(garmin_utc - measurement_utc) <= tolerance:
                found = True
                break
        synced_flags.append(found)
    return synced_flags


def build_sync_row_html(row_data, columns):
    """Build a single HTML table row for a sync measurement.

    Args:
        row_data: Dict with keys: row_id, timestamp, is_synced, and column-specific data.
        columns: List of dicts with keys: key, label, format_fn (optional).

    Returns:
        HTML string for a single <tr>.
    """
    row_id = row_data["row_id"]
    ts_iso = format_measurement_timestamp(row_data["timestamp"])
    is_synced = row_data.get("is_synced", False)

    data_attrs = " ".join(
        f'data-{col["key"].replace("_", "-")}="{row_data.get(col["key"], "")}"'
        for col in columns
        if col["key"] != "timestamp"
    )

    if is_synced:
        tr_open = (
            f'<tr id="row-{row_id}" data-timestamp="{ts_iso}" {data_attrs} '
            f'class="synced" aria-disabled="true" style="opacity:0.5">'
        )
        first_td = "<td><small>✓ Synced</small></td>"
    else:
        tr_open = f'<tr id="row-{row_id}" data-timestamp="{ts_iso}" {data_attrs}>'
        first_td = f'<td><input type="checkbox" name="selected" value="{row_id}"></td>'

    cells = [
        first_td,
        f'<td class="timestamp" data-utc="{ts_iso}">{ts_iso}</td>',
    ]
    for col in columns:
        if col["key"] == "timestamp":
            continue
        display_value = row_data.get(f"{col['key']}_display", "\u2014")
        cells.append(f"<td>{display_value}</td>")

    return tr_open + "".join(cells) + "</tr>"


def build_sync_table_html(rows, columns, load_more_url, offset, limit):
    """Build HTML table (or just rows for pagination) for sync measurements.

    Args:
        rows: List of row_data dicts (see build_sync_row_html).
        columns: List of column dicts with keys: key, label.
        load_more_url: URL for the "Load more" button hx-get.
        offset: Current offset (0 means render full table with thead).
        limit: Number of days per page (used for next load-more URL param).

    Returns:
        HTML string — full <table> if offset==0, otherwise just <tr> rows + load-more.
    """
    colspan = len(columns) + 1  # +1 for checkbox column

    row_htmls = [build_sync_row_html(row, columns) for row in rows]
    table_rows = "\n".join(row_htmls)

    load_more_button = (
        '<tr id="load-more-row">'
        f'<td colspan="{colspan}" style="text-align: center; padding: 1rem;">'
        f'<button hx-get="{load_more_url}" '
        'hx-target="#load-more-row" hx-swap="outerHTML" hx-indicator="#load-more-spinner" '
        'style="margin: 0;">Load more</button>'
        "</td></tr>"
    )

    if offset == 0:
        header_cells = "<th></th>" + "".join(f"<th>{col['label']}</th>" for col in columns)
        return (
            "<table>\n"
            f"<thead><tr>{header_cells}</tr></thead>\n"
            '<tbody id="measurements-body">\n'
            f"{table_rows}\n"
            f"{load_more_button}\n"
            "</tbody>\n"
            "</table>"
        )

    return f"{table_rows}\n{load_more_button}"
