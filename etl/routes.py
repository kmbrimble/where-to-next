"""Thin Google Routes API client for stage-3 polylines. No call happens until
.compute_route() is invoked. Never logs the API key.

Routing preference is explicit: TRAFFIC_UNAWARE. Traffic-aware routing
(TRAFFIC_AWARE / TRAFFIC_AWARE_OPTIMAL) is a Pro SKU with only 5,000 free
events/month; plain routing is Essentials with 10,000. Polylines here are
precomputed once and static — live traffic is the Worker's job at request time,
not the ETL's. Using the traffic-aware preference here would silently double the
cost tier for a number this code never even uses.
"""
from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from typing import Callable

ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"

# See module docstring — deliberately not TRAFFIC_AWARE.
ROUTING_PREFERENCE = "TRAFFIC_UNAWARE"


@dataclass
class RouteResult:
    polyline: str
    distance_m: int | None
    duration_s: int


class RoutesClient:
    """Wraps the Routes API and counts every request it makes."""

    def __init__(self, api_key: str | None = None, fetch: Callable[[str, bytes], dict] | None = None):
        self._api_key = api_key or os.environ.get("GOOGLE_ROUTES_KEY")
        self._fetch = fetch or self._http_fetch
        self._call_count = 0

    @property
    def call_count(self) -> int:
        return self._call_count

    def compute_route(self, origin_latlng: tuple[float, float], dest_latlng: tuple[float, float], mode: str) -> RouteResult | None:
        """mode is "DRIVE" or "WALK". Returns None on ZERO_RESULTS-equivalent."""
        if not self._api_key:
            raise RuntimeError("GOOGLE_ROUTES_KEY is not set")

        body = json.dumps({
            "origin": {"location": {"latLng": {"latitude": origin_latlng[0], "longitude": origin_latlng[1]}}},
            "destination": {"location": {"latLng": {"latitude": dest_latlng[0], "longitude": dest_latlng[1]}}},
            "travelMode": mode,
            "routingPreference": ROUTING_PREFERENCE if mode == "DRIVE" else None,
        }).encode("utf-8")

        self._call_count += 1
        payload = self._fetch(body)
        routes = payload.get("routes") or []
        if not routes:
            return None
        route = routes[0]

        # distanceMeters is dropped by Google's protobuf-JSON encoding when the
        # value is exactly 0 (field-presence semantics, not an error) — verified
        # against a live zero-length route. That's a genuine "0m", not a parse
        # failure, so it's None+warned by the caller rather than defaulted here.
        # polyline/duration are never legitimately absent; their absence means
        # the response shape changed and callers need to know exactly where.
        if "polyline" not in route or "encodedPolyline" not in route.get("polyline", {}):
            raise RuntimeError(
                f"Routes API response missing polyline.encodedPolyline for "
                f"{origin_latlng} -> {dest_latlng} ({mode})"
            )
        if "duration" not in route:
            raise RuntimeError(
                f"Routes API response missing duration for "
                f"{origin_latlng} -> {dest_latlng} ({mode})"
            )

        return RouteResult(
            polyline=route["polyline"]["encodedPolyline"],
            distance_m=route.get("distanceMeters"),
            duration_s=int(route["duration"].rstrip("s")),
        )

    def _http_fetch(self, body: bytes) -> dict:
        req = urllib.request.Request(
            ROUTES_URL,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": self._api_key,
                "X-Goog-FieldMask": "routes.duration,routes.distanceMeters,routes.polyline.encodedPolyline",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
