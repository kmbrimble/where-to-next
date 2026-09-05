"""Tests for etl/verify_pins.py — mocked Sheets client, no live calls."""
from __future__ import annotations

import pytest

from etl.models import Day, Stop, Trip, TripMeta
from etl.verify_pins import HEADER, build, compute_rows, merge_preserved


def make_stop(row_num, title, lat=49.0, lng=-123.0, resolved_from="geocoded", address="123 Main St", seq=None,
              has_real_id=True) -> Stop:
    return Stop(
        id=f"row{row_num}", seq=seq if seq is not None else row_num, title=title, kind="poi",
        timezone="America/Vancouver", how="drive", travel_minutes=10, dwell_minutes=10, timing="floating",
        row_num=row_num, lat=lat, lng=lng, resolved_from=resolved_from, address=address, has_real_id=has_real_id,
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
        self.batches = []

    def get_all_values(self):
        return self._values

    def clear(self):
        self.cleared = True

    def update(self, values, value_input_option=None):
        self.updated_with = values
        self._values = values

    def batch_update(self, updates):
        self.batches.append(updates)


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


def test_build_fails_loudly_on_synthetic_id():
    """A stop with no real sheet id can never be found by apply's fresh
    re-read — this must be caught here, not silently reach the user's sheet."""
    trip = make_trip([[make_stop(2, "A", has_real_id=False)]])

    with pytest.raises(RuntimeError, match="synthetic placeholder id"):
        build(trip, live=False, worksheet=None)


# --- apply step -------------------------------------------------------------

from etl.verify_pins import apply_corrections, parse_corrected_coords, validate_pin_rows  # noqa: E402

ITINERARY_HEADER = ["Plan", "Address", "id", "lat", "lng", "place_id", "resolved_from"]


def make_itinerary_ws(rows_by_id: dict[str, list[str]]):
    """rows_by_id: id -> [plan, address, id, lat, lng, place_id, resolved_from]."""
    return FakeWorksheet(existing_values=[ITINERARY_HEADER] + list(rows_by_id.values()))


def pin_row(id_, corrected_coords="", applied=""):
    return {"id": id_, "corrected_coords": corrected_coords, "applied": applied}


def test_pasted_url_rejected_with_clear_message():
    with pytest.raises(ValueError, match="Maps URL"):
        parse_corrected_coords("https://maps.app.goo.gl/xYz123")


def test_out_of_box_coordinates_rejected():
    trip = make_trip([[make_stop(2, "A")]])  # America/Vancouver box
    rows = [pin_row("row2", corrected_coords="0, 0")]

    candidates, rejected, skipped = validate_pin_rows(rows, trip)

    assert candidates == []
    assert any("outside the trip's bounding box" in r for r in rejected)


def test_one_bad_row_blocks_whole_batch():
    trip = make_trip([[make_stop(2, "A"), make_stop(3, "B")]])
    rows = [pin_row("row2", corrected_coords="49.5, -123.5"), pin_row("row3", corrected_coords="not-coords")]
    itinerary_ws = make_itinerary_ws({
        "row2": ["A", "old addr", "row2", "49.0", "-123.0", "pid2", "geocoded"],
        "row3": ["B", "old addr", "row3", "49.1", "-123.1", "pid3", "geocoded"],
    })

    report = apply_corrections(trip, rows, live=True, itinerary_ws=itinerary_ws, pin_ws=FakeWorksheet())

    assert report.aborted is True
    assert report.applied == 0
    assert itinerary_ws.batches == []


def test_already_applied_rows_skipped():
    trip = make_trip([[make_stop(2, "A")]])
    rows = [pin_row("row2", corrected_coords="49.5, -123.5", applied="2026-01-01T00:00:00Z")]

    candidates, rejected, skipped = validate_pin_rows(rows, trip)

    assert candidates == []
    assert rejected == []
    assert any("already applied" in s for s in skipped)


def test_dry_run_apply_writes_nothing():
    trip = make_trip([[make_stop(2, "A")]])
    rows = [pin_row("row2", corrected_coords="49.5, -123.5")]
    itinerary_ws = make_itinerary_ws({"row2": ["A", "old addr", "row2", "49.0", "-123.0", "pid2", "geocoded"]})

    report = apply_corrections(trip, rows, live=False, itinerary_ws=itinerary_ws, pin_ws=None)

    assert report.applied == 0
    assert itinerary_ws.batches == []
    assert len(report.would_apply) == 1


def test_address_written_and_lat_lng_place_id_cleared_on_success():
    trip = make_trip([[make_stop(2, "A")]])
    rows = [pin_row("row2", corrected_coords="49.5, -123.5")]
    itinerary_ws = make_itinerary_ws({"row2": ["A", "old addr", "row2", "49.0", "-123.0", "pid2", "geocoded"]})
    pin_ws = FakeWorksheet(existing_values=[HEADER, ["row2"] + [""] * (len(HEADER) - 1)])

    report = apply_corrections(trip, rows, live=True, itinerary_ws=itinerary_ws, pin_ws=pin_ws, now="2026-05-01T00:00:00Z")

    assert report.applied == 1
    assert report.aborted is False
    updates = {u["range"]: u["values"][0][0] for batch in itinerary_ws.batches for u in batch}
    assert updates["B2"] == "49.5, -123.5"  # Address column
    assert updates["D2"] == ""  # lat
    assert updates["E2"] == ""  # lng
    assert updates["F2"] == ""  # place_id
    pin_updates = {u["range"]: u["values"][0][0] for batch in pin_ws.batches for u in batch}
    applied_col_letter = chr(ord("A") + HEADER.index("applied"))
    assert pin_updates[f"{applied_col_letter}2"] == "2026-05-01T00:00:00Z"
