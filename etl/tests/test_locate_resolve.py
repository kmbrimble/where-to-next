"""Tests for etl/locate.py's resolve_locations orchestration. HTTP mocked, no live calls."""
from __future__ import annotations

from etl.geocode import GeocodeClient, RequestBudget
from etl.locate import resolve_locations
from etl.models import Day, Stop, Trip, TripMeta


def make_trip(stops: list[Stop]) -> Trip:
    day = Day(day=1, date="2026-09-27", timezone="America/Vancouver", stops=stops)
    return Trip(trip=TripMeta(), days=[day])


def make_stop(row_num: int, address: str = "", links: list[str] | None = None, **overrides) -> Stop:
    fields = dict(
        id=f"row{row_num}", seq=1, title="Somewhere", kind="poi", timezone="America/Vancouver",
        how="drive", travel_minutes=10, dwell_minutes=10, timing="floating",
        links=links or [], row_num=row_num, address=address,
    )
    fields.update(overrides)
    return Stop(**fields)


def test_dry_run_makes_zero_network_calls():
    def exploding_fetch(url):
        raise AssertionError("dry run must never call fetch")

    client = GeocodeClient(api_key="fake-key", fetch=exploding_fetch)
    stops = [
        make_stop(2, address="Shannon Falls, Squamish, BC"),
        make_stop(3, address="9535R9RG+9X"),
    ]
    trip = make_trip(stops)

    report = resolve_locations(trip, live=False, client=client, budget=None)

    assert client.call_count == 0
    assert report.projected_calls == 2
    assert report.actual_calls == 0
    # dry run must not fabricate values
    assert all(s.lat is None for s in stops)


def test_dry_run_self_resolves_coordinates_for_free_but_does_not_apply_them():
    stops = [make_stop(2, address="49.6725, -123.1583")]
    trip = make_trip(stops)

    report = resolve_locations(trip, live=False, client=None, budget=None)

    assert report.counts["coordinates"] == 1
    assert report.projected_calls == 0
    assert stops[0].lat is None  # not applied under dry run


def test_live_applies_coordinates_directly_no_call():
    stops = [make_stop(2, address="49.6725, -123.1583")]
    trip = make_trip(stops)

    report = resolve_locations(trip, live=True, client=None, budget=RequestBudget())

    assert stops[0].lat == 49.6725
    assert stops[0].resolved_from == "coordinates"
    assert report.actual_calls == 0


def test_live_respects_budget_before_any_request():
    calls = []

    def fetch(url):
        calls.append(url)
        return {"status": "OK", "results": [{
            "place_id": "x", "geometry": {"location": {"lat": 1, "lng": 1}, "location_type": "ROOFTOP"},
        }]}

    client = GeocodeClient(api_key="fake-key", fetch=fetch)
    stops = [make_stop(n, address=f"Address {n}") for n in range(2, 5)]  # 3 geocodable stops
    trip = make_trip(stops)
    budget = RequestBudget(limit=2)  # projected 3 > limit 2

    try:
        resolve_locations(trip, live=True, client=client, budget=budget)
        raised = False
    except RuntimeError:
        raised = True

    assert raised
    assert calls == []  # budget checked before any request


def test_live_geocodes_within_budget():
    def fetch(url):
        return {"status": "OK", "results": [{
            "place_id": "ChIJ1", "geometry": {"location": {"lat": 49.6, "lng": -123.1}, "location_type": "ROOFTOP"},
        }]}

    client = GeocodeClient(api_key="fake-key", fetch=fetch)
    stops = [make_stop(2, address="Shannon Falls, Squamish, BC")]
    trip = make_trip(stops)

    report = resolve_locations(trip, live=True, client=client, budget=RequestBudget())

    assert stops[0].lat == 49.6
    assert stops[0].place_id == "ChIJ1"
    assert stops[0].resolved_from == "geocoded"
    assert report.actual_calls == 1


def test_bounding_box_rejects_coordinates_outside_the_zone():
    # America/Vancouver box is roughly lat 48-60, lng -139..-114 — Sydney is nowhere
    # near it, so a coordinate pair claiming to be in Vancouver's timezone is bogus.
    stops = [make_stop(2, address="-33.8688, 151.2093")]  # Sydney, wrong hemisphere
    trip = make_trip(stops)

    report = resolve_locations(trip, live=True, client=None, budget=RequestBudget())

    assert any("bounding box" in e for e in report.errors)
    # rejected coordinates must not be applied
    assert stops[0].lat is None


def test_bounding_box_accepts_coordinates_inside_the_zone():
    stops = [make_stop(2, address="49.6725, -123.1583")]  # Vancouver, correct
    trip = make_trip(stops)

    report = resolve_locations(trip, live=True, client=None, budget=RequestBudget())

    assert report.errors == []
    assert stops[0].lat == 49.6725
