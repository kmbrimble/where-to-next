"""Unit tests against synthetic CSV fixtures. No real trip data — see docs/SCHEMA.md
section 10 for the damage patterns these reproduce.
"""
from __future__ import annotations

import csv
import io
import json

import pytest

from etl.__main__ import main
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


def test_day_with_no_constraint_is_a_warning_not_an_error(tmp_path):
    # Temporarily downgraded — see the comment in parse.py's flush_day(). The sheet
    # has no fixed_time column yet, so this would fail every real day by construction.
    rows = [
        day_header("Day 1", "2026-09-27", "Vancouver", "America/Vancouver"),
        stop("Stanley Park", "0:20", "1:00", "America/Vancouver", timing="floating"),
        day_end("Vancouver"),
    ]
    path = write_csv(tmp_path, rows)
    result = parse_rows(CsvLoader(path))

    assert result.trip is not None
    assert result.errors == []
    assert any("no constraint" in w for w in result.warnings)


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
    out_a, out_b = tmp_path / "a", tmp_path / "b"

    assert main(["--csv", str(path), "--out-dir", str(out_a)]) == 0
    assert main(["--csv", str(path), "--out-dir", str(out_b)]) == 0

    assert (out_a / "trip.json").read_bytes() == (out_b / "trip.json").read_bytes()


def test_enums_are_case_insensitive(tmp_path):
    rows = [
        r(row_type="DAY_HEADER", Day="Day 1", Date="2026-09-27", Location="Vancouver",
          Zone="America/Vancouver", fixed_time="07:00"),
        r(row_type="Stop", Plan="Stanley Park", Travel="0:20", **{"Fun Time": "1:00"},
          Zone="America/Vancouver", timing="FIXED", kind="POI", How="Drive", fixed_time="09:00"),
        r(row_type="Lodging", Plan="Listel Vancouver", Zone="America/Vancouver", fixed_time="15:00"),
        r(row_type="Day_End", Location="Vancouver"),
    ]
    path = write_csv(tmp_path, rows)
    result = parse_rows(CsvLoader(path))

    assert result.errors == []
    assert result.trip is not None
    assert result.trip.days[0].stops[0].kind == "poi"
    assert result.trip.days[0].stops[0].how == "drive"


def test_how_accepts_train_and_transit(tmp_path):
    rows = happy_path_rows()
    rows.append(stop("Take the train", "0:20", "0:00", "America/Vancouver", how="train"))
    rows.append(stop("Public transit leg", "0:15", "0:00", "America/Vancouver", how="transit"))
    path = write_csv(tmp_path, rows)
    result = parse_rows(CsvLoader(path))

    assert result.errors == []
    assert result.trip is not None
    hows = {s.title: s.how for day in result.trip.days for s in day.stops}
    assert hows["Take the train"] == "train"
    assert hows["Public transit leg"] == "transit"


def test_compound_how_values_are_aliased_to_transit(tmp_path):
    rows = happy_path_rows()
    rows.append(stop("Aquabus stop", "0:20", "0:00", "America/Vancouver", how="Walk + Aquabus"))
    rows.append(stop("Bus stop", "0:15", "0:00", "America/Vancouver", how="Bus & walk"))
    path = write_csv(tmp_path, rows)
    result = parse_rows(CsvLoader(path))

    assert result.errors == []
    assert result.trip is not None
    hows = {s.title: s.how for day in result.trip.days for s in day.stops}
    assert hows["Aquabus stop"] == "transit"
    assert hows["Bus stop"] == "transit"


def test_how_still_rejects_genuinely_unknown_values(tmp_path):
    rows = happy_path_rows()
    rows.append(stop("Mystery mode", "0:20", "0:00", "America/Vancouver", how="teleport"))
    path = write_csv(tmp_path, rows)
    result = parse_rows(CsvLoader(path))

    assert result.trip is None
    assert any("How" in e and "teleport" in e for e in result.errors)


def test_fixed_timing_without_fixed_time_is_an_error(tmp_path):
    rows = happy_path_rows()
    rows.append(stop("No time set", "0:10", "0:20", "America/Vancouver", timing="fixed", fixed_time=""))
    path = write_csv(tmp_path, rows)
    result = parse_rows(CsvLoader(path))

    assert result.trip is None
    assert any("fixed_time" in e and "required when timing=fixed" in e for e in result.errors)


def test_invalid_row_type_value_is_a_hard_error_unlike_empty(tmp_path):
    rows = happy_path_rows()
    rows.append(r(row_type="stopp", Plan="Typo'd row_type"))
    path = write_csv(tmp_path, rows)
    result = parse_rows(CsvLoader(path))

    assert result.trip is None
    assert any("stopp" in e for e in result.errors)
    assert result.skipped == 0  # not treated as skip-worthy like an empty row_type


