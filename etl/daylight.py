"""Sunrise/sunset computation for stage 3. Pure local computation via astral — no
network calls. See docs/SCHEMA.md §8/§9 for the shape and the "primary coordinates"
rule this implements.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as date_cls

from astral import LocationInfo
from astral.sun import sun


@dataclass
class DaylightReport:
    warnings: list = field(default_factory=list)


def _sun_times(lat: float, lng: float, iso_date: str, timezone: str) -> tuple[str, str] | None:
    """Returns (sunrise, sunset) as local HH:MM strings, or None if astral can't
    compute them for this lat/lng/date (polar day/night — not relevant to this
    trip's latitudes, but astral raises rather than returning None)."""
    loc = LocationInfo(latitude=lat, longitude=lng)
    d = date_cls.fromisoformat(iso_date)
    try:
        result = sun(loc.observer, date=d, tzinfo=timezone)
    except ValueError:
        return None
    return result["sunrise"].strftime("%H:%M"), result["sunset"].strftime("%H:%M")


def compute_daylight(trip) -> DaylightReport:
    """Mutates trip.days[*].sunrise/sunset and any daylight_required stop's sunset,
    in place. Primary coordinates for a day = the first stop of that day with
    resolved lat/lng (in stop order); if none, sunrise/sunset stay null and a
    warning is logged. Always uses the day's own declared timezone (from
    day_header), never a stop's, since a day can span a timezone boundary.
    """
    report = DaylightReport()

    for day in trip.days:
        primary = next((s for s in day.stops if s.lat is not None and s.lng is not None), None)
        if primary is None:
            report.warnings.append(f"Day {day.day}: no stop has resolved coordinates — sunrise/sunset left null")
        else:
            times = _sun_times(primary.lat, primary.lng, day.date, day.timezone)
            if times is None:
                report.warnings.append(
                    f"Day {day.day}: sun never reaches the required angle at "
                    f"({primary.lat}, {primary.lng}) on {day.date} — sunrise/sunset left null"
                )
            else:
                day.sunrise, day.sunset = times

        for stop in day.stops:
            if not stop.daylight_required:
                continue
            if stop.lat is None or stop.lng is None:
                report.warnings.append(
                    f"Row {stop.row_num}: {stop.title!r} has daylight_required but no resolved "
                    f"coordinates — sunset left null"
                )
                continue
            times = _sun_times(stop.lat, stop.lng, day.date, day.timezone)
            if times is None:
                report.warnings.append(
                    f"Row {stop.row_num}: {stop.title!r} — sun never reaches the required angle "
                    f"at its coordinates on {day.date} — sunset left null"
                )
            else:
                stop.sunset = times[1]

    return report
