"""Pure address-parsing helpers for stage 2 location resolution. No network calls,
no sheet access — see docs/SCHEMA.md's Address-resolution notes for the detection
order this implements (coordinates, then plus code, then address string).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote

# Coordinates are compared to a cache value at this tolerance (~1m) before treating
# them as "disagreeing" — floats round-tripped through the sheet lose some precision.
COORD_TOLERANCE = 1e-5

COORD_RE = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?)\s*$")

# Open Location Code alphabet — deliberately excludes 0,1,I,O and vowel-like letters
# that could be misread. Without restricting to this set, any address containing a
# literal "+" (unit numbers, "Smith + Jones Ave") would misdetect as a plus code.
OLC_CHARS = "23456789CFGHJMPQRVWX"
PLUS_CODE_RE = re.compile(
    rf"^[{OLC_CHARS}]{{4,8}}\+[{OLC_CHARS}]{{2,}}(\s+.+)?$",
    re.IGNORECASE,
)

MAPS_AT_RE = re.compile(r"@(-?\d+\.\d+),(-?\d+\.\d+)")
MAPS_3D4D_RE = re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)")


def classify_address(raw: str) -> tuple[str, str]:
    """Return (kind, normalised) where kind is one of:
    "empty", "coordinates", "plus_code", "address".
    """
    v = raw.strip().strip("\"'").strip()
    if not v:
        return "empty", ""

    m = COORD_RE.match(v)
    if m:
        lat, lng = float(m.group(1)), float(m.group(2))
        if -90 <= lat <= 90 and -180 <= lng <= 180:
            return "coordinates", v

    if PLUS_CODE_RE.match(v):
        return "plus_code", v

    return "address", v


def extract_coords_from_maps_url(url: str) -> tuple[float, float] | None:
    """Pull a lat/lng pin out of a Google Maps URL. Prefers !3d<lat>!4d<lng> (the
    actual pin) over @lat,lng (the viewport centre, which can differ from the pin).
    Short links (maps.app.goo.gl) return None — resolving those needs a redirect
    follow, which is a network call and belongs to the live-geocoding path.
    """
    if "maps.app.goo.gl" in url or "goo.gl" in url:
        return None

    m = MAPS_3D4D_RE.search(url)
    if m:
        return float(m.group(1)), float(m.group(2))

    m = MAPS_AT_RE.search(url)
    if m:
        return float(m.group(1)), float(m.group(2))

    return None


def plus_code_query(raw: str) -> str:
    """URL-encode a plus code for a geocoding request: + -> %2B, space -> %20."""
    return quote(raw.strip(), safe="")


@dataclass
class ResolutionPlan:
    """A decision about what to do for one stop's location — never the resolution
    itself. Actions:
    - "use_cache": cache is trusted as-is, no network call.
    - "resolve_coordinates": Address is a coordinate pair; lat/lng is already known,
      no network call (cache was empty, or there was no cache to check).
    - "overwrite_cache_coordinates": Address is a coordinate pair that disagrees with
      a populated cache; the cache is wrong (coordinates are deterministic), so
      overwrite it with the Address-derived value. No network call.
    - "resolve_plus_code": needs a geocode call using `query`. Plus codes are
      deterministic but can't be decoded offline, so this always calls regardless
      of whether a cache exists — a stale cache is exactly what SCHEMA.md §3 says
      not to trust for a deterministic Address.
    - "resolve_address": needs a geocode call using `query`. Only reached when the
      cache is empty — a populated cache is trusted for address strings.
    - "resolve_maps_link": Address is empty but a Links URL yielded coordinates
      directly (no network call — the redirect-follow case for short links is a
      later piece). Always flagged via `warning` for the report's eyeball list.
    - "unresolvable": Address is empty and no link yielded usable coordinates.
    """
    action: str
    lat: float | None = None
    lng: float | None = None
    place_id: str | None = None
    query: str | None = None
    warning: str | None = None


def decide_resolution(
    address: str | None,
    links: list[str],
    cached_lat: float | None,
    cached_lng: float | None,
    cached_place_id: str | None,
) -> ResolutionPlan:
    kind, normalised = classify_address(address or "")
    has_cache = cached_lat is not None and cached_lng is not None

    if kind == "coordinates":
        m = COORD_RE.match(normalised)
        lat, lng = float(m.group(1)), float(m.group(2))
        if has_cache:
            if abs(cached_lat - lat) <= COORD_TOLERANCE and abs(cached_lng - lng) <= COORD_TOLERANCE:
                return ResolutionPlan(action="use_cache", lat=cached_lat, lng=cached_lng, place_id=cached_place_id)
            return ResolutionPlan(
                action="overwrite_cache_coordinates",
                lat=lat,
                lng=lng,
                warning=(
                    f"cached lat/lng ({cached_lat}, {cached_lng}) disagreed with the "
                    f"deterministic coordinates in Address ({lat}, {lng}) — cache overwritten"
                ),
            )
        return ResolutionPlan(action="resolve_coordinates", lat=lat, lng=lng)

    if kind == "plus_code":
        return ResolutionPlan(action="resolve_plus_code", query=normalised)

    if kind == "address":
        if has_cache:
            return ResolutionPlan(action="use_cache", lat=cached_lat, lng=cached_lng, place_id=cached_place_id)
        return ResolutionPlan(action="resolve_address", query=normalised)

    # kind == "empty"
    for link in links:
        coords = extract_coords_from_maps_url(link)
        if coords:
            lat, lng = coords
            return ResolutionPlan(
                action="resolve_maps_link",
                lat=lat,
                lng=lng,
                warning="resolved from a Links URL, not Address — needs eyeballing",
            )
    return ResolutionPlan(action="unresolvable")