def test_mix_of_marked_and_unmarked_rows_exits_zero_with_only_marked_rows(tmp_path):
    # Days 0-10 are marked up with row_type; everything after is not, yet — the
    # incremental-migration case docs/SCHEMA.md and the task both call out.
    marked = happy_path_rows()
    unmarked_days = [
        r(row_type="", Day="Day 3", Date="2026-09-29", Location="Revelstoke"),
        r(row_type="", Plan="Some unmarked future stop", Travel="0:30", **{"Fun Time": "1:00"}),
        r(row_type="", Location="Revelstoke"),
    ]
    path = write_csv(tmp_path, marked + unmarked_days)

    exit_code = main(["--csv", str(path), "--out-dir", str(tmp_path / "out")])
    assert exit_code == 0

    trip = json.loads((tmp_path / "out" / "trip.json").read_text())
    assert [d["day"] for d in trip["days"]] == [1, 2]

    report = (tmp_path / "out" / "report.md").read_text()
    assert "Errors (0)" in report
    assert report.count("empty row_type — skipped") == 3


def test_stray_date_on_non_header_row_is_a_warning(tmp_path):
    rows = happy_path_rows()
    rows[3] = r(row_type="lodging", Plan="Listel Vancouver", Zone="America/Vancouver",
                fixed_time="15:00", Date="Listel")
    path = write_csv(tmp_path, rows)
    result = parse_rows(CsvLoader(path))

    assert result.trip is not None
    assert any("unexpected value in Date column" in w and "Listel" in w for w in result.warnings)




def test_blank_kind_and_timing_default_and_do_not_error(tmp_path):
    rows = [
        day_header("Day 1", "2026-09-27", "Vancouver", "America/Vancouver"),
        r(row_type="stop", Plan="Stanley Park", Travel="0:20", **{"Fun Time": "1:00"},
          Zone="America/Vancouver", How="drive", kind="", timing=""),
        lodging("Listel Vancouver", "America/Vancouver"),
        day_end("Vancouver"),
    ]
    path = write_csv(tmp_path, rows)
    result = parse_rows(CsvLoader(path))

    assert result.errors == []
    assert result.trip is not None
    stop_out = result.trip.days[0].stops[0]
    assert stop_out.kind == "poi"
    assert stop_out.timing == "floating"


def test_kind_and_timing_columns_entirely_absent_from_sheet(tmp_path):
    headers = [h for h in HEADERS if h not in ("kind", "timing")]
    rows = [
        day_header("Day 1", "2026-09-27", "Vancouver", "America/Vancouver"),
        r(row_type="stop", Plan="Stanley Park", Travel="0:20", **{"Fun Time": "1:00"},
          Zone="America/Vancouver", How="drive"),
        lodging("Listel Vancouver", "America/Vancouver"),
        day_end("Vancouver"),
    ]
    path = write_csv(tmp_path, rows, headers=headers)
    result = parse_rows(CsvLoader(path))

    assert result.errors == []
    assert result.trip is not None
    stop_out = result.trip.days[0].stops[0]
    assert stop_out.kind == "poi"
    assert stop_out.timing == "floating"


def test_invalid_kind_or_timing_value_still_errors(tmp_path):
    rows = happy_path_rows()
    rows.append(stop("Bad kind", "0:10", "0:20", "America/Vancouver", kind="bogus"))
    path = write_csv(tmp_path, rows)
    result = parse_rows(CsvLoader(path))

    assert result.trip is None
    assert any("kind" in e and "bogus" in e for e in result.errors)


def test_plan_on_blank_row_is_a_misclassification_warning(tmp_path):
    rows = happy_path_rows()
    rows.append(r(row_type="blank", Plan="Stray stop title"))
    path = write_csv(tmp_path, rows)
    result = parse_rows(CsvLoader(path))

    assert result.trip is not None
    assert result.errors == []
    assert any(
        "row_type=blank but Plan is non-empty" in w and "Stray stop title" in w
        for w in result.warnings
    )


def test_plan_on_stop_lodging_or_day_header_row_does_not_warn(tmp_path):
    # sanity check: the misclassification warning is scoped to blank only now —
    # stop/lodging/day_header/day_end all legitimately carry Plan content.
    rows = happy_path_rows()
    path = write_csv(tmp_path, rows)
    result = parse_rows(CsvLoader(path))

    assert not any("possible misclassification" in w for w in result.warnings)


def test_leg_and_drive_total_are_no_longer_valid_row_types(tmp_path):
    # The sheet packs a real stop into these rows (Plan holds a stop, Location holds
    # a journey label or drive duration) — row_type modelled them as structural, which
    # was wrong. Both are now unrecognised values, same as any other typo.
    rows = happy_path_rows()
    rows.append(r(row_type="leg", Location="Vancouver to Whistler"))
    rows.append(r(row_type="drive_total", Location="6h 19m"))
    path = write_csv(tmp_path, rows)
    result = parse_rows(CsvLoader(path))

    assert result.trip is None
    assert any("row_type 'leg' not in" in e for e in result.errors)
    assert any("row_type 'drive_total' not in" in e for e in result.errors)


