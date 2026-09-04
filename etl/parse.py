"""Stage 1 ETL: parse and validate the Itinerary sheet. No network calls.

Column resolution is by header name (normalised: lowercased, trimmed, whitespace
collapsed), never by position — see docs/SCHEMA.md section 1. Row classification is
by row_type; an empty row_type is a warning and the row is skipped, per docs/SCHEMA.md
section 2, so the sheet can be migrated incrementally. A row_type that's present but
not in the enum is a hard error.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from .loaders import RowSource
from .models import Day, Lodging, Stop, Trip, TripMeta

REQUIRED_HEADERS = [
    "day", "date", "location", "travel", "fun time", "plan", "address",
    "how", "zone", "price", "notes", "links", "row_type",
]

ROW_TYPES = {"day_header", "stop", "lodging", "day_end", "blank"}
KINDS = {"poi", "meal", "activity", "lodging", "flight", "transfer"}
TIMINGS = {"fixed", "floating"}
HOWS = {"drive", "walk", "taxi", "train", "shuttle", "plane", "transit"}
# Real compound-mode values seen in the sheet (e.g. "Walk + Aquabus") — normalised to
# the transit catch-all rather than treated as invalid.
HOW_ALIASES = {"bus & walk": "transit", "walk + aquabus": "transit"}
DEFAULT_KIND = "poi"
DEFAULT_TIMING = "floating"

DURATION_RE = re.compile(r"^\d{1,2}:\d{2}$")
DAY_RE = re.compile(r"day\s*(\d+)", re.IGNORECASE)
TIME_RE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")
DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y")


def normalize_header(h: str) -> str:
    return re.sub(r"\s+", " ", h.strip().lower())


def parse_duration(value: str) -> int | None:
    """Return whole minutes for an H:MM / HH:MM string, or None if unparseable."""
    v = value.strip()
    if not DURATION_RE.match(v):
        return None
    h, m = v.split(":")
    if int(m) >= 60:
        return None
    return int(h) * 60 + int(m)


def parse_bool(value: str) -> bool:
    return value.strip().lower() in {"true", "yes", "1"}


def valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(name.strip())
        return True
    except Exception:
        return False


def parse_date(value: str) -> str | None:
    """Parse ISO (YYYY-MM-DD) or AU-locale (D/M/YYYY), return ISO or None."""
    v = value.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_time(value: str) -> str | None:
    """Parse H:MM or H:MM:SS wall-clock, return zero-padded HH:MM or None."""
    v = value.strip()
    if not TIME_RE.match(v):
        return None
    parts = v.split(":")
    h, m = int(parts[0]), int(parts[1])
    if h > 23 or m > 59:
        return None
    return f"{h:02d}:{m:02d}"


def derive_leg(start_location: str | None, end_location: str | None) -> str | None:
    """day.leg is derived from day_header/day_end locations, not read from a leg row
    (the leg row_type is gone — the sheet packed a journey label and a real stop into
    the same row, and there's no way to keep the label without dropping the stop)."""
    if not start_location and not end_location:
        return None
    if not end_location or start_location == end_location:
        return start_location
    if not start_location:
        return end_location
    return f"{start_location} to {end_location}"


@dataclass
class ParseResult:
    trip: Trip | None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    skipped: int = 0


def parse_rows(source: RowSource) -> ParseResult:
    rows = source()
    if not rows:
        return ParseResult(trip=None, errors=["Sheet is empty"])

    header_row, *data_rows = rows
    normalized = [normalize_header(h) for h in header_row]
    index: dict[str, int] = {}
    for i, h in enumerate(normalized):
        index.setdefault(h, i)

    errors: list[str] = []
    warnings: list[str] = []
    counts: dict[str, int] = {}
    skipped = 0

    missing = [h for h in REQUIRED_HEADERS if h not in index]
    if missing:
        for h in missing:
            errors.append(f"Missing required header: {h!r}")
        return ParseResult(trip=None, errors=errors, warnings=warnings, counts=counts, skipped=skipped)

    def cell(row: list[str], name: str) -> str:
        i = index.get(name)
        if i is None or i >= len(row):
            return ""
        return row[i].strip()

    days: list[Day] = []
    current_day: dict | None = None
    seq = 0

    def parse_stop_content(row: list[str], row_num: int) -> Stop | None:
        """Build a Stop from a row's stop-shaped columns (Travel, Fun Time, Plan,
        Address, How, Zone, timing, fixed_time, ...). Used for stop rows, and for
        day_header/day_end rows that also carry a Plan — the sheet packs both a
        structural purpose and a real stop into the same row on some days."""
        missing_fields: list[str] = []

        plan = cell(row, "plan")
        if not plan:
            missing_fields.append("Plan")

        kind_raw = cell(row, "kind")
        kind = kind_raw.lower() or DEFAULT_KIND
        if kind not in KINDS:
            errors.append(f"Row {row_num}, column 'kind': {kind_raw!r} not in {sorted(KINDS)}")
            missing_fields.append("kind")

        timing_raw = cell(row, "timing")
        timing = timing_raw.lower() or DEFAULT_TIMING
        if timing not in TIMINGS:
            errors.append(f"Row {row_num}, column 'timing': {timing_raw!r} not in {sorted(TIMINGS)}")
            missing_fields.append("timing")

        travel_raw = cell(row, "travel")
        travel_minutes = None
        if not travel_raw:
            missing_fields.append("Travel")
        else:
            travel_minutes = parse_duration(travel_raw)
            if travel_minutes is None:
                errors.append(f"Row {row_num}, column 'Travel': {travel_raw!r} is not a valid H:MM duration")
                missing_fields.append("Travel")

        fun_raw = cell(row, "fun time")
        dwell_minutes = None
        if not fun_raw:
            missing_fields.append("Fun Time")
        else:
            dwell_minutes = parse_duration(fun_raw)
            if dwell_minutes is None:
                errors.append(f"Row {row_num}, column 'Fun Time': {fun_raw!r} is not a valid H:MM duration")
                missing_fields.append("Fun Time")

        zone = cell(row, "zone")
        if not zone:
            missing_fields.append("Zone")
        elif not valid_timezone(zone):
            errors.append(f"Row {row_num}, column 'Zone': {zone!r} is not a valid IANA timezone")
            missing_fields.append("Zone")

        how_raw = cell(row, "how")
        how: str | None = how_raw.lower()
        how = HOW_ALIASES.get(how, how)
        if not how:
            warnings.append(f"Row {row_num}: How is blank — no deep link mode for this stop")
            how = None
        elif how not in HOWS:
            errors.append(f"Row {row_num}, column 'How': {how_raw!r} not in {sorted(HOWS)}")
            how = None

        fixed_time_raw = cell(row, "fixed_time")
        fixed_time = None
        if fixed_time_raw:
            fixed_time = parse_time(fixed_time_raw)
            if fixed_time is None:
                errors.append(f"Row {row_num}, column 'fixed_time': {fixed_time_raw!r} is not a valid HH:MM time")
                missing_fields.append("fixed_time")
        if timing == "fixed" and not fixed_time_raw:
            missing_fields.append("fixed_time (required when timing=fixed)")

        if missing_fields:
            errors.append(f"Row {row_num}: stop missing/invalid required field(s): {', '.join(missing_fields)}")
            return None

        if dwell_minutes == 0 and timing == "floating":
            warnings.append(f"Row {row_num}: Fun Time = 0 with timing = floating")

        day_offset_raw = cell(row, "day_offset")
        day_offset = 0
        if day_offset_raw:
            try:
                day_offset = int(day_offset_raw)
            except ValueError:
                errors.append(f"Row {row_num}, column 'day_offset': {day_offset_raw!r} is not an integer")
                return None

        arrive_before_raw = cell(row, "arrive_before")
        arrive_before = None
        if arrive_before_raw:
            arrive_before = parse_duration(arrive_before_raw)
            if arrive_before is None:
                errors.append(
                    f"Row {row_num}, column 'arrive_before': {arrive_before_raw!r} is not a valid H:MM duration"
                )
                return None

        documents_raw = cell(row, "documents")
        links_raw = cell(row, "links")

        try:
            return Stop(
                id=f"d{current_day['day']:02d}-s{seq:02d}",
                seq=seq,
                title=plan,
                kind=kind,
                timezone=zone,
                day_offset=day_offset,
                how=how,
                travel_minutes=travel_minutes,
                dwell_minutes=dwell_minutes,
                timing=timing,
                fixed_time=fixed_time,
                arrive_before=arrive_before,
                daylight_required=parse_bool(cell(row, "daylight_required")),
                notes=cell(row, "notes") or None,
                price=cell(row, "price") or None,
                links=[links_raw] if links_raw else [],
                documents=[d.strip() for d in documents_raw.split(",") if d.strip()] or None,
            )
        except ValidationError as e:
            errors.append(f"Row {row_num}: {e}")
            return None

    def flush_day() -> None:
        nonlocal current_day
        if current_day is None:
            return
        cd = current_day
        current_day = None

        timezone = cd["timezone"]
        if not timezone and cd["stops"]:
            timezone = cd["stops"][0].timezone
        if not timezone:
            errors.append(f"Day {cd['day']}: could not determine timezone (no Zone on day_header and no stops)")
            timezone = ""

        if not cd["anchor_time"]:
            warnings.append(f"Day {cd['day']}: no anchor_time (fixed_time empty on day_header)")

        # Downgraded to a warning: the schedule engine that actually depends on a
        # constraint doesn't exist yet, and the sheet has no fixed_time column to
        # populate one with — as an error this fails every day by construction. Revert
        # to an error once the schedule engine lands and fixed_time is a real column
        # (docs/SCHEMA.md §7).
        has_fixed = any(s.timing == "fixed" for s in cd["stops"])
        has_checkin = cd["lodging"] is not None and bool(cd["lodging"].check_in)
        if not has_fixed and not has_checkin:
            warnings.append(f"Day {cd['day']}: no constraint — nothing fixed and no lodging check-in")

        try:
            days.append(Day(
                day=cd["day"],
                date=cd["date"],
                leg=derive_leg(cd["start_location"], cd["end_location"]),
                start_location=cd["start_location"],
                end_location=cd["end_location"],
                timezone=timezone,
                anchor_time=cd["anchor_time"],
                lodging=cd["lodging"],
                stops=cd["stops"],
            ))
        except ValidationError as e:
            errors.append(f"Day {cd['day']}: {e}")

    for row_num, row in enumerate(data_rows, start=2):
        if not any(c.strip() for c in row):
            continue

        row_type_raw = cell(row, "row_type")
        row_type = row_type_raw.lower()

        if not row_type:
            warnings.append(f"Row {row_num}: empty row_type — skipped")
            skipped += 1
            continue

        if row_type not in ROW_TYPES:
            errors.append(f"Row {row_num}: row_type {row_type_raw!r} not in {sorted(ROW_TYPES)}")
            continue

        counts[row_type] = counts.get(row_type, 0) + 1

        # blank is the only row type left with no legitimate reason to carry a stop —
        # day_header and day_end can, and now do (see below); a blank row with a Plan
        # is still genuinely suspicious.
        if row_type == "blank":
            plan_val = cell(row, "plan")
            if plan_val:
                warnings.append(
                    f"Row {row_num}: row_type={row_type} but Plan is non-empty "
                    f"({plan_val!r}) — possible misclassification"
                )
            continue

        if row_type == "day_header":
            flush_day()
            seq = 0
            day_field = cell(row, "day")
            m = DAY_RE.search(day_field)
            if not m:
                errors.append(f"Row {row_num}, column 'Day': {day_field!r} doesn't match 'Day N'")
                continue
            day_num = int(m.group(1))

            date_raw = cell(row, "date")
            date_val = None
            if not date_raw:
                errors.append(f"Row {row_num}, column 'Date': day_header missing Date")
            else:
                date_val = parse_date(date_raw)
                if date_val is None:
                    errors.append(
                        f"Row {row_num}, column 'Date': {date_raw!r} is not a valid date "
                        "(expected YYYY-MM-DD or D/M/YYYY)"
                    )

            zone = cell(row, "zone")
            if zone and not valid_timezone(zone):
                errors.append(f"Row {row_num}, column 'Zone': {zone!r} is not a valid IANA timezone")

            anchor_raw = cell(row, "fixed_time")
            anchor = None
            if anchor_raw:
                anchor = parse_time(anchor_raw)
                if anchor is None:
                    errors.append(f"Row {row_num}, column 'fixed_time': {anchor_raw!r} is not a valid HH:MM time")

            current_day = {
                "day": day_num,
                "date": date_val or "",
                "start_location": cell(row, "location") or None,
                "end_location": None,
                "timezone": zone or None,
                "anchor_time": anchor or None,
                "lodging": None,
                "stops": [],
            }

            # Some day_header rows also carry a real stop (e.g. a sunrise shoot
            # timed to the day's own fixed_time) — Location holds the start
            # location, Plan holds the stop. Read it exactly like a stop row.
            if cell(row, "plan"):
                seq += 1
                first_stop = parse_stop_content(row, row_num)
                if first_stop is not None:
                    current_day["stops"].append(first_stop)
            continue

        if current_day is None:
            errors.append(f"Row {row_num}: {row_type} row appears before any day_header")
            continue

        stray_date = cell(row, "date")
        if stray_date and parse_date(stray_date) is None:
            warnings.append(f"Row {row_num}: unexpected value in Date column: {stray_date!r} (ignored)")

        if row_type == "day_end":
            current_day["end_location"] = cell(row, "location") or None
            if not current_day["timezone"]:
                zone = cell(row, "zone")
                if zone:
                    current_day["timezone"] = zone

            # Same dual-purpose pattern as day_header, at the end of the day.
            if cell(row, "plan"):
                seq += 1
                last_stop = parse_stop_content(row, row_num)
                if last_stop is not None:
                    current_day["stops"].append(last_stop)
            continue

        if row_type == "lodging":
            name = cell(row, "plan")
            if not name:
                errors.append(f"Row {row_num}, column 'Plan': lodging row missing hotel name")
            check_in_raw = cell(row, "fixed_time")
            check_in = None
            if check_in_raw:
                check_in = parse_time(check_in_raw)
                if check_in is None:
                    errors.append(
                        f"Row {row_num}, column 'fixed_time': {check_in_raw!r} is not a valid HH:MM time"
                    )
            current_day["lodging"] = Lodging(
                name=name or "",
                check_in=check_in,
                notes=cell(row, "notes") or None,
            )
            continue

        # row_type == "stop"
        seq += 1
        new_stop = parse_stop_content(row, row_num)
        if new_stop is not None:
            current_day["stops"].append(new_stop)

    flush_day()

    if not days:
        errors.append("No day_header rows found")

    day_numbers = [d.day for d in days]
    for a, b in zip(day_numbers, day_numbers[1:]):
        if b != a + 1:
            errors.append(f"Day numbers not contiguous: Day {a} followed by Day {b}")

    trip = None
    if not errors and days:
        trip = Trip(trip=TripMeta(start_date=days[0].date, end_date=days[-1].date), days=days)

    return ParseResult(trip=trip, errors=errors, warnings=warnings, counts=counts, skipped=skipped)
