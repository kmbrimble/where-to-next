"""Sunrise/sunset computation for stage 3. Pure local computation via astral — no
network calls. See docs/SCHEMA.md §8/§9 for the shape and the endpoint rule this
implements.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as date_cls

from astral import LocationInfo
from astral.sun import sun


@dataclass
class DaylightReport:
    warnings: list = field(default_factory=list)


def _sun_time(lat: float, lng: float, iso_date: str, timezone: str, which: str) -> str | None:
    """which is "sunrise" or "sunset". Returns a local HH:MM string, or None if
    astral can't compute it for this lat/lng/date (polar day/night — not relevant
    to this trip's latitudes, but astral raises rather than returning None)."""
    loc = LocationInfo(latitude=lat, longitude=lng)
    d = date_cls.fromisoformat(iso_date)
    try:
        result = sun(loc.observer, date=d, tzinfo=timezone)
    except ValueError:
        return None
    return result[which].strftime("%H:%M")


def compute_daylight(trip) -> DaylightReport:
    """Mutates trip.days[*].sunrise/sunset (+ *_location) and any daylight_required
    stop's sunset, in place.

    Sunrise comes from the FIRST stop of the day with resolved coordinates, in
    THAT STOP'S OWN timezone. Sunset comes from the LAST such stop, in ITS OWN
    timezone. You see sunrise where you wake up and sunset where you end up — on
    a day that doesn't move, both endpoints are the same stop and nothing changes.
    Using the day's single declared timezone for both (the old rule) put a real
    place's sun time on a different place's clock on any day that crosses a
    timezone boundary.
    """
    report = DaylightReport()

    for day in trip.days:
        with_coords = [s for s in day.stops if s.lat is not None and s.lng is not None]
        if not with_coords:
            report.warnings.append(f"Day {day.day}: no stop has resolved coordinates — sunrise/sunset left null")
        else:
            first, last = with_coords[0], with_coords[-1]

            sunrise = _sun_time(first.lat, first.lng, day.date, first.timezone, "sunrise")
            if sunrise is None:
                report.warnings.append(
                    f"Day {day.day}: sun never rises at {first.title!r}'s coordinates "
                    f"on {day.date} — sunrise left null"
                )
            else:
                day.sunrise = sunrise
                day.sunrise_location = f"{first.title} ({first.timezone})"

            sunset = _sun_time(last.lat, last.lng, day.date, last.timezone, "sunset")
            if sunset is None:
                report.warnings.append(
                    f"Day {day.day}: sun never sets at {last.title!r}'s coordinates "
                    f"on {day.date} — sunset left null"
                )
            else:
                day.sunset = sunset
                day.sunset_location = f"{last.title} ({last.timezone})"

            if sunrise is not None and sunset is not None and first.timezone != last.timezone:
                report.warnings.append(
                    f"Day {day.day}: sunrise computed in {first.timezone} ({first.title!r}), "
                    f"sunset in {last.timezone} ({last.title!r}) — day crosses a timezone"
                )

        for stop in day.stops:
            if not stop.daylight_required:
                continue
            if stop.lat is None or stop.lng is None:
                report.warnings.append(
                    f"Row {stop.row_num}: {stop.title!r} has daylight_required but no resolved "
                    f"coordinates — sunset left null"
                )
                continue
            sunset = _sun_time(stop.lat, stop.lng, day.date, stop.timezone, "sunset")
            if sunset is None:
                report.warnings.append(
                    f"Row {stop.row_num}: {stop.title!r} — sun never sets at its coordinates "
                    f"on {day.date} — sunset left null"
                )
            else:
                stop.sunset = sunset

    return report
