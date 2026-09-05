"""Tests for etl/locate.py's decide_resolution policy — pure, no network."""
from __future__ import annotations

from etl.locate import decide_resolution


def test_coordinates_no_cache_resolves_locally_no_call():
    plan = decide_resolution("51.23087, -115.4957", [], None, None, None)
    assert plan.action == "resolve_coordinates"
    assert plan.lat == 51.23087
    assert plan.lng == -115.4957


def test_coordinates_cache_agrees_uses_cache():
    plan = decide_resolution("51.23087, -115.4957", [], 51.23087, -115.4957, "ChIJexisting")
    assert plan.action == "use_cache"
    assert plan.place_id == "ChIJexisting"


def test_coordinates_cache_disagrees_overwrites_and_warns():
    # The cache-disagreement case: Address is deterministic, cache is stale/wrong.
    plan = decide_resolution("51.23087, -115.4957", [], 40.0, -100.0, "ChIJold")
    assert plan.action == "overwrite_cache_coordinates"
    assert plan.lat == 51.23087
    assert plan.lng == -115.4957
    assert plan.warning is not None
    assert "disagreed" in plan.warning


def test_plus_code_always_resolves_regardless_of_cache():
    plan_no_cache = decide_resolution("9535R9RG+9X", [], None, None, None)
    plan_with_cache = decide_resolution("9535R9RG+9X", [], 51.0, -115.0, "ChIJcached")
    assert plan_no_cache.action == "resolve_plus_code"
    assert plan_with_cache.action == "resolve_plus_code"
    assert plan_no_cache.query == "9535R9RG+9X"


def test_address_string_with_cache_uses_cache_no_call():
    plan = decide_resolution("Shannon Falls, Squamish, BC", [], 49.6, -123.1, "ChIJcached")
    assert plan.action == "use_cache"
    assert plan.lat == 49.6
    assert plan.place_id == "ChIJcached"


def test_address_string_no_cache_resolves():
    plan = decide_resolution("Shannon Falls, Squamish, BC", [], None, None, None)
    assert plan.action == "resolve_address"
    assert plan.query == "Shannon Falls, Squamish, BC"


def test_empty_address_with_long_maps_link_resolves_from_link():
    link = "https://www.google.com/maps/place/X/@49.6725,-123.1583,15z"
    plan = decide_resolution("", [link], None, None, None)
    assert plan.action == "resolve_maps_link"
    assert plan.lat == 49.6725
    assert plan.lng == -123.1583
    assert plan.warning is not None


def test_empty_address_with_only_short_link_defers_to_redirect_follow():
    # No longer immediately unresolvable — a short link needs a redirect follow
    # (network, live-only), deferred via resolve_short_link rather than given up on.
    plan = decide_resolution("", ["https://maps.app.goo.gl/xYz123"], None, None, None)
    assert plan.action == "resolve_short_link"
    assert plan.query == "https://maps.app.goo.gl/xYz123"


def test_empty_address_no_links_is_unresolvable():
    plan = decide_resolution("", [], None, None, None)
    assert plan.action == "unresolvable"


def test_whitespace_only_address_treated_as_empty():
    plan = decide_resolution("   ", [], None, None, None)
    assert plan.action == "unresolvable"


def test_maps_url_in_notes_is_mined():
    notes = "Great spot, see https://www.google.com/maps/@49.6725,-123.1583,15z for the pin."
    plan = decide_resolution("", [], None, None, None, notes=notes)
    assert plan.action == "resolve_maps_link"
    assert plan.lat == 49.6725
    assert plan.lng == -123.1583


def test_maps_link_outranks_address_string_geocoding():
    # THE precedence test: an address string is present (would normally geocode),
    # but a Notes Maps link also yields coordinates. The link must win. Under the
    # OLD ordering (maps link only consulted when Address is empty), this would
    # have returned "resolve_address" instead — this test fails under that code.
    notes = "Pin: https://www.google.com/maps/@49.6725,-123.1583,15z"
    plan = decide_resolution("Shannon Falls, Squamish, BC", [], None, None, None, notes=notes)
    assert plan.action == "resolve_maps_link"
    assert plan.lat == 49.6725
    assert plan.lng == -123.1583


def test_maps_link_outranks_populated_cache_for_address_string():
    notes = "https://www.google.com/maps/@49.6725,-123.1583,15z"
    plan = decide_resolution("Shannon Falls, Squamish, BC", [], 40.0, -100.0, "ChIJold", notes=notes)
    assert plan.action == "resolve_maps_link"
    assert plan.lat == 49.6725


def test_multiple_urls_in_one_cell_prefers_first_usable_and_notes_the_rest():
    links = [
        "See https://maps.app.goo.gl/short1 or "
        "https://www.google.com/maps/@50.0,-120.0,15z or "
        "https://www.google.com/maps/@51.0,-121.0,15z"
    ]
    plan = decide_resolution("", links, None, None, None)
    assert plan.action == "resolve_maps_link"
    assert plan.lat == 50.0
    assert plan.lng == -120.0
    # the short link and the second long link are both noted as unused
    assert len(plan.alt_urls) == 2
