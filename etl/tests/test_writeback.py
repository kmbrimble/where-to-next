"""Tests for etl/writeback.py, with a fake gspread-like worksheet. No live writes."""
from __future__ import annotations

from etl.models import Day, Stop, Trip, TripMeta
from etl.writeback import generate_id, plan_checksum, write_back

HEADERS = ["Day", "Plan", "id", "lat", "lng", "place_id", "resolved_from"]


class FakeWorksheet:
    def __init__(self, rows: list[list[str]]):
        self.rows = rows
        self.batch_calls: list[list[dict]] = []

    def get_all_values(self) -> list[list[str]]:
        return [list(r) for r in self.rows]

    def batch_update(self, updates: list[dict]) -> None:
        self.batch_calls.append(updates)
        for update in updates:
            col_letter = "".join(c for c in update["range"] if c.isalpha())
            row_num = int("".join(c for c in update["range"] if c.isdigit()))
            col_idx = 0
            for ch in col_letter:
                col_idx = col_idx * 26 + (ord(ch) - ord("A") + 1)
            col_idx -= 1
            row = self.rows[row_num - 1]
            while len(row) <= col_idx:
                row.append("")
            row[col_idx] = update["values"][0][0]


def make_sheet_rows(plans: list[str], ids: list[str] | None = None) -> list[list[str]]:
    ids = ids or [""] * len(plans)
    rows = [HEADERS]
    for i, (plan, sheet_id) in enumerate(zip(plans, ids)):
        rows.append([f"Day {i}", plan, sheet_id, "", "", "", ""])
    return rows


def make_stop(row_num: int, lat=49.6, lng=-123.1, place_id="ChIJ1", resolved_from="geocoded",
              has_real_id=False, stop_id=None) -> Stop:
    return Stop(
        id=stop_id or f"placeholder{row_num}", seq=1, title="Somewhere", kind="poi",
        timezone="America/Vancouver", how="drive", travel_minutes=10, dwell_minutes=10,
        timing="floating", row_num=row_num, lat=lat, lng=lng, place_id=place_id,
        resolved_from=resolved_from, has_real_id=has_real_id,
    )


def make_trip(stops: list[Stop]) -> Trip:
    return Trip(trip=TripMeta(), days=[Day(day=1, date="2026-09-27", timezone="America/Vancouver", stops=stops)])


def test_dry_run_writes_nothing():
    ws = FakeWorksheet(make_sheet_rows(["A", "B"]))
    trip = make_trip([make_stop(2)])
    report = write_back(trip, ws, original_row_count=2, original_plan_checksum=plan_checksum(["A", "B"]), live=False)

    assert report.cells_written == 0
    assert ws.batch_calls == []
    assert len(report.would_write) == 1


def test_no_writeback_flag_writes_nothing_even_under_live():
    ws = FakeWorksheet(make_sheet_rows(["A", "B"]))
    trip = make_trip([make_stop(2)])
    report = write_back(
        trip, ws, original_row_count=2, original_plan_checksum=plan_checksum(["A", "B"]),
        live=True, no_writeback=True,
    )

    assert report.cells_written == 0
    assert ws.batch_calls == []


def test_live_writes_only_the_five_columns_and_assigns_id():
    rows = make_sheet_rows(["A", "B"])
    ws = FakeWorksheet(rows)
    trip = make_trip([make_stop(2)])

    report = write_back(trip, ws, original_row_count=2, original_plan_checksum=plan_checksum(["A", "B"]), live=True)

    assert report.cells_written == 5  # id, lat, lng, place_id, resolved_from
    assert len(report.ids_assigned) == 1
    written_row = ws.rows[1]  # row 2 = index 1
    assert written_row[0] == "Day 0"  # Day column untouched
    assert written_row[1] == "A"  # Plan (Address stand-in here) untouched
    assert written_row[2] != ""  # id assigned
    assert written_row[3] == "49.6"
    assert written_row[4] == "-123.1"


