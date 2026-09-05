"""Tests for etl/locate.py's resolve_locations orchestration. HTTP mocked, no live calls."""
from __future__ import annotations

from etl.geocode import GeocodeClient, RequestBudget, ShortLinkResolver
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
        make_stop(3, address="4VMF+42 Whistler, British Columbia, Canada"),  # compound -> needs a call
        make_stop(4, address="9535R9RG+9X"),  # global -> decodes offline, no call
    ]
    trip = make_trip(stops)

    report = resolve_locations(trip, live=False, client=client, budget=None)

    assert client.call_count == 0
    assert report.projected_calls == 2  # address string + compound code; global code is free
    assert report.plus_code_global == 1
    assert report.plus_code_compound == 1
    assert report.actual_calls == 0
    # dry run must not fabricate values, even for the free-to-resolve global code
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
    # Out-of-bounds is a WARNING (discard + mark unresolved), not a build-gating error.
    stops = [make_stop(2, address="-33.8688, 151.2093")]  # Sydney, wrong hemisphere
    trip = make_trip(stops)

    report = resolve_locations(trip, live=True, client=None, budget=RequestBudget())

    assert report.errors == []
    assert any("bounding box" in w and "km" in w for w in report.warnings)
    assert report.counts["unresolved"] == 1
    # rejected coordinates must not be applied, and the stop is marked unresolved
    assert stops[0].lat is None
    assert stops[0].resolved_from == "unresolved"


def test_bounding_box_accepts_coordinates_inside_the_zone():
    stops = [make_stop(2, address="49.6725, -123.1583")]  # Vancouver, correct
    trip = make_trip(stops)

    report = resolve_locations(trip, live=True, client=None, budget=RequestBudget())

    assert report.errors == []
    assert stops[0].lat == 49.6725


def test_compound_plus_code_geometric_center_is_not_flagged_as_low_precision():
    # A geocoded plus code always decodes to GEOMETRIC_CENTER — that's correct
    # behaviour for the format, not low confidence, so it must not be flagged.
    # (A global code wouldn't hit this path at all — it decodes offline, no geocode.)
    def fetch(url):
        return {"status": "OK", "results": [{
            "place_id": "ChIJpc", "geometry": {
                "location": {"lat": 50.13, "lng": -123.13}, "location_type": "GEOMETRIC_CENTER",
            },
        }]}

    client = GeocodeClient(api_key="fake-key", fetch=fetch)
    stops = [make_stop(48, address="4VMF+42 Whistler, British Columbia, Canada")]
    trip = make_trip(stops)

    report = resolve_locations(trip, live=True, client=client, budget=RequestBudget())

    assert client.call_count == 1  # compound code still needs the call
    assert report.geometric_center == []
    assert report.approximate == []
    assert not any("precision" in w for w in report.warnings)
    assert stops[0].lat == 50.13  # still resolved, just not flagged


def test_global_plus_code_resolves_offline_with_zero_calls_under_live():
    client = GeocodeClient(api_key="fake-key", fetch=lambda url: (_ for _ in ()).throw(
        AssertionError("global plus code must not call the geocoder")
    ))
    stops = [make_stop(48, address="9535R9RG+9X")]
    trip = make_trip(stops)

    report = resolve_locations(trip, live=True, client=client, budget=RequestBudget())

    assert client.call_count == 0
    assert report.actual_calls == 0
    assert stops[0].resolved_from == "plus_code"
    assert stops[0].lat is not None


def test_geocoded_low_precision_sorted_into_separate_lists():
    calls = iter([
        {"status": "OK", "results": [{
            "place_id": "ChIJ1", "geometry": {"location": {"lat": 49.1, "lng": -123.2}, "location_type": "APPROXIMATE"},
        }]},
        {"status": "OK", "results": [{
            "place_id": "ChIJ2", "geometry": {"location": {"lat": 49.2, "lng": -123.3}, "location_type": "GEOMETRIC_CENTER"},
        }]},
    ])
    client = GeocodeClient(api_key="fake-key", fetch=lambda url: next(calls))
    stops = [
        make_stop(2, address="Vague Place", title="Dinner"),
        make_stop(3, address="Some Park", title="Some Park"),
    ]
    trip = make_trip(stops)

    report = resolve_locations(trip, live=True, client=client, budget=RequestBudget())

    assert any("Dinner" in e for e in report.approximate)
    assert any("Some Park" in e for e in report.geometric_center)
    assert not any("Dinner" in e for e in report.geometric_center)
    assert not any("Some Park" in e for e in report.approximate)


def test_short_link_with_no_address_is_unresolved_and_tells_user_to_run_expand_links():
    # The main ETL never follows short links (Google 429-rate-limits rapid
    # redirect follows) — that's etl/expand_links.py's job, run separately.
    stops = [make_stop(2, links=["https://maps.app.goo.gl/abc123"])]
    trip = make_trip(stops)

    report = resolve_locations(trip, live=True, client=None, budget=RequestBudget())

    assert stops[0].lat is None
    assert report.counts["unresolved"] == 1
    assert any("expand_links" in w for w in report.warnings)


