"""Tests for etl/legs.py — HTTP mocked, no live calls."""
from __future__ import annotations

from etl.geocode import RequestBudget
from etl.legs import compute_legs
from etl.models import Day, Stop, Trip, TripMeta
from etl.routes import RouteResult, RoutesClient


def make_stop(row_num: int, title: str, how: str, lat=49.6, lng=-123.1, travel_minutes=20, **overrides) -> Stop:
    fields = dict(
        id=f"row{row_num}", seq=row_num, title=title, kind="poi", timezone="America/Vancouver",
        how=how, travel_minutes=travel_minutes, dwell_minutes=10, timing="floating",
        row_num=row_num, lat=lat, lng=lng,
    )
    fields.update(overrides)
    return Stop(**fields)


def make_trip(stops: list[Stop]) -> Trip:
    return Trip(trip=TripMeta(), days=[Day(day=1, date="2026-09-27", timezone="America/Vancouver", stops=stops)])


def fake_client(result=None):
    calls = []

    def fetch(body):
        calls.append(body)
        return {"routes": [{
            "distanceMeters": result.distance_m if result else 1000,
            "duration": f"{result.duration_s if result else 600}s",
            "polyline": {"encodedPolyline": result.polyline if result else "xyz"},
        }]}

    return RoutesClient(api_key="fake-key", fetch=fetch), calls


def test_non_drive_modes_make_no_calls():
    # A stop's How is the mode for the leg ARRIVING at it — A itself has no
    # incoming leg (it's the day's first stop), so every leg here is B/C/D/E's
    # mode: train, shuttle, plane, transit. None of those call the Routes API.
    client, calls = fake_client()
    stops = [
        make_stop(2, "A", "drive"),
        make_stop(3, "B", "train"),
        make_stop(4, "C", "shuttle"),
        make_stop(5, "D", "plane"),
        make_stop(6, "E", "transit"),
    ]
    trip = make_trip(stops)

    report = compute_legs(trip, live=True, client=client, budget=RequestBudget())

    assert report.by_mode == {"train": 1, "shuttle": 1, "plane": 1, "transit": 1}
    assert client.call_count == 0
    assert calls == []
    assert len(trip.days[0].legs) == 4
    assert all(leg.duration_s is None and leg.polyline for leg in trip.days[0].legs)


def test_cache_hit_makes_no_call():
    client, calls = fake_client()
    stops = [make_stop(2, "A", "drive"), make_stop(3, "B", "drive")]
    trip = make_trip(stops)
    key_cache = {}

    # First call populates the cache.
    compute_legs(trip, live=True, client=client, budget=RequestBudget(), cache=key_cache)
    assert client.call_count == 1

    # Second run, fresh trip/client, same cache — must not call.
    client2, calls2 = fake_client()
    trip2 = make_trip([make_stop(2, "A", "drive"), make_stop(3, "B", "drive")])
    compute_legs(trip2, live=True, client=client2, budget=RequestBudget(), cache=key_cache)
    assert client2.call_count == 0
    assert calls2 == []


def test_dry_run_makes_zero_calls():
    client, calls = fake_client()
    stops = [make_stop(2, "A", "drive"), make_stop(3, "B", "drive")]
    trip = make_trip(stops)

    report = compute_legs(trip, live=False, client=client, budget=None)

    assert client.call_count == 0
    assert calls == []
    assert report.projected_calls == 1
    assert trip.days[0].legs == []


def test_budget_enforced_before_any_request():
    client, calls = fake_client()
    stops = [make_stop(n, f"S{n}", "drive") for n in range(2, 6)]  # 3 legs
    trip = make_trip(stops)
    budget = RequestBudget(limit=1)

    try:
        compute_legs(trip, live=True, client=client, budget=budget)
        raised = False
    except RuntimeError:
        raised = True

    assert raised
    assert calls == []


def test_missing_coordinates_skips_and_warns():
    stops = [make_stop(2, "A", "drive"), make_stop(3, "B", "drive", lat=None, lng=None)]
    trip = make_trip(stops)

    report = compute_legs(trip, live=False, client=None, budget=None)

    assert len(report.skipped_missing_coords) == 1
    assert any("missing coordinates" in w for w in report.warnings)
    assert report.projected_calls == 0


def test_25_percent_divergence_warning_fires():
    result = RouteResult(polyline="p", distance_m=1000, duration_s=1800)  # 30 min
    client, calls = fake_client(result)
    # sheet says 10 min (600s) — API says 1800s, way over 25% divergence
    stops = [make_stop(2, "A", "drive"), make_stop(3, "B", "drive", travel_minutes=10)]
    trip = make_trip(stops)

    report = compute_legs(trip, live=True, client=client, budget=RequestBudget())

    assert any("divergence" in w and "not overridden" in w for w in report.warnings)
    assert stops[1].travel_minutes == 10  # sheet value untouched


def test_no_divergence_warning_when_close():
    result = RouteResult(polyline="p", distance_m=1000, duration_s=1150)  # ~19min, close to 20
    client, calls = fake_client(result)
    stops = [make_stop(2, "A", "drive"), make_stop(3, "B", "drive", travel_minutes=20)]
    trip = make_trip(stops)

    report = compute_legs(trip, live=True, client=client, budget=RequestBudget())

    assert not any("divergence" in w for w in report.warnings)


def test_straight_line_leg_has_polyline_and_null_duration():
    stops = [make_stop(2, "A", "drive"), make_stop(3, "B", "plane")]
    trip = make_trip(stops)

    compute_legs(trip, live=False, client=None, budget=None)
    # dry run doesn't build legs for routed modes, but non-routed modes need no
    # call and should still be emitted even under dry run.
    leg = trip.days[0].legs[0]
    assert leg.how == "plane"
    assert leg.duration_s is None
    assert leg.polyline
