"""Tests for etl/routes.py — HTTP mocked, no live calls."""
from __future__ import annotations

import json

import pytest

from etl.routes import ROUTING_PREFERENCE, RoutesClient


def fake_fetch_ok(body: bytes) -> dict:
    payload = json.loads(body)
    assert payload["routingPreference"] == "TRAFFIC_UNAWARE" if payload["travelMode"] == "DRIVE" else True
    return {"routes": [{
        "distanceMeters": 14200,
        "duration": "1080s",
        "polyline": {"encodedPolyline": "abc123"},
    }]}


def test_compute_route_returns_result_and_counts_call():
    client = RoutesClient(api_key="fake-key", fetch=fake_fetch_ok)
    result = client.compute_route((49.67, -123.15), (49.68, -123.16), "DRIVE")

    assert result.polyline == "abc123"
    assert result.distance_m == 14200
    assert result.duration_s == 1080
    assert client.call_count == 1


def test_drive_uses_traffic_unaware_preference_not_traffic_aware():
    captured = {}

    def fetch(body):
        captured["payload"] = json.loads(body)
        return {"routes": [{"distanceMeters": 1, "duration": "1s", "polyline": {"encodedPolyline": "x"}}]}

    client = RoutesClient(api_key="fake-key", fetch=fetch)
    client.compute_route((0, 0), (1, 1), "DRIVE")

    assert captured["payload"]["routingPreference"] == ROUTING_PREFERENCE
    assert ROUTING_PREFERENCE == "TRAFFIC_UNAWARE"  # the actual cost-tier-critical value


def test_no_results_returns_none():
    client = RoutesClient(api_key="fake-key", fetch=lambda body: {"routes": []})
    assert client.compute_route((0, 0), (1, 1), "WALK") is None


def test_no_api_key_raises_before_any_fetch():
    called = []
    client = RoutesClient(api_key=None, fetch=lambda body: called.append(body))
    with pytest.raises(RuntimeError):
        client.compute_route((0, 0), (1, 1), "DRIVE")
    assert called == []


def test_key_never_appears_in_result_repr():
    client = RoutesClient(api_key="super-secret", fetch=fake_fetch_ok)
    result = client.compute_route((0, 0), (1, 1), "WALK")
    assert "super-secret" not in repr(result)
