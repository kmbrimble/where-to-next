"""Tests for etl/verify_pins.py — mocked Sheets client, no live calls."""
from __future__ import annotations

from etl.models import Day, Stop, Trip, TripMeta
from etl.verify_pins import HEADER, build, compute_rows, merge_preserved


def make_stop(row_num, title, lat=49.0, lng=-123.0, resolved_from="geocoded", address="123 Main St", seq=None) -> Stop:
    return Stop(
        id=f"row{row_num}", seq=seq if seq is not None else row_num, title=title, kind="poi",
        timezone="America/Vancouver", how="drive", travel_minutes=10, dwell_minutes=10, timing="floating",
        row_num=row_num, lat=lat, lng=lng, resolved_from=resolved_from, address=address,
    )


def make_trip(days_stops: list[list[Stop]]) -> Trip:
    return Trip(trip=TripMeta(), days=[
        Day(day=i + 1, date=f"2026-09-{27 + i}", timezone="America/Vancouver", stops=stops)
        for i, stops in enumerate(days_stops)
    ])


class FakeWorksheet:
    def __init__(self, existing_values=None):
        self._values = existing_values or []
        self.cleared = False
        self.updated_with = None

    def get_all_values(self):
        return self._values

    def clear(self):
        self.cleared = True

    def update(self, values, value_input_option=None):
        self.updated_with = values
        self._values = values


def test_column_order():
    assert HEADER == [
        "id", "day", "seq", "plan", "address", "resolved_from", "current_coords",
        "km_to_nearest", "check_pin", "search_again", "corrected_coords", "applied",
    ]


def test_hyperlink_formula_shape():
    trip = make_trip([[make_stop(2, "Shannon Falls", lat=49.6725, lng=-123.1583)]])
    rows = compute_rows(trip)

    row = rows[0]
    assert row["check_pin"] == '=HYPERLINK("https://www.google.com/maps/search/?api=1&query=49.6725,-123.1583","check pin")'
    assert row["search_again"].startswith('=HYPERLINK("https://www.google.com/maps/search/?api=1&query=')
    assert row["search_again"].endswith('","search again")')
    assert "Shannon%20Falls" in row["search_again"]


def test_sort_order_farthest_first():
    far = make_stop(2, "Far", lat=51.0, lng=-116.0)
    near_a = make_stop(3, "NearA", lat=49.0, lng=-123.0)
    near_b = make_stop(4, "NearB", lat=49.001, lng=-123.001)
    trip = make_trip([[far, near_a, near_b]])

    rows = compute_rows(trip)

    assert [r["id"] for r in rows] == ["row2", "row3", "row4"]
    assert rows[0]["km_to_nearest"] > rows[1]["km_to_nearest"]


def test_unresolved_stop_sorts_last_and_has_no_check_pin():
    resolved = make_stop(2, "Resolved", lat=49.0, lng=-123.0)
    unresolved = make_stop(3, "Unresolved", lat=None, lng=None, resolved_from=None)
    trip = make_trip([[resolved, unresolved]])

    rows = compute_rows(trip)

    assert rows[-1]["id"] == "row3"
    assert rows[-1]["check_pin"] == ""
    assert rows[-1]["km_to_nearest"] == ""


def test_merge_preserved_carries_corrected_coords_forward_by_id():
    trip = make_trip([[make_stop(2, "A"), make_stop(3, "B")]])
    rows = compute_rows(trip)
    existing = [
        HEADER,
        ["row2", "1", "2", "A", "123 Main St", "geocoded", "49.0, -123.0", "1.0",
         "=HYPERLINK(...)", "=HYPERLINK(...)", "49.001, -123.001", "yes"],
    ]

    merged = merge_preserved(rows, existing)

    by_id = {r["id"]: r for r in merged}
    assert by_id["row2"]["corrected_coords"] == "49.001, -123.001"
    assert by_id["row2"]["applied"] == "yes"
    assert by_id["row3"]["corrected_coords"] == ""  # untouched, no prior entry


def test_refresh_preserves_corrected_coords_through_build():
    trip = make_trip([[make_stop(2, "A")]])
    existing = [
        HEADER,
        ["row2", "1", "2", "A", "123 Main St", "geocoded", "49.0, -123.0", "",
         "=HYPERLINK(...)", "=HYPERLINK(...)", "49.5, -123.5", ""],
    ]
    ws = FakeWorksheet(existing_values=existing)

    report = build(trip, live=True, worksheet=ws)

    assert report.would_write is False
    assert ws.cleared is True
    written_rows = ws.updated_with[1:]
    corrected_col = HEADER.index("corrected_coords")
    assert written_rows[0][corrected_col] == "49.5, -123.5"


def test_dry_run_writes_nothing():
    trip = make_trip([[make_stop(2, "A")]])

    report = build(trip, live=False, worksheet=None)

    assert report.would_write is True
    assert report.row_count == 1
    assert report.days == [1]
