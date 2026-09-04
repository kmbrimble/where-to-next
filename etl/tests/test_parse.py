"""Unit tests against synthetic CSV fixtures. No real trip data — see docs/SCHEMA.md
section 10 for the damage patterns these reproduce.
"""
from __future__ import annotations

import csv
import io
import json

from etl.loaders import CsvLoader
from etl.parse import parse_rows
from etl.report import render_report

HEADERS = [
    "Day", "Date", "Location", "Travel", "Fun Time", "Plan", "Address", "How",
    "Zone", "Price", "Notes", "Links", "row_type", "kind", "timing",
    "fixed_time", "arrive_before", "daylight_required", "day_offset", "documents",
]


def make_csv(rows: list[dict[str, str]], headers: list[str] = HEADERS) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=headers)
    writer.writeheader()
    for r in rows:
        writer.writerow({h: r.get(h, "") for h in headers})
    return buf.getvalue()


def r(**kw) -> dict[str, str]:
    return kw


def day_header(day: str, date: str, location: str, zone: str, anchor: str = "07:00") -> dict:
    return r(row_type="day_header", Day=day, Date=date, Location=location, Zone=zone, fixed_time=anchor)


def stop(plan: str, travel: str, fun: str, zone: str, timing: str = "floating",
         kind: str = "poi", how: str = "drive", fixed_time: str = "") -> dict:
    return r(
        row_type="stop", Plan=plan, Travel=travel, **{"Fun Time": fun},
        Zone=zone, timing=timing, kind=kind, How=how, fixed_time=fixed_time,
    )


def lodging(name: str, zone: str, check_in: str = "15:00") -> dict:
    return r(row_type="lodging", Plan=name, Zone=zone, fixed_time=check_in)


def day_end(location: str) -> dict:
    return r(row_type="day_end", Location=location)


def write_csv(tmp_path, rows: list[dict], headers: list[str] = HEADERS):
    path = tmp_path / "itinerary.csv"
    path.write_text(make_csv(rows, headers), encoding="utf-8")
    return path


def happy_path_rows() -> list[dict]:
    return [
        day_header("Day 1", "2026-09-27", "Vancouver", "America/Vancouver"),
        stop("Stanley Park", "0:20", "1:00", "America/Vancouver"),
        stop("Hotel Check-in Tour", "0:10", "0:30", "America/Vancouver",
             timing="fixed", kind="activity", fixed_time="16:30"),
        lodging("Listel Vancouver", "America/Vancouver"),
        day_end("Vancouver"),
        day_header("Day 2", "2026-09-28", "Vancouver", "America/Vancouver"),
        stop("Whistler Drive", "2:00", "0:00", "America/Vancouver"),
        lodging("Listel Whistler", "America/Vancouver"),
        day_end("Whistler"),
    ]


def test_happy_path_parses_with_no_errors(tmp_path):
    path = write_csv(tmp_path, happy_path_rows())
    result = parse_rows(CsvLoader(path))

    assert result.errors == []
    assert result.trip is not None
    assert [d.day for d in result.trip.days] == [1, 2]
    assert result.trip.days[0].stops[0].dwell_minutes == 60
    assert result.trip.days[0].stops[0].travel_minutes == 20
    assert result.trip.days[0].lodging.name == "Listel Vancouver"


def test_missing_required_header_is_a_hard_error(tmp_path):
    headers = [h for h in HEADERS if h != "Travel"]
    path = write_csv(tmp_path, happy_path_rows(), headers=headers)
    result = parse_rows(CsvLoader(path))

    assert result.trip is None
    assert any("travel" in e.lower() for e in result.errors)


def test_empty_row_type_is_a_warning_not_an_error(tmp_path):
    rows = happy_path_rows() + [r(row_type="", Plan="Stray phantom row")]
    path = write_csv(tmp_path, rows)
    result = parse_rows(CsvLoader(path))

    assert result.trip is not None
    assert result.skipped == 1
    assert any("empty row_type" in w for w in result.warnings)
    assert not any("row_type" in e and "empty" in e for e in result.errors)


def test_phantom_row_block_all_skipped_as_warnings(tmp_path):
    phantom_rows = [r(row_type="", Location="Arrive") for _ in range(5)]
    rows = happy_path_rows() + phantom_rows
    path = write_csv(tmp_path, rows)
    result = parse_rows(CsvLoader(path))

    assert result.trip is not None
    assert result.skipped == 5
    assert result.errors == []


def test_text_time_1_00pm_is_rejected(tmp_path):
    rows = happy_path_rows()
    rows.append(stop("Day 0 stop", "1.00pm", "0:30", "America/Vancouver"))
    rows[0] = day_header("Day 1", "2026-09-27", "Vancouver", "America/Vancouver")
    path = write_csv(tmp_path, rows)
    result = parse_rows(CsvLoader(path))

    assert result.trip is None
    assert any("Travel" in e and "1.00pm" in e for e in result.errors)