def test_dry_run_short_link_makes_no_network_call_and_needs_no_resolver():
    stops = [make_stop(2, links=["https://maps.app.goo.gl/abc123"])]
    trip = make_trip(stops)

    report = resolve_locations(trip, live=False, client=None, budget=None)

    assert stops[0].lat is None
    assert report.projected_calls == 0  # nothing to geocode — no Address, no cache
    assert any("expand_links" in w for w in report.warnings)


def test_short_link_falls_back_to_geocoding_address_not_unresolved():
    # A short link is present but never followed; if there's a usable Address,
    # it must fall through to geocoding it rather than giving up.
    def fetch(url):
        return {"status": "OK", "results": [{
            "place_id": "ChIJfallback", "geometry": {
                "location": {"lat": 49.65, "lng": -123.2}, "location_type": "ROOFTOP"},
        }]}

    client = GeocodeClient(api_key="fake-key", fetch=fetch)
    notes = "https://maps.app.goo.gl/deadlink123"
    stops = [make_stop(2, address="Shannon Falls, Squamish, BC", notes=notes)]
    trip = make_trip(stops)

    report = resolve_locations(trip, live=True, client=client, budget=RequestBudget())

    assert stops[0].resolved_from == "geocoded"
    assert stops[0].place_id == "ChIJfallback"
    assert stops[0].lat == 49.65
    assert report.counts["unresolved"] == 0
    assert any("expand_links" in w for w in report.warnings)


def test_cached_compound_plus_code_makes_no_call():
    client = GeocodeClient(api_key="fake-key", fetch=lambda url: (_ for _ in ()).throw(
        AssertionError("cached compound plus code must not call the geocoder")
    ))
    stops = [make_stop(
        2, address="4VMF+42 Whistler, British Columbia, Canada",
        lat=50.13, lng=-122.95, place_id="ChIJcached", resolved_from="plus_code", has_real_id=True,
    )]
    trip = make_trip(stops)

    report = resolve_locations(trip, live=True, client=client, budget=RequestBudget())

    assert client.call_count == 0
    assert report.counts["cached"] == 1
    assert stops[0].lat == 50.13  # unchanged


def test_cached_short_link_stop_makes_no_call():
    stops = [make_stop(
        2, links=["https://maps.app.goo.gl/abc123"],
        lat=49.6, lng=-123.1, place_id="ChIJcached", resolved_from="maps_link", has_real_id=True,
    )]
    trip = make_trip(stops)

    report = resolve_locations(trip, live=True, client=None, budget=RequestBudget())

    assert report.counts["cached"] == 1
    assert stops[0].lat == 49.6  # unchanged


def test_reverify_forces_full_re_resolution_ignoring_cache():
    def fetch(url):
        return {"status": "OK", "results": [{
            "place_id": "ChIJnew", "geometry": {"location": {"lat": 51.0, "lng": -117.0}, "location_type": "ROOFTOP"},
        }]}

    client = GeocodeClient(api_key="fake-key", fetch=fetch)
    stops = [make_stop(
        2, address="4VMF+42 Whistler, British Columbia, Canada",
        lat=50.13, lng=-122.95, place_id="ChIJold", resolved_from="plus_code", has_real_id=True,
    )]
    trip = make_trip(stops)

    report = resolve_locations(trip, live=True, client=client, budget=RequestBudget(), reverify=True)

    assert client.call_count == 1
    assert stops[0].place_id == "ChIJnew"
    assert stops[0].lat == 51.0


def test_cleared_cache_still_triggers_resolution():
    # Clearing lat/lng in the sheet is the documented correction mechanism — it
    # must still force re-resolution even with reverify=False (the default).
    stops = [make_stop(2, address="49.6725, -123.1583", lat=None, lng=None, resolved_from=None)]
    trip = make_trip(stops)

    report = resolve_locations(trip, live=True, client=None, budget=RequestBudget())

    assert stops[0].lat == 49.6725
    assert report.counts["coordinates"] == 1
    assert report.counts["cached"] == 0


def test_cache_disagreement_on_global_plus_code_still_warns_and_overwrites():
    # Offline-decodable sources (coordinates, global plus codes) cost nothing to
    # verify, so disagreement is still caught even though the trust-cache policy
    # otherwise skips re-verification for compound codes / addresses / Maps links.
    stops = [make_stop(
        2, address="9535R9RG+9X", lat=1.0, lng=1.0, place_id="ChIJold",
        resolved_from="plus_code", has_real_id=True,
    )]
    trip = make_trip(stops)

    report = resolve_locations(trip, live=True, client=None, budget=RequestBudget())

    assert stops[0].lat != 1.0
    assert stops[0].resolved_from == "plus_code"
    assert any("disagreed" in w for w in report.warnings)
