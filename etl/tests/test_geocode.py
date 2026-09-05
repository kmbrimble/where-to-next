"""Tests for etl/geocode.py — all HTTP mocked via injected fetch, no live calls."""
from __future__ import annotations

import pytest

from etl.geocode import GeocodeClient, RequestBudget


def fake_fetch_ok(url: str) -> dict:
    return {
        "status": "OK",
        "results": [{
            "place_id": "ChIJabc123",
            "geometry": {"location": {"lat": 49.6725, "lng": -123.1583}, "location_type": "ROOFTOP"},
        }],
    }


def test_geocode_returns_result_and_counts_call():
    client = GeocodeClient(api_key="fake-key", fetch=fake_fetch_ok)
    result = client.geocode("Shannon Falls, Squamish, BC")

    assert result.lat == 49.6725
    assert result.lng == -123.1583
    assert result.place_id == "ChIJabc123"
    assert result.location_type == "ROOFTOP"
    assert client.call_count == 1


def test_geocode_zero_results_returns_none():
    client = GeocodeClient(api_key="fake-key", fetch=lambda url: {"status": "ZERO_RESULTS", "results": []})
    assert client.geocode("Nonexistent Place XYZ") is None
    assert client.call_count == 1


def test_geocode_error_status_raises():
    client = GeocodeClient(api_key="fake-key", fetch=lambda url: {"status": "OVER_QUERY_LIMIT"})
    with pytest.raises(RuntimeError):
        client.geocode("Somewhere")


def test_geocode_without_api_key_raises_before_any_fetch(monkeypatch):
    monkeypatch.delenv("GOOGLE_GEOCODING_KEY", raising=False)
    called = []
    client = GeocodeClient(api_key=None, fetch=lambda url: called.append(url))
    with pytest.raises(RuntimeError):
        client.geocode("Somewhere")
    assert called == []


def test_query_is_url_encoded_plus_and_space():
    captured = {}

    def fetch(url):
        captured["url"] = url
        return {"status": "ZERO_RESULTS"}

    client = GeocodeClient(api_key="fake-key", fetch=fetch)
    client.geocode("9535R9RG+9X Whistler")
    assert "%2B" in captured["url"]
    assert "%20" in captured["url"]
    assert "+" not in captured["url"].split("address=")[1].split("&")[0]


def test_api_key_never_appears_in_call_count_or_result_repr():
    client = GeocodeClient(api_key="super-secret-key", fetch=fake_fetch_ok)
    result = client.geocode("Somewhere")
    assert "super-secret-key" not in repr(result)
    assert "super-secret-key" not in repr(client.call_count)


def test_request_budget_allows_under_limit():
    RequestBudget(limit=300).check(299)  # no raise


def test_request_budget_refuses_over_limit_before_any_request():
    budget = RequestBudget(limit=300)
    with pytest.raises(RuntimeError) as exc_info:
        budget.check(301)
    message = str(exc_info.value)
    assert "301" in message
    assert "--allow-bulk" in message


def test_request_budget_allow_bulk_overrides():
    RequestBudget(limit=300, allow_bulk=True).check(5000)  # no raise


def test_request_budget_exactly_at_limit_is_allowed():
    RequestBudget(limit=300).check(300)  # no raise, boundary is inclusive