def test_prose_in_fun_time_is_rejected(tmp_path):
    rows = happy_path_rows()
    rows.append(stop("Zoo maybe", "0:20", "Zoo??", "America/Vancouver"))
    path = write_csv(tmp_path, rows)
    result = parse_rows(CsvLoader(path))

    assert result.trip is None
    assert any("Fun Time" in e and "Zoo??" in e for e in result.errors)


def test_prose_stanley_park_question_mark_is_rejected(tmp_path):
    rows = happy_path_rows()
    rows.append(stop("Somewhere", "0:20", "Stanley park?", "America/Vancouver"))
    path = write_csv(tmp_path, rows)
    result = parse_rows(CsvLoader(path))

    assert result.trip is None
    assert any("Fun Time" in e and "Stanley park?" in e for e in result.errors)


def test_hotel_name_in_date_column(tmp_path):
    rows = happy_path_rows()
    rows[0] = day_header("Day 1", "Listel", "Vancouver", "America/Vancouver")
    path = write_csv(tmp_path, rows)
    result = parse_rows(CsvLoader(path))

    assert result.trip is None
    assert any("Date" in e and "Listel" in e for e in result.errors)


def test_row_shifted_one_column_right(tmp_path):
    # Row 179 pattern: Plan=06:00, Price=Stanley Park, Notes=Yes, Links=$18 parking —
    # the actual duration cells end up holding shifted-in prose instead of durations.
    rows = happy_path_rows()
    shifted = r(
        row_type="stop", Plan="06:00", Travel="Stanley Park", **{"Fun Time": "Yes"},
        Zone="America/Vancouver", timing="floating", kind="poi", How="drive",
        Price="$18 parking",
    )
    rows.append(shifted)
    path = write_csv(tmp_path, rows)
    result = parse_rows(CsvLoader(path))

    assert result.trip is None
    assert any("Travel" in e and "Stanley Park" in e for e in result.errors)
    assert any("Fun Time" in e and "Yes" in e for e in result.errors)


def test_day_with_no_constraint_is_an_error(tmp_path):
    rows = [
        day_header("Day 1", "2026-09-27", "Vancouver", "America/Vancouver"),
        stop("Stanley Park", "0:20", "1:00", "America/Vancouver", timing="floating"),
        day_end("Vancouver"),
    ]
    path = write_csv(tmp_path, rows)
    result = parse_rows(CsvLoader(path))

    assert result.trip is None
    assert any("no constraint" in e for e in result.errors)


def test_invalid_timezone_is_rejected(tmp_path):
    rows = happy_path_rows()
    rows[0] = day_header("Day 1", "2026-09-27", "Vancouver", "America/Vancouver")
    rows.append(stop("Somewhere", "0:20", "0:30", "Mars/Colony"))
    path = write_csv(tmp_path, rows)
    result = parse_rows(CsvLoader(path))

    assert result.trip is None
    assert any("Zone" in e and "Mars/Colony" in e for e in result.errors)


def test_day_numbers_not_contiguous(tmp_path):
    rows = happy_path_rows()
    rows[5] = day_header("Day 3", "2026-09-28", "Vancouver", "America/Vancouver")
    path = write_csv(tmp_path, rows)
    result = parse_rows(CsvLoader(path))

    assert result.trip is None
    assert any("not contiguous" in e for e in result.errors)


def test_unrecognised_header_is_ignored_silently(tmp_path):
    headers = HEADERS + ["Some Random Column"]
    rows = happy_path_rows()
    for row in rows:
        row["Some Random Column"] = "whatever"
    path = write_csv(tmp_path, rows, headers=headers)
    result = parse_rows(CsvLoader(path))

    assert result.trip is not None
    assert result.errors == []


def test_report_renders_counts_errors_and_warnings(tmp_path):
    rows = happy_path_rows() + [r(row_type="", Location="phantom")]
    path = write_csv(tmp_path, rows)
    result = parse_rows(CsvLoader(path))
    report = render_report(result)

    assert "Rows parsed by type" in report
    assert "day_header: 2" in report
    assert "Rows skipped (1)" in report
    assert "Errors (0)" in report
    assert "Warnings" in report


def test_deterministic_output_same_bytes_twice(tmp_path):
    path = write_csv(tmp_path, happy_path_rows())
    source = CsvLoader(path)

    result_a = parse_rows(source)
    result_b = parse_rows(source)

    json_a = json.dumps(result_a.trip.model_dump(mode="json", by_alias=True), sort_keys=True)
    json_b = json.dumps(result_b.trip.model_dump(mode="json", by_alias=True), sort_keys=True)

    assert json_a == json_b
