"""Pure address-parsing helpers for stage 2 location resolution. No network calls,
no sheet access — see docs/SCHEMA.md's Address-resolution notes for the detection
order this implements (coordinates, then plus code, then address string).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from urllib.parse import quote

from openlocationcode import openlocationcode as olc

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
URL_RE = re.compile(r"https?://\S+")


def is_short_maps_link(url: str) -> bool:
    return "maps.app.goo.gl" in url or "goo.gl" in url


def find_maps_urls(text: str) -> list[str]:
    """Find every Google Maps URL in a text blob (Links or Notes), in order of
    appearance. Trailing punctuation from prose ("see it here.") is stripped.
    """
    if not text:
        return []
    urls = URL_RE.findall(text)
    return [u.rstrip(").,;\"'") for u in urls if "google.com/maps" in u or is_short_maps_link(u)]


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


def decode_global_plus_code(normalised: str) -> tuple[float, float] | None:
    """Decode a GLOBAL plus code offline (openlocationcode), returning the centre
    of the decoded cell — no API call. Returns None for a compound/short code
    (e.g. "4VMF+42 Whistler, BC") or one with a trailing locality string: those
    need the locality resolved via geocoding to recover the missing leading
    digits, which we deliberately do NOT attempt to reconstruct from a nearby
    reference (see docs/SCHEMA.md's compound-code note).
    """
    code = normalised.split()[0] if normalised.split() else normalised
    if code != normalised or not olc.isFull(code):
        return None
    area = olc.decode(code)
    return area.latitudeCenter, area.longitudeCenter


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
    - "resolve_maps_link": a Links or Notes URL yielded coordinates directly (no
      network call). Outranks geocoding an address string and outranks a cached
      value for one — the URL was hand-copied by the user from Maps pointing at
      the exact spot they mean, which is stronger evidence than a geocode result
      (docs/SCHEMA.md §3: geocoding an address string can return a confidently
      wrong answer). `alt_urls` lists any other Maps URLs found on the row that
      weren't used.
    - "resolve_short_link": a short Maps link (maps.app.goo.gl / goo.gl) was found
      but needs a redirect follow to reveal coordinates — that's a network call,
      so it's deferred to the live execution path. `query` is the short URL.
    - "resolve_address": needs a geocode call using `query`. Only reached when
      there's no coordinate/plus-code Address and no usable Maps URL anywhere on
      the row, and (if a cache exists) the cache is empty — see resolve_maps_link
      for why a Maps URL outranks a cached geocode too.
    - "unresolvable": nothing usable — no Address, no Maps URL, no cache.
    """
    action: str
    lat: float | None = None
    lng: float | None = None
    place_id: str | None = None
    query: str | None = None
    warning: str | None = None
    alt_urls: list[str] = field(default_factory=list)
    fallback_query: str | None = None  # geocode this Address if resolve_short_link fails


def _find_maps_evidence(links: list[str], notes: str | None) -> tuple[tuple[str, float, float] | None, list[str], list[str]]:
    """Scan Links + Notes for Maps URLs. Returns (first usable long-link hit or
    None, other short links found, other URLs found but not used)."""
    urls: list[str] = []
    for text in list(links) + [notes or ""]:
        urls.extend(find_maps_urls(text))

    long_hit = None
    short_links: list[str] = []
    unused: list[str] = []
    for url in urls:
        if is_short_maps_link(url):
            short_links.append(url)
            continue
        coords = extract_coords_from_maps_url(url)
        if coords and long_hit is None:
            long_hit = (url, coords[0], coords[1])
        elif coords:
            unused.append(url)
        else:
            unused.append(url)
    return long_hit, short_links, unused


def decide_resolution(
    address: str | None,
    links: list[str],
    cached_lat: float | None,
    cached_lng: float | None,
    cached_place_id: str | None,
    notes: str | None = None,
    reverify: bool = False,
) -> ResolutionPlan:
    """reverify=True ignores the cache entirely (the --reverify escape hatch) — used
    when a cache is suspected wrong, since id-based row matching makes normal runs
    safe to trust the cache without re-checking it (docs/SCHEMA.md §3).
    """
    kind, normalised = classify_address(address or "")
    has_cache = not reverify and cached_lat is not None and cached_lng is not None

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
        decoded = decode_global_plus_code(normalised)
        if decoded is not None:
            lat, lng = decoded
            if has_cache:
                if abs(cached_lat - lat) <= COORD_TOLERANCE and abs(cached_lng - lng) <= COORD_TOLERANCE:
                    return ResolutionPlan(action="use_cache", lat=cached_lat, lng=cached_lng, place_id=cached_place_id)
                return ResolutionPlan(
                    action="overwrite_cache_plus_code",
                    lat=lat,
                    lng=lng,
                    warning=(
                        f"cached lat/lng ({cached_lat}, {cached_lng}) disagreed with the "
                        f"decoded plus code ({lat}, {lng}) — cache overwritten"
                    ),
                )
            return ResolutionPlan(action="resolve_plus_code_offline", lat=lat, lng=lng)
        # Compound/short code — the locality has to be resolved via geocoding to
        # recover the missing leading digits; not reconstructable offline. Once
        # cached, trust it rather than re-geocoding every run — the id column now
        # handles row matching, so the old cache/Address-drift guard is redundant.
        if has_cache:
            return ResolutionPlan(action="use_cache", lat=cached_lat, lng=cached_lng, place_id=cached_place_id)
        return ResolutionPlan(action="resolve_plus_code", query=normalised)

    # A cached value is trusted here too — no network call, no re-scanning Links/
    # Notes — for the same reason as compound plus codes above. Maps-URL evidence
    # is only consulted when there's nothing cached yet to trust.
    if has_cache:
        return ResolutionPlan(action="use_cache", lat=cached_lat, lng=cached_lng, place_id=cached_place_id)

    long_hit, short_links, unused = _find_maps_evidence(links, notes)
    if long_hit:
        _, lat, lng = long_hit
        alt = short_links + unused
        warning = "resolved from a Maps URL — needs eyeballing"
        if alt:
            warning += f" ({len(alt)} other Maps URL(s) on this row not used)"
        return ResolutionPlan(action="resolve_maps_link", lat=lat, lng=lng, warning=warning, alt_urls=alt)

    if short_links:
        # If the short link fails to resolve (dead link, redirect follow fails),
        # execution must fall through to geocoding the Address rather than giving
        # up — carry it along now so that fallback doesn't need re-deciding later.
        # has_cache is always False here (already handled above), so this only
        # depends on there being a plain address string to fall back to.
        fallback = normalised if kind == "address" else None
        return ResolutionPlan(
            action="resolve_short_link", query=short_links[0], alt_urls=short_links[1:] + unused,
            fallback_query=fallback,
        )

    if kind == "address":
        return ResolutionPlan(action="resolve_address", query=normalised)

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


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def km_outside_box(lat: float, lng: float, timezone: str) -> float:
    """Distance from (lat, lng) to the nearest edge of its timezone's bounding box."""
    lat_min, lat_max, lng_min, lng_max = TIMEZONE_BOUNDING_BOXES[timezone]
    clamped_lat = min(max(lat, lat_min), lat_max)
    clamped_lng = min(max(lng, lng_min), lng_max)
    return _haversine_km(lat, lng, clamped_lat, clamped_lng)


@dataclass
class LocationReport:
    counts: dict
    projected_calls: int
    actual_calls: int
    eyeball: list
    would_write: list
    errors: list
    warnings: list
    approximate: list = field(default_factory=list)
    geometric_center: list = field(default_factory=list)
    maps_link_long: list = field(default_factory=list)
    maps_link_short: list = field(default_factory=list)
    still_needs_geocode: list = field(default_factory=list)
    unparseable_maps_urls: list = field(default_factory=list)
    plus_code_global: int = 0
    plus_code_compound: int = 0


def resolve_locations(
    trip, *, live: bool, client=None, budget=None, short_link_resolver=None, reverify: bool = False
) -> LocationReport:
    """Walk every stop, decide via decide_resolution(), and either record what WOULD
    happen (dry run — no mutation, no network) or actually resolve it (--live).
    """
    counts = {"coordinates": 0, "plus_code": 0, "geocoded": 0, "maps_link": 0, "cached": 0, "unresolved": 0}
    eyeball: list[str] = []
    would_write: list[str] = []
    errors: list[str] = []
    warnings: list[str] = []
    to_geocode: list[tuple] = []
    approximate: list[str] = []
    geometric_center: list[str] = []
    maps_link_long: list[str] = []
    maps_link_short: list[str] = []
    still_needs_geocode: list[str] = []
    unparseable_maps_urls: list[str] = []
    plus_code_global = 0
    plus_code_compound = 0

    for day in trip.days:
        for stop in day.stops:
            plan = decide_resolution(
                stop.address, stop.links, stop.lat, stop.lng, stop.place_id, stop.notes, reverify=reverify,
            )

            for extra in plan.alt_urls:
                unparseable_maps_urls.append(f"Row {stop.row_num}: {stop.title!r} — {extra!r} not used")

            if plan.action == "use_cache":
                counts["cached"] += 1

            elif plan.action in ("resolve_coordinates", "overwrite_cache_coordinates"):
                if plan.warning:
                    warnings.append(f"Row {stop.row_num}: {plan.warning}")
                box_ok = in_bounding_box(plan.lat, plan.lng, stop.timezone)
                if box_ok is False:
                    # Wrong, not corrupt: discard the bad value and treat the stop as
                    # unresolved rather than failing the whole build over one stop.
                    dist = km_outside_box(plan.lat, plan.lng, stop.timezone)
                    counts["unresolved"] += 1
                    warnings.append(
                        f"Row {stop.row_num}: resolved coordinates ({plan.lat}, {plan.lng}) "
                        f"fall ~{dist:.0f}km outside the {stop.timezone} bounding box — discarded"
                    )
                    if live:
                        stop.lat, stop.lng, stop.place_id, stop.resolved_from = None, None, None, "unresolved"
                else:
                    counts["coordinates"] += 1
                    would_write.append(
                        f"Row {stop.row_num}: id={stop.id} lat={plan.lat} lng={plan.lng} resolved_from=coordinates"
                    )
                    if live:
                        stop.lat, stop.lng, stop.resolved_from = plan.lat, plan.lng, "coordinates"

            elif plan.action in ("resolve_plus_code_offline", "overwrite_cache_plus_code"):
                if plan.warning:
                    warnings.append(f"Row {stop.row_num}: {plan.warning}")
                box_ok = in_bounding_box(plan.lat, plan.lng, stop.timezone)
                if box_ok is False:
                    dist = km_outside_box(plan.lat, plan.lng, stop.timezone)
                    counts["unresolved"] += 1
                    warnings.append(
                        f"Row {stop.row_num}: decoded plus code ({plan.lat}, {plan.lng}) "
                        f"fall ~{dist:.0f}km outside the {stop.timezone} bounding box — discarded"
                    )
                    if live:
                        stop.lat, stop.lng, stop.place_id, stop.resolved_from = None, None, None, "unresolved"
                else:
                    counts["plus_code"] += 1
                    plus_code_global += 1
                    would_write.append(
                        f"Row {stop.row_num}: id={stop.id} lat={plan.lat} lng={plan.lng} resolved_from=plus_code"
                    )
                    if live:
                        stop.lat, stop.lng, stop.resolved_from = plan.lat, plan.lng, "plus_code"

            elif plan.action == "resolve_plus_code":
                counts["plus_code"] += 1
                plus_code_compound += 1
                to_geocode.append((stop, plan, "plus_code"))

            elif plan.action == "resolve_address":
                counts["geocoded"] += 1
                to_geocode.append((stop, plan, "geocoded"))
                eyeball.append(f"Row {stop.row_num}: {stop.title!r} geocoded from address — needs eyeballing")
                still_needs_geocode.append(f"Row {stop.row_num}: {stop.title!r}")

            elif plan.action == "resolve_maps_link":
                counts["maps_link"] += 1
                eyeball.append(f"Row {stop.row_num}: {stop.title!r} resolved from a Maps URL — needs eyeballing")
                maps_link_long.append(f"Row {stop.row_num}: {stop.title!r}")
                would_write.append(
                    f"Row {stop.row_num}: id={stop.id} lat={plan.lat} lng={plan.lng} resolved_from=maps_link"
                )
                if live:
                    stop.lat, stop.lng, stop.resolved_from = plan.lat, plan.lng, "maps_link"

            elif plan.action == "resolve_short_link":
                to_geocode.append((stop, plan, "short_link"))

            else:  # unresolvable
                counts["unresolved"] += 1
                # A warning, not an error — matches the rest of this ETL's incremental-
                # migration stance (empty row_type, missing kind/timing, etc.): a stop
                # not yet given an Address isn't corruption, it's just not done yet.
                warnings.append(f"Row {stop.row_num}: {stop.title!r} has no resolvable Address or Links coordinates")

    projected_calls = len(to_geocode)

    if not live:
        for stop, plan, category in to_geocode:
            if category == "short_link":
                would_write.append(f"Row {stop.row_num}: id={stop.id} PENDING short-link follow ({plan.query!r})")
                maps_link_short.append(f"Row {stop.row_num}: {stop.title!r}")
            else:
                would_write.append(f"Row {stop.row_num}: id={stop.id} PENDING geocode ({category}) query={plan.query!r}")
        return LocationReport(
            counts, projected_calls, 0, eyeball, would_write, errors, warnings, approximate, geometric_center,
            maps_link_long, maps_link_short, still_needs_geocode, unparseable_maps_urls,
            plus_code_global, plus_code_compound,
        )

    if budget is not None:
        budget.check(projected_calls)

    def geocode_and_apply(stop, query: str, category: str) -> None:
        result = client.geocode(query)
        if result is None:
            errors.append(f"Row {stop.row_num}: {stop.title!r} — geocode returned no results for {query!r}")
            return

        box_ok = in_bounding_box(result.lat, result.lng, stop.timezone)
        if box_ok is False:
            dist = km_outside_box(result.lat, result.lng, stop.timezone)
            counts["unresolved"] += 1
            warnings.append(
                f"Row {stop.row_num}: resolved coordinates ({result.lat}, {result.lng}) "
                f"fall ~{dist:.0f}km outside the {stop.timezone} bounding box — discarded"
            )
            stop.lat, stop.lng, stop.place_id, stop.resolved_from = None, None, None, "unresolved"
            return

        stop.lat, stop.lng, stop.place_id, stop.resolved_from = result.lat, result.lng, result.place_id, category

        # A plus code always decodes to GEOMETRIC_CENTER (the center of its grid
        # cell) — that's the format working correctly, not low confidence. Only
        # geocoded (address-string) results get flagged for precision.
        if category != "plus_code" and result.location_type in LOW_PRECISION_TYPES:
            entry = f"Row {stop.row_num}: {stop.title!r}"
            if result.location_type == "APPROXIMATE":
                warnings.append(f"Row {stop.row_num}: geocode precision APPROXIMATE (no specific feature found)")
                approximate.append(entry)
            else:
                warnings.append(f"Row {stop.row_num}: geocode precision GEOMETRIC_CENTER")
                geometric_center.append(entry)

        would_write.append(
            f"Row {stop.row_num}: id={stop.id} lat={result.lat} lng={result.lng} resolved_from={category}"
        )

    for stop, plan, category in to_geocode:
        if category == "short_link":
            final_url = short_link_resolver.resolve(plan.query) if short_link_resolver else None
            coords = extract_coords_from_maps_url(final_url) if final_url else None
            if coords:
                counts["maps_link"] += 1
                maps_link_short.append(f"Row {stop.row_num}: {stop.title!r}")
                stop.lat, stop.lng, stop.resolved_from = coords[0], coords[1], "maps_link"
                would_write.append(
                    f"Row {stop.row_num}: id={stop.id} lat={coords[0]} lng={coords[1]} resolved_from=maps_link"
                )
                continue

            reason = "could not be followed" if not final_url else f"resolved but no coordinates found in {final_url!r}"
            if plan.fallback_query:
                warnings.append(
                    f"Row {stop.row_num}: {stop.title!r} — short Maps link {reason}, "
                    f"falling back to geocoding Address"
                )
                counts["geocoded"] += 1
                geocode_and_apply(stop, plan.fallback_query, "geocoded")
            else:
                warnings.append(f"Row {stop.row_num}: {stop.title!r} — short Maps link {reason}")
                counts["unresolved"] += 1
            continue

        geocode_and_apply(stop, plan.query, category)

    actual_calls = client.call_count if client is not None else 0
    return LocationReport(
        counts, projected_calls, actual_calls, eyeball, would_write, errors, warnings, approximate, geometric_center,
        maps_link_long, maps_link_short, still_needs_geocode, unparseable_maps_urls,
        plus_code_global, plus_code_compound,
    )
