from datetime import UTC, datetime, timedelta

from src.routes.sync_common import (
    build_sync_row_html,
    build_sync_table_html,
    compare_measurements_with_garmin,
    format_measurement_timestamp,
)


def _measurement(ts=None):
    return {
        "timestamp": ts or datetime(2026, 4, 10, 8, 0, tzinfo=UTC),
        "weight_kg": 80.5,
        "weight_kg_display": "80.5 kg",
        "body_fat_pct": 20.1,
        "body_fat_pct_display": "20.1%",
    }


def _columns():
    return [
        {"key": "timestamp", "label": "Timestamp"},
        {"key": "weight_kg", "label": "Weight"},
        {"key": "body_fat_pct", "label": "Body fat"},
    ]


class TestCompareMeasurementsWithGarmin:
    def test_matching_within_tolerance_returns_true(self):
        measurement_ts = datetime(2026, 4, 10, 8, 0, tzinfo=UTC)
        garmin_ts = measurement_ts + timedelta(minutes=4)

        result = compare_measurements_with_garmin(
            [{"timestamp": measurement_ts}],
            [{"timestamp_utc": garmin_ts}],
        )

        assert result == [True]

    def test_non_matching_beyond_tolerance_returns_false(self):
        measurement_ts = datetime(2026, 4, 10, 8, 0, tzinfo=UTC)
        garmin_ts = measurement_ts + timedelta(minutes=6)

        result = compare_measurements_with_garmin(
            [{"timestamp": measurement_ts}],
            [{"timestamp_utc": garmin_ts}],
        )

        assert result == [False]

    def test_empty_measurements_returns_empty_list(self):
        result = compare_measurements_with_garmin([], [{"timestamp_utc": datetime(2026, 4, 10, 8, 0, tzinfo=UTC)}])

        assert result == []

    def test_empty_garmin_weights_marks_all_false(self):
        result = compare_measurements_with_garmin([_measurement(), _measurement(datetime(2026, 4, 10, 9, 0, tzinfo=UTC))], [])

        assert result == [False, False]

    def test_exact_match_returns_true(self):
        measurement_ts = datetime(2026, 4, 10, 8, 0, tzinfo=UTC)

        result = compare_measurements_with_garmin(
            [{"timestamp": measurement_ts}],
            [{"timestamp_utc": measurement_ts}],
        )

        assert result == [True]


class TestBuildSyncRowHtml:
    def test_builds_unsynced_row(self):
        html = build_sync_row_html(
            {"row_id": 1, "timestamp": datetime(2026, 4, 10, 8, 0, tzinfo=UTC), "weight_kg_display": "80.5 kg"},
            _columns(),
        )

        assert '<tr id="row-1"' in html
        assert 'type="checkbox"' in html
        assert 'data-utc="2026-04-10T08:00:00+00:00"' in html
        assert '<td>80.5 kg</td>' in html

    def test_builds_synced_row(self):
        html = build_sync_row_html(
            {
                "row_id": 2,
                "timestamp": datetime(2026, 4, 10, 8, 0, tzinfo=UTC),
                "is_synced": True,
                "weight_kg_display": "80.5 kg",
            },
            _columns(),
        )

        assert 'class="synced"' in html
        assert 'aria-disabled="true"' in html
        assert '<small>✓ Synced</small>' in html


class TestBuildSyncTableHtml:
    def test_offset_zero_renders_full_table(self):
        html = build_sync_table_html([{"row_id": 1, "timestamp": datetime(2026, 4, 10, 8, 0, tzinfo=UTC), "weight_kg_display": "80.5 kg"}], _columns(), "/load-more?page=2", 0, 10)

        assert html.startswith("<table>")
        assert "<thead>" in html
        assert "<tbody id=\"measurements-body\">" in html
        assert "</table>" in html
        assert 'hx-get="/load-more?page=2"' in html

    def test_offset_positive_renders_rows_and_load_more_only(self):
        html = build_sync_table_html([{"row_id": 1, "timestamp": datetime(2026, 4, 10, 8, 0, tzinfo=UTC), "weight_kg_display": "80.5 kg"}], _columns(), "/load-more?page=3", 5, 10)

        assert "<table>" not in html
        assert "<thead>" not in html
        assert "<tbody" not in html
        assert '<tr id="load-more-row">' in html
        assert 'hx-get="/load-more?page=3"' in html


class TestFormatMeasurementTimestamp:
    def test_datetime_returns_isoformat(self):
        dt = datetime(2026, 4, 10, 8, 0, tzinfo=UTC)

        assert format_measurement_timestamp(dt) == "2026-04-10T08:00:00+00:00"
