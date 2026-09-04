"""Tests for etl/locate.py's pure address-parsing helpers. No network in this file."""
from __future__ import annotations

from etl.locate import classify_address, extract_coords_from_maps_url, plus_code_query


def test_classify_coordinates_plain():
    assert classify_address("51.23087, -115.4957") == ("coordinates", "51.23087, -115.4957")


def test_classify_coordinates_quoted_and_padded():
    kind, normalised = classify_address('  "51.23087, -115.4957"  ')
    assert kind == "coordinates"
    assert normalised == "51.23087, -115.4957"


def test_classify_coordinates_negative_both():
    assert classify_address("-33.8688, -151.2093")[0] == "coordinates"


def test_classify_out_of_range_latitude_is_not_coordinates():
    # 191 is not a valid latitude — falls through to address (won't match plus code
    # or anything else meaningful, but must not be misclassified as coordinates).
    kind, _ = classify_address("191, -115.4957")
    assert kind == "address"


def test_classify_out_of_range_longitude_is_not_coordinates():
    kind, _ = classify_address("51.23087, 181")
    assert kind == "address"


def test_classify_global_plus_code():
    assert classify_address("9535R9RG+9X") == ("plus_code", "9535R9RG+9X")


def test_classify_compound_plus_code():
    kind, normalised = classify_address("4VMF+42 Whistler, British Columbia, Canada")
    assert kind == "plus_code"
    assert normalised == "4VMF+42 Whistler, British Columbia, Canada"


def test_classify_plus_code_lowercase():
    assert classify_address("9535r9rg+9x")[0] == "plus_code"


def test_classify_address_string():
    assert classify_address("Shannon Falls, Squamish, BC")[0] == "address"


def test_classify_address_with_literal_plus_is_not_a_plus_code():
    # A "+" in ordinary text (unit number, "and") must not misdetect as OLC — OLC
    # uses a restricted character set that excludes vowels and 0/1.
    kind, _ = classify_address("Smith + Jones Ave, Vancouver")
    assert kind == "address"


def test_classify_empty_string():
    assert classify_address("") == ("empty", "")


def test_classify_whitespace_only_is_empty():
    assert classify_address("   ") == ("empty", "")


def test_extract_coords_from_long_url_prefers_3d4d_over_at():
    url = (
        "https://www.google.com/maps/place/Skyline+Viewpoint/@51.0343408,-114.048786,3a,75y"
        "/data=!3m8!1e2!3m6!1sCIHM0ogKEICAgIDh44vyeQ!2e10"
        "!7i4032!8i3024!4m6!3m5!1s0x537170087db4f3db:0x863939e6cd10b867"
        "!8m2!3d51.0300000!4d-114.0500000"
    )
    result = extract_coords_from_maps_url(url)
    assert result == (51.0300000, -114.0500000)


def test_extract_coords_from_url_with_only_at_pattern():
    url = "https://www.google.com/maps/@49.6725,-123.1583,15z"
    assert extract_coords_from_maps_url(url) == (49.6725, -123.1583)


def test_extract_coords_from_short_link_returns_none():
    assert extract_coords_from_maps_url("https://maps.app.goo.gl/xYz123") is None


def test_extract_coords_from_url_with_neither_pattern_returns_none():
    assert extract_coords_from_maps_url("https://www.google.com/maps/place/Somewhere") is None


def test_plus_code_query_encodes_plus_and_space():
    assert plus_code_query("9535R9RG+9X") == "9535R9RG%2B9X"


def test_plus_code_query_compound_encodes_commas_and_spaces_too():
    encoded = plus_code_query("4VMF+42 Whistler, British Columbia, Canada")
    assert encoded == "4VMF%2B42%20Whistler%2C%20British%20Columbia%2C%20Canada"
