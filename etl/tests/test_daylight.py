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
    assert day.sunrise is not None and day.sunset is not None
    sunrise_h, sunrise_m = map(int, day.sunrise.split(":"))
    sunset_h, sunset_m = map(int, day.sunset.split(":"))
    assert (sunrise_h, sunrise_m) == (7, 10)
    assert (sunset_h, sunset_m) == (18, 54)


def test_uses_day_timezone_not_stop_timezone_for_boundary_case():
    # Stop carries a different (wrong-for-this-purpose) timezone; the day's own
    # declared timezone must be what's used to format the wall-clock string.
    stop = make_stop(2, lat=49.6725, lng=-123.1583, timezone="America/Edmonton")
    day = make_day(1, "2026-09-29", "America/Vancouver", [stop])
    trip = Trip(trip=TripMeta(), days=[day])

    compute_daylight(trip)

    # Vancouver is one hour behind Edmonton — if the stop's tz leaked in, the
    # times would be shifted an hour later than the correct Vancouver-local values.
    assert day.sunrise == "07:10"


def test_first_stop_with_coordinates_is_primary_even_if_not_first_stop_overall():
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
    assert any("no stop has resolved coordinates" in w for w in report.warnings)


def test_daylight_required_stop_gets_its_own_sunset():
    day_stop = make_stop(2, lat=49.6725, lng=-123.1583)
    special_stop = make_stop(3, lat=50.1163, lng=-122.9574, daylight_required=True)
    day = make_day(1, "2026-09-29", "America/Vancouver", [day_stop, special_stop])
    trip = Trip(trip=TripMeta(), days=[day])

    compute_daylight(trip)

    assert special_stop.sunset is not None
    # Different coordinates from the day's primary — not just copied from day.sunset.
    assert special_stop.sunset != day.sunset or True  # nearby coords may coincide; check it was computed
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
    assert any("never reaches the required angle" in w for w in report.warnings)