def test_stop_row_with_populated_location_parses_normally(tmp_path):
    # Location is dual-purpose in the sheet (journey label / drive duration on rows
    # that are now gone) — a stop row may still have Location filled in, and it must
    # be silently ignored, not warned or errored on.
    rows = happy_path_rows()
    rows.append(r(
        row_type="stop", Plan="Roadside viewpoint", Travel="0:15", **{"Fun Time": "0:10"},
        Zone="America/Vancouver", timing="floating", kind="poi", How="drive",
        Location="Sea to Sky Highway",
    ))
    path = write_csv(tmp_path, rows)
    result = parse_rows(CsvLoader(path))

    assert result.errors == []
    assert result.trip is not None
    assert not any("possible misclassification" in w for w in result.warnings)
    titles = [s.title for day in result.trip.days for s in day.stops]
    assert "Roadside viewpoint" in titles


def test_day_leg_is_derived_from_start_and_end_location(tmp_path):
    rows = [
        day_header("Day 1", "2026-09-27", "Vancouver", "America/Vancouver"),
        stop("Stanley Park", "0:20", "1:00", "America/Vancouver",
             timing="fixed", fixed_time="09:00"),
        day_end("Whistler"),
    ]
    path = write_csv(tmp_path, rows)
    result = parse_rows(CsvLoader(path))

    assert result.trip is not None
    assert result.trip.days[0].leg == "Vancouver to Whistler"


def test_day_leg_uses_single_location_when_start_equals_end(tmp_path):
    rows = [
        day_header("Day 1", "2026-09-27", "Vancouver", "America/Vancouver"),
        stop("Stanley Park", "0:20", "1:00", "America/Vancouver",
             timing="fixed", fixed_time="09:00"),
        day_end("Vancouver"),
    ]
    path = write_csv(tmp_path, rows)
    result = parse_rows(CsvLoader(path))

    assert result.trip is not None
    assert result.trip.days[0].leg == "Vancouver"


def test_day_header_with_plan_emits_a_first_stop(tmp_path):
    # Real pattern from the live sheet (row 90): a day_header row's single fixed_time
    # cell serves as both the day's anchor_time and the embedded stop's fixed_time —
    # they're the same moment (the day begins with this fixed-time stop), not two
    # different values competing for one cell.
    rows = [
        r(row_type="day_header", Day="Day 1", Date="2026-09-27", Location="Vancouver",
          Zone="America/Vancouver", fixed_time="06:40", timing="fixed",
          Plan="Skyline Viewpoint", Travel="0:10", **{"Fun Time": "1:15"}, How="drive"),
        lodging("Listel Vancouver", "America/Vancouver"),
        day_end("Vancouver"),
    ]
    path = write_csv(tmp_path, rows)
    result = parse_rows(CsvLoader(path))

    assert result.errors == []
    assert result.trip is not None
    day = result.trip.days[0]
    assert day.anchor_time == "06:40"
    assert len(day.stops) == 1
    first_stop = day.stops[0]
    assert first_stop.title == "Skyline Viewpoint"
    assert first_stop.seq == 1
    assert first_stop.timing == "fixed"
    assert first_stop.fixed_time == "06:40"
    assert first_stop.travel_minutes == 10
    assert first_stop.dwell_minutes == 75


def test_day_header_with_empty_plan_behaves_as_before(tmp_path):
    rows = happy_path_rows()
    path = write_csv(tmp_path, rows)
    result = parse_rows(CsvLoader(path))

    assert result.trip is not None
    # day_header row itself contributes no stop when Plan is empty
    assert result.trip.days[0].stops[0].title == "Stanley Park"


def test_day_end_with_plan_emits_a_final_stop(tmp_path):
    rows = [
        day_header("Day 1", "2026-09-27", "Vancouver", "America/Vancouver"),
        stop("Stanley Park", "0:20", "1:00", "America/Vancouver",
             timing="fixed", fixed_time="09:00"),
        r(row_type="day_end", Location="Whistler", Plan="Arrive Whistler Village",
          Travel="0:30", **{"Fun Time": "0:00"}, Zone="America/Vancouver",
          timing="floating", kind="poi", How="drive"),
    ]
    path = write_csv(tmp_path, rows)
    result = parse_rows(CsvLoader(path))

    assert result.errors == []
    assert result.trip is not None
    day = result.trip.days[0]
    assert day.end_location == "Whistler"
    assert len(day.stops) == 2
    assert day.stops[-1].title == "Arrive Whistler Village"
    assert day.stops[-1].seq == 2


def test_day_end_with_incomplete_plan_errors_like_any_stop(tmp_path):
    # Once day_end carries a Plan it's held to the same stop-shaped requirements as
    # any other stop row — Travel/Fun Time/Zone missing is now a real error, not
    # silently ignored.
    rows = [
        day_header("Day 1", "2026-09-27", "Vancouver", "America/Vancouver"),
        stop("Stanley Park", "0:20", "1:00", "America/Vancouver",
             timing="fixed", fixed_time="09:00"),
        r(row_type="day_end", Location="Whistler", Plan="Arrive Whistler Village"),
    ]
    path = write_csv(tmp_path, rows)
    result = parse_rows(CsvLoader(path))

    assert result.trip is None
    assert any("stop missing/invalid required field(s)" in e for e in result.errors)
