"""Thin Google Geocoding API client. No call happens until .geocode() is invoked —
construction alone never touches the network. Never logs the API key.
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"


@dataclass
class GeocodeResult:
    lat: float
    lng: float
    place_id: str
    location_type: str  # ROOFTOP | RANGE_INTERPOLATED | GEOMETRIC_CENTER | APPROXIMATE


class GeocodeClient:
    """Wraps the Geocoding API and counts every request it makes."""

    def __init__(self, api_key: str | None = None, fetch: Callable[[str], dict] | None = None):
        self._api_key = api_key or os.environ.get("GOOGLE_GEOCODING_KEY")
        self._fetch = fetch or self._http_fetch
        self._call_count = 0

    @property
    def call_count(self) -> int:
        return self._call_count

    def geocode(self, query: str) -> GeocodeResult | None:
        """query is a raw (unencoded) address or plus-code string."""
        if not self._api_key:
            raise RuntimeError("GOOGLE_GEOCODING_KEY is not set")

        encoded = urllib.parse.quote(query.strip(), safe="")
        url = f"{GEOCODE_URL}?address={encoded}&key={self._api_key}"
        self._call_count += 1
        payload = self._fetch(url)
        return self._parse(payload)

    @staticmethod
    def _parse(payload: dict) -> GeocodeResult | None:
        status = payload.get("status")
        if status == "ZERO_RESULTS":
            return None
        if status != "OK":
            raise RuntimeError(f"Geocoding API error: {status}")

        result = payload["results"][0]
        location = result["geometry"]["location"]
        return GeocodeResult(
            lat=location["lat"],
            lng=location["lng"],
            place_id=result["place_id"],
            location_type=result["geometry"].get("location_type", "UNKNOWN"),
        )

    @staticmethod
    def _http_fetch(url: str) -> dict:
        with urllib.request.urlopen(url) as resp:
            return json.loads(resp.read().decode("utf-8"))


class RequestBudget:
    """Hard cap on geocoding requests per invocation (docs/SCHEMA.md §7 safety
    guard) — Maps daily quota isn't adjustable on this account, so this is the only
    hard stop between a bad loop and the credit balance. Must be checked against the
    *projected* count before any request is sent, not counted down as calls happen.
    """

    DEFAULT_LIMIT = 300

    def __init__(self, limit: int = DEFAULT_LIMIT, allow_bulk: bool = False):
        self.limit = limit
        self.allow_bulk = allow_bulk

    def check(self, projected_count: int) -> None:
        if projected_count > self.limit and not self.allow_bulk:
            raise RuntimeError(
                f"Refusing to run: {projected_count} geocoding requests projected, "
                f"which exceeds the {self.limit}-request safety limit. "
                "Pass --allow-bulk to override."
            )


class ShortLinkResolver:
    """Follows a maps.app.goo.gl / goo.gl redirect to reveal the real Maps URL with
    coordinates in it. Counts as a network call (contributes to RequestBudget) and
    caches per-process so the same short link is only followed once. Failures
    (dead link, timeout) return None rather than raising — a dead short link is a
    warning for the caller to raise, not a crash here.

    A small delay before each follow, plus one retry with backoff on failure,
    since a batch of rapid-fire redirect follows can trip transient rate-limiting
    (observed: 27/27 failures in one run, 0/33 failures when re-run moments later
    at a slower pace) rather than any real dead link or code bug.
    """

    def __init__(self, follow: Callable[[str], str | None] | None = None, delay: float = 0.2):
        self._follow = follow or self._http_follow
        self._cache: dict[str, str | None] = {}
        self._call_count = 0
        self._delay = delay

    @property
    def call_count(self) -> int:
        return self._call_count

    def resolve(self, short_url: str) -> str | None:
        if short_url in self._cache:
            return self._cache[short_url]
        self._call_count += 1

        time.sleep(self._delay)
        resolved = self._try_follow(short_url)
        if resolved is None:
            time.sleep(self._delay * 3)  # backoff before the one retry
            resolved = self._try_follow(short_url)

        self._cache[short_url] = resolved
        return resolved

    def _try_follow(self, short_url: str) -> str | None:
        try:
            return self._follow(short_url)
        except Exception:
            return None

    @staticmethod
    def _http_follow(short_url: str) -> str | None:
        req = urllib.request.Request(short_url, method="HEAD")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.geturl()
