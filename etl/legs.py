"""Stage 3 piece 2: one leg per consecutive stop pair within a day. drive/walk/taxi
call the Routes API (cached); train/shuttle/plane/transit get a straight-line
geometry with no call. See docs/SCHEMA.md §2 (How table) and §8 (legs shape).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .models import Leg
from .routes import RouteResult, RoutesClient

CACHE_PATH = Path(__file__).parent / "cache" / "routes.json"

# drive/walk/taxi get a real route; taxi still drives, just isn't traffic-recalculated
# at runtime (that's the Worker's concern, not this ETL's) — SCHEMA.md's How table.
ROUTE_MODES = {"drive": "DRIVE", "walk": "WALK", "taxi": "DRIVE"}

DIVERGENCE_THRESHOLD = 0.25


def _round5(v: float) -> float:
    return round(v, 5)


def cache_key(origin: tuple[float, float], dest: tuple[float, float], mode: str) -> str:
    o = (_round5(origin[0]), _round5(origin[1]))
    d = (_round5(dest[0]), _round5(dest[1]))
    return f"{o[0]},{o[1]}|{d[0]},{d[1]}|{mode}"


def load_cache(path: Path = CACHE_PATH) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def save_cache(cache: dict, path: Path = CACHE_PATH) -> None:
    path.write_text(json.dumps(cache, sort_keys=True, indent=2) + "\n")


def encode_polyline(points: list[tuple[float, float]]) -> str:
    """Standard Google polyline algorithm, precision 5. Used for the straight-line
    (two-point) geometry on modes that don't get a routed polyline."""
    def encode_number(num: int) -> str:
        num = num << 1
        if num < 0:
            num = ~num
        chunks = []
        while num >= 0x20:
            chunks.append((0x20 | (num & 0x1F)) + 63)
            num >>= 5
        chunks.append(num + 63)
        return "".join(chr(c) for c in chunks)

    out = []
    prev_lat = prev_lng = 0
    for lat, lng in points:
        lat_i, lng_i = round(lat * 1e5), round(lng * 1e5)
        out.append(encode_number(lat_i - prev_lat))
        out.append(encode_number(lng_i - prev_lng))
        prev_lat, prev_lng = lat_i, lng_i
    return "".join(out)


@dataclass
class LegsReport:
    projected_calls: int = 0
    actual_calls: int = 0
    by_mode: dict = field(default_factory=dict)
    skipped_missing_coords: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    errors: list = field(default_factory=list)


def compute_legs(
    trip, *, live: bool, client: RoutesClient | None = None, budget=None, cache: dict | None = None,
) -> LegsReport:
    report = LegsReport()
    cache = {} if cache is None else cache
    to_call: list[tuple] = []  # (day, from_stop, to_stop, mode, key)

    for day in trip.days:
        stops = day.stops
        for i in range(len(stops) - 1):
            from_stop, to_stop = stops[i], stops[i + 1]
            how = (to_stop.how or "").lower()
            report.by_mode[how] = report.by_mode.get(how, 0) + 1

            if from_stop.lat is None or from_stop.lng is None or to_stop.lat is None or to_stop.lng is None:
                report.skipped_missing_coords.append(f"Day {day.day}: {from_stop.title!r} -> {to_stop.title!r}")
                report.warnings.append(
                    f"Day {day.day}: leg {from_stop.title!r} -> {to_stop.title!r} skipped — missing coordinates"
                )
                continue

            origin = (from_stop.lat, from_stop.lng)
            dest = (to_stop.lat, to_stop.lng)

            if how not in ROUTE_MODES:
                day.legs.append(Leg(
                    **{"from": from_stop.id}, to=to_stop.id, how=how or "unknown",
                    distance_m=None, duration_s=None, api_duration_s=None,
                    polyline=encode_polyline([origin, dest]),
                ))
                continue

            mode = ROUTE_MODES[how]
            key = cache_key(origin, dest, mode)
            if key in cache:
                cached = cache[key]
                _apply_leg(day, from_stop, to_stop, how, RouteResult(**cached), report)
            else:
                to_call.append((day, from_stop, to_stop, how, mode, origin, dest, key))

    report.projected_calls = len(to_call)

    if not live:
        return report

    if budget is not None:
        budget.check(report.projected_calls)

    for day, from_stop, to_stop, how, mode, origin, dest, key in to_call:
        result = client.compute_route(origin, dest, mode)
        if result is None:
            # ZERO_RESULTS is real but rare (e.g. two viewpoints with no
            # connecting road in Google's graph) — one unroutable leg must not
            # withhold the whole trip. Fall back to the same straight-line
            # geometry the non-routed modes already use, and warn instead of
            # failing; the warning is more useful than the crash.
            report.warnings.append(
                f"Day {day.day}: leg {from_stop.title!r} -> {to_stop.title!r} — Routes API returned "
                f"no route (ZERO_RESULTS); using straight-line geometry, duration left null"
            )
            day.legs.append(Leg(
                **{"from": from_stop.id}, to=to_stop.id, how=how,
                distance_m=None, duration_s=None, api_duration_s=None,
                polyline=encode_polyline([origin, dest]),
            ))
            continue
        cache[key] = {"polyline": result.polyline, "distance_m": result.distance_m, "duration_s": result.duration_s}
        _apply_leg(day, from_stop, to_stop, how, result, report)

    report.actual_calls = client.call_count if client is not None else 0
    return report


def _apply_leg(day, from_stop, to_stop, how: str, result: RouteResult, report: LegsReport) -> None:
    day.legs.append(Leg(
        **{"from": from_stop.id}, to=to_stop.id, how=how,
        distance_m=result.distance_m, duration_s=result.duration_s, api_duration_s=result.duration_s,
        polyline=result.polyline,
    ))

    if result.distance_m is None:
        report.warnings.append(
            f"Day {day.day}: leg {from_stop.title!r} -> {to_stop.title!r} — Routes API omitted "
            f"distanceMeters (zero-length route); stored as null, not defaulted to 0"
        )

    # The API's duration is NOT authoritative — the sheet's Travel column (the
    # human's own estimate) remains the schedule's source of truth. This is purely
    # a "your estimate may be stale" signal, and must never touch to_stop.travel_minutes.
    sheet_s = to_stop.travel_minutes * 60
    if sheet_s > 0 and abs(result.duration_s - sheet_s) / sheet_s > DIVERGENCE_THRESHOLD:
        report.warnings.append(
            f"Row {to_stop.row_num}: {to_stop.title!r} — Travel says {to_stop.travel_minutes}min, "
            f"Routes API says {result.duration_s // 60}min ({abs(result.duration_s - sheet_s) / sheet_s:.0%} "
            f"divergence) — sheet Travel value kept, not overridden"
        )
