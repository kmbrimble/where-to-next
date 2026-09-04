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


# Approximate (lat_min, lat_max, lng_min, lng_max) per IANA zone the trip touches.
# Generous on purpose — this catches a mis-pasted coordinate, not a precision audit.
# An unknown zone skips the check rather than erroring (see in_bounding_box).
TIMEZONE_BOUNDING_BOXES = {
    "Australia/Brisbane": (-29.0, -9.0, 138.0, 154.0),
    "Pacific/Auckland": (-47.5, -34.0, 166.0, 179.0),
    "America/Vancouver": (48.0, 60.0, -139.0, -114.0),
    "America/Edmonton": (48.0, 60.0, -120.0, -110.0),
    "America/Phoenix": (31.0, 37.0, -114.8, -109.0),
    "America/Los_Angeles": (32.0, 42.0, -124.5, -114.0),
}

LOW_PRECISION_TYPES = {"APPROXIMATE", "GEOMETRIC_CENTER"}


def in_bounding_box(lat: float, lng: float, timezone: str) -> bool | None:
    """True/False if timezone has a known box, None if the zone is unknown (skip)."""
    box = TIMEZONE_BOUNDING_BOXES.get(timezone)
    if box is None:
        return None
    lat_min, lat_max, lng_min, lng_max = box
    return lat_min <= lat <= lat_max and lng_min <= lng <= lng_max


@dataclass
class LocationReport:
    counts: dict
    projected_calls: int
    actual_calls: int
    eyeball: list
    would_write: list
    errors: list
    warnings: list


def resolve_locations(trip, *, live: bool, client=None, budget=None) -> LocationReport:
    """Walk every stop, decide via decide_resolution(), and either record what WOULD
    happen (dry run — no mutation, no network) or actually resolve it (--live).
    """
    counts = {"coordinates": 0, "plus_code": 0, "geocoded": 0, "maps_link": 0, "cached": 0, "unresolved": 0}
    eyeball: list[str] = []
    would_write: list[str] = []
    errors: list[str] = []
    warnings: list[str] = []
    to_geocode: list[tuple] = []

    for day in trip.days:
        for stop in day.stops:
            plan = decide_resolution(stop.address, stop.links, stop.lat, stop.lng, stop.place_id)

            if plan.action == "use_cache":
                counts["cached"] += 1

            elif plan.action in ("resolve_coordinates", "overwrite_cache_coordinates"):
                counts["coordinates"] += 1
                if plan.warning:
                    warnings.append(f"Row {stop.row_num}: {plan.warning}")
                box_ok = in_bounding_box(plan.lat, plan.lng, stop.timezone)
                if box_ok is False:
                    errors.append(
                        f"Row {stop.row_num}: resolved coordinates ({plan.lat}, {plan.lng}) "
                        f"outside the {stop.timezone} bounding box"
                    )
                else:
                    would_write.append(
                        f"Row {stop.row_num}: id={stop.id} lat={plan.lat} lng={plan.lng} resolved_from=coordinates"
                    )
                    if live:
                        stop.lat, stop.lng, stop.resolved_from = plan.lat, plan.lng, "coordinates"

            elif plan.action == "resolve_plus_code":
                counts["plus_code"] += 1
                to_geocode.append((stop, plan, "plus_code"))

            elif plan.action == "resolve_address":
                counts["geocoded"] += 1
                to_geocode.append((stop, plan, "geocoded"))
                eyeball.append(f"Row {stop.row_num}: {stop.title!r} geocoded from address — needs eyeballing")

            elif plan.action == "resolve_maps_link":
                counts["maps_link"] += 1
                eyeball.append(f"Row {stop.row_num}: {stop.title!r} resolved from Links — needs eyeballing")
                would_write.append(
                    f"Row {stop.row_num}: id={stop.id} lat={plan.lat} lng={plan.lng} resolved_from=maps_link"
                )
                if live:
                    stop.lat, stop.lng, stop.resolved_from = plan.lat, plan.lng, "maps_link"

            else:  # unresolvable
                counts["unresolved"] += 1
                # A warning, not an error — matches the rest of this ETL's incremental-
                # migration stance (empty row_type, missing kind/timing, etc.): a stop
                # not yet given an Address isn't corruption, it's just not done yet.
                warnings.append(f"Row {stop.row_num}: {stop.title!r} has no resolvable Address or Links coordinates")

    projected_calls = len(to_geocode)

    if not live:
        for stop, plan, category in to_geocode:
            would_write.append(f"Row {stop.row_num}: id={stop.id} PENDING geocode ({category}) query={plan.query!r}")
        return LocationReport(counts, projected_calls, 0, eyeball, would_write, errors, warnings)

    if budget is not None:
        budget.check(projected_calls)

    for stop, plan, category in to_geocode:
        result = client.geocode(plan.query)
        if result is None:
            errors.append(f"Row {stop.row_num}: {stop.title!r} — geocode returned no results for {plan.query!r}")
            continue
        stop.lat, stop.lng, stop.place_id, stop.resolved_from = result.lat, result.lng, result.place_id, category
        box_ok = in_bounding_box(result.lat, result.lng, stop.timezone)
        if box_ok is False:
            errors.append(
                f"Row {stop.row_num}: resolved coordinates ({result.lat}, {result.lng}) "
                f"outside the {stop.timezone} bounding box"
            )
        if result.location_type in LOW_PRECISION_TYPES:
            warnings.append(f"Row {stop.row_num}: geocode precision {result.location_type} — needs eyeballing")
            eyeball.append(f"Row {stop.row_num}: {stop.title!r} — {result.location_type} precision")
        would_write.append(
            f"Row {stop.row_num}: id={stop.id} lat={result.lat} lng={result.lng} resolved_from={category}"
        )

    actual_calls = client.call_count if client is not None else 0
    return LocationReport(counts, projected_calls, actual_calls, eyeball, would_write, errors, warnings)