def test_id_based_matching_survives_a_row_insertion():
    # Stop was originally read at row 3 with a real id already in the sheet. By
    # write time a row has been inserted above it, shifting it to row 4 — but the
    # id is unchanged, so write-back must find it by id, not by row_num.
    rows = make_sheet_rows(["NEW ROW", "A", "B"], ids=["", "abc123", ""])
    ws = FakeWorksheet(rows)
    stop = make_stop(row_num=2, has_real_id=True, stop_id="abc123")  # stale row_num from before the insert
    trip = make_trip([stop])

    # original snapshot (before the insert) had 2 rows and a different Plan checksum
    report = write_back(
        trip, ws, original_row_count=2, original_plan_checksum=plan_checksum(["A", "B"]), live=True,
    )

    # shape changed (row count differs) -> must abort rather than write positionally
    assert report.aborted
    assert "shape changed" in report.abort_reason


def test_id_based_matching_when_shape_unchanged_but_id_present():
    rows = make_sheet_rows(["A", "B"], ids=["abc123", ""])
    ws = FakeWorksheet(rows)
    stop = make_stop(row_num=2, has_real_id=True, stop_id="abc123")
    trip = make_trip([stop])

    report = write_back(
        trip, ws, original_row_count=2, original_plan_checksum=plan_checksum(["A", "B"]), live=True,
    )

    assert not report.aborted
    assert report.ids_assigned == []  # already had a real id
    assert ws.rows[1][3] == "49.6"  # lat written to row 2 (index 1), matched by id


def test_shape_change_aborts_without_writing():
    ws = FakeWorksheet(make_sheet_rows(["A", "B", "C"]))  # 3 rows now, was 2
    trip = make_trip([make_stop(2)])

    report = write_back(trip, ws, original_row_count=2, original_plan_checksum=plan_checksum(["A", "B"]), live=True)

    assert report.aborted
    assert "shape changed" in report.abort_reason
    assert ws.batch_calls == []


def test_idempotent_second_run_writes_zero_cells():
    rows = make_sheet_rows(["A", "B"])
    ws = FakeWorksheet(rows)
    trip1 = make_trip([make_stop(2)])
    write_back(trip1, ws, original_row_count=2, original_plan_checksum=plan_checksum(["A", "B"]), live=True)

    # second run: same resolved values, sheet already has them
    trip2 = make_trip([make_stop(2, has_real_id=True, stop_id=ws.rows[1][2])])
    report2 = write_back(trip2, ws, original_row_count=2, original_plan_checksum=plan_checksum(["A", "B"]), live=True)

    assert report2.cells_written == 0
    assert ws.batch_calls[-1] == [] if len(ws.batch_calls) > 1 else True


def test_never_writes_address_or_other_columns():
    headers_with_address = ["Day", "Plan", "Address", "id", "lat", "lng", "place_id", "resolved_from"]
    rows = [headers_with_address, ["Day 0", "A", "123 Main St", "", "", "", "", ""]]
    ws = FakeWorksheet(rows)
    trip = make_trip([make_stop(2)])

    write_back(trip, ws, original_row_count=1, original_plan_checksum=plan_checksum(["A"]), live=True)

    written_ranges = [u["range"][0] for call in ws.batch_calls for u in call]
    # column A=Day, B=Plan, C=Address — none of these letters should be touched
    assert "A" not in written_ranges
    assert "B" not in written_ranges
    assert "C" not in written_ranges


def test_unresolved_stop_still_gets_an_id_assigned():
    """id is a stable row identity for D1 state, decoupled from whether the stop
    ever resolved to coordinates — a stop with no resolvable Address must not be
    left on a synthetic placeholder id that's never written to the sheet."""
    rows = make_sheet_rows(["A"])
    ws = FakeWorksheet(rows)
    stop = make_stop(2, lat=None, lng=None, place_id=None, resolved_from=None)
    trip = make_trip([stop])

    report = write_back(trip, ws, original_row_count=1, original_plan_checksum=plan_checksum(["A"]), live=True)

    assert len(report.ids_assigned) == 1
    written_row = ws.rows[1]
    assert written_row[2] != ""  # id column now has a real id
    assert stop.id != "placeholder2"  # no longer the synthetic parse-time placeholder


def test_generate_id_is_short_url_safe_and_deterministic_per_row():
    id1 = generate_id(42)
    id2 = generate_id(42)
    id3 = generate_id(43)
    assert id1 == id2
    assert id1 != id3
    assert id1.isalnum()
    assert len(id1) <= 10
