"""Tests for etl/daylight.py — pure local computation, no network."""
from __future__ import annotations

from etl.daylight import compute_daylight
from etl.models import Day, Stop, Trip, TripMeta


def make_stop(row_num: int, lat=None, lng=None, daylight_required=False, **overrides) -> Stop:
    fields = dict(
        id=f"row{row_num}", seq=1, title="Somewhere", kind="poi", timezone="America/Vancouver",
        how="drive", travel_minutes=10, dwell_minutes=10, timing="floating", row_num=row_num,
        lat=lat, lng=lng, daylight_required=daylight_required,
    )
    fields.update(overrides)
    return Stop(**fields)


def make_day(day_num: int, date: str, timezone: str, stops: list[Stop]) -> Day:
    return Day(day=day_num, date=date, timezone=timezone, stops=stops)


def test_known_coordinates_and_date_within_a_minute():
    # Vancouver, 2026-09-29 — cross-checked against astral itself (deterministic
    # library, not an external source): sunrise ~07:10, sunset ~18:54 local.
    stop = make_stop(2, lat=49.6725, lng=-123.1583)
    day = make_day(1, "2026-09-29", "America/Vancouver", [stop])
    trip = Trip(trip=TripMeta(), days=[day])

    report = compute_daylight(trip)

    assert report.warnings == []
    assert day.sunrise == "07:10"
    assert day.sunset == "18:54"


def test_single_location_day_is_unchanged_same_stop_both_endpoints():
    stop1 = make_stop(2, lat=49.6725, lng=-123.1583, title="Stanley Park")
    stop2 = make_stop(3, lat=49.68, lng=-123.16, title="Coal Harbour")  # same city, same tz
    day = make_day(1, "2026-09-29", "America/Vancouver", [stop1, stop2])
    trip = Trip(trip=TripMeta(), days=[day])

    report = compute_daylight(trip)

    assert not any("crosses a timezone" in w for w in report.warnings)
    assert "America/Vancouver" in day.sunrise_location
    assert "America/Vancouver" in day.sunset_location


def test_timezone_crossing_day_computes_each_endpoint_in_its_own_zone_and_warns():
    # Day 0 shape: first stop in Brisbane, last stop in Vancouver — sunrise must
    # come from Brisbane's clock, sunset from Vancouver's, not the day's single tz.
    first = make_stop(2, lat=-27.4698, lng=153.0251, timezone="Australia/Brisbane", title="Brisbane Airport")
    last = make_stop(3, lat=49.1967, lng=-123.1815, timezone="America/Vancouver", title="Vancouver Airport")
    day = make_day(0, "2026-09-27", "Australia/Brisbane", [first, last])
    trip = Trip(trip=TripMeta(), days=[day])

    report = compute_daylight(trip)

    # Brisbane sunrise on 2026-09-27 is early morning Brisbane time, nowhere near
    # what it would be if (wrongly) rendered on Vancouver's clock.
    assert day.sunrise is not None and day.sunset is not None
    brisbane_only_sunrise = _sun_time_direct(-27.4698, 153.0251, "2026-09-27", "Australia/Brisbane")
    vancouver_only_sunset = _sun_time_direct(49.1967, -123.1815, "2026-09-27", "America/Vancouver", "sunset")
    assert day.sunrise == brisbane_only_sunrise
    assert day.sunset == vancouver_only_sunset

    assert day.sunrise_location == "Brisbane Airport (Australia/Brisbane)"
    assert day.sunset_location == "Vancouver Airport (America/Vancouver)"
    assert any(
        "sunrise computed in Australia/Brisbane" in w and "sunset in America/Vancouver" in w
        for w in report.warnings
    )


def _sun_time_direct(lat, lng, iso_date, timezone, which="sunrise"):
    from etl.daylight import _sun_time
    return _sun_time(lat, lng, iso_date, timezone, which)


def test_first_stop_with_coordinates_is_sunrise_source_even_if_not_first_stop_overall():
    no_coords = make_stop(2, lat=None, lng=None)
    has_coords = make_stop(3, lat=49.6725, lng=-123.1583)
    day = make_day(1, "2026-09-29", "America/Vancouver", [no_coords, has_coords])
    trip = Trip(trip=TripMeta(), days=[day])

    report = compute_daylight(trip)

    assert report.warnings == []
    assert day.sunrise == "07:10"


def test_no_resolvable_coordinates_warns_and_leaves_null():
    stop = make_stop(2, lat=None, lng=None)
    day = make_day(1, "2026-09-29", "America/Vancouver", [stop])
    trip = Trip(trip=TripMeta(), days=[day])

    report = compute_daylight(trip)

    assert day.sunrise is None
    assert day.sunset is None
    assert day.sunrise_location is None
    assert day.sunset_location is None
    assert any("no stop has resolved coordinates" in w for w in report.warnings)


def test_daylight_required_stop_gets_its_own_sunset():
    day_stop = make_stop(2, lat=49.6725, lng=-123.1583)
    special_stop = make_stop(3, lat=50.1163, lng=-122.9574, daylight_required=True)
    day = make_day(1, "2026-09-29", "America/Vancouver", [day_stop, special_stop])
    trip = Trip(trip=TripMeta(), days=[day])

    compute_daylight(trip)

    assert special_stop.sunset is not None
    assert day_stop.sunset is None  # only daylight_required stops get their own


def test_daylight_required_stop_without_coordinates_warns():
    special_stop = make_stop(2, lat=None, lng=None, daylight_required=True)
    day = make_day(1, "2026-09-29", "America/Vancouver", [special_stop])
    trip = Trip(trip=TripMeta(), days=[day])

    report = compute_daylight(trip)

    assert special_stop.sunset is None
    assert any("daylight_required but no resolved coordinates" in w for w in report.warnings)


def test_astral_exception_path_is_caught_not_crashed():
    # High-latitude polar day/night case — astral raises ValueError rather than
    # returning None. Must be caught and turned into a warning, not a crash.
    stop = make_stop(2, lat=80.0, lng=0.0)
    day = make_day(1, "2026-06-21", "UTC", [stop])
    trip = Trip(trip=TripMeta(), days=[day])

    report = compute_daylight(trip)

    assert day.sunrise is None
    assert day.sunset is None
    assert any("never rises" in w or "never sets" in w for w in report.warnings)
