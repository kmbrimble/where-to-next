"""Stage 1 ETL: parse and validate the Itinerary sheet. No network calls.

Column resolution is by header name (normalised: lowercased, trimmed, whitespace
collapsed), never by position — see docs/SCHEMA.md section 1. Row classification is
by row_type; an empty row_type is a warning and the row is skipped (docs/SCHEMA.md
section 2 says reject, but the task explicitly overrides that to warn+skip so the
sheet can be migrated incrementally — see PR description).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from .loaders import RowSource
from .models import Day, Lodging, Stop, Trip, TripMeta

REQUIRED_HEADERS = [
    "day", "date", "location", "travel", "fun time", "plan", "address",
    "how", "zone", "price", "notes", "links", "row_type", "kind", "timing",
]

ROW_TYPES = {"day_header", "leg", "drive_total", "stop", "lodging", "day_end", "blank"}
KINDS = {"poi", "meal", "activity", "lodging", "flight", "transfer"}
TIMINGS = {"fixed", "floating"}
HOWS = {"drive", "walk", "taxi", "shuttle", "plane"}

DURATION_RE = re.compile(r"^\d{1,2}:\d{2}$")
DAY_RE = re.compile(r"day\s*(\d+)", re.IGNORECASE)
DATE_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATE_DMY_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")


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


def valid_date(value: str) -> bool:
    v = value.strip()
    return bool(DATE_ISO_RE.match(v) or DATE_DMY_RE.match(v))


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

        has_fixed = any(s.timing == "fixed" for s in cd["stops"])
        has_checkin = cd["lodging"] is not None and bool(cd["lodging"].check_in)
        if not has_fixed and not has_checkin:
            errors.append(f"Day {cd['day']}: no constraint — nothing fixed and no lodging check-in")

        try:
            days.append(Day(
                day=cd["day"],
                date=cd["date"],
                leg=cd["leg"],
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

        if row_type == "blank":
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

            date_val = cell(row, "date")
            if not date_val:
                errors.append(f"Row {row_num}, column 'Date': day_header missing Date")
            elif not valid_date(date_val):
                errors.append(
                    f"Row {row_num}, column 'Date': {date_val!r} is not a valid date "
                    "(expected YYYY-MM-DD or D/M/YYYY)"
                )

            zone = cell(row, "zone")
            if zone and not valid_timezone(zone):
                errors.append(f"Row {row_num}, column 'Zone': {zone!r} is not a valid IANA timezone")

            anchor = cell(row, "fixed_time")
            current_day = {
                "day": day_num,
                "date": date_val,
                "leg": None,
                "start_location": cell(row, "location") or None,
                "end_location": None,
                "timezone": zone or None,
                "anchor_time": anchor or None,
                "lodging": None,
                "stops": [],
            }
            continue

        if current_day is None:
            errors.append(f"Row {row_num}: {row_type} row appears before any day_header")
            continue

        if row_type == "leg":
            current_day["leg"] = cell(row, "location") or None
            continue

        if row_type == "drive_total":
            continue  # recomputed at render time, content ignored

        if row_type == "day_end":
            current_day["end_location"] = cell(row, "location") or None
            if not current_day["timezone"]:
                zone = cell(row, "zone")
                if zone:
                    current_day["timezone"] = zone
            continue

        if row_type == "lodging":
            name = cell(row, "plan")
            if not name:
                errors.append(f"Row {row_num}, column 'Plan': lodging row missing hotel name")
            check_in = cell(row, "fixed_time")
            current_day["lodging"] = Lodging(
                name=name or "",
                check_in=check_in or None,
                notes=cell(row, "notes") or None,
            )
            continue

        # row_type == "stop"
        seq += 1
        missing_fields: list[str] = []

        plan = cell(row, "plan")
        if not plan:
            missing_fields.append("Plan")

        kind_raw = cell(row, "kind")
        kind = kind_raw.lower()
        if not kind:
            missing_fields.append("kind")
        elif kind not in KINDS:
            errors.append(f"Row {row_num}, column 'kind': {kind_raw!r} not in {sorted(KINDS)}")
            missing_fields.append("kind")

        timing_raw = cell(row, "timing")
        timing = timing_raw.lower()
        if not timing:
            missing_fields.append("timing")
        elif timing not in TIMINGS:
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
        how = how_raw.lower()
        if how and how not in HOWS:
            errors.append(f"Row {row_num}, column 'How': {how_raw!r} not in {sorted(HOWS)}")
            how = ""

        fixed_time = cell(row, "fixed_time")
        if timing == "fixed" and not fixed_time:
            missing_fields.append("fixed_time (required when timing=fixed)")

        if missing_fields:
            errors.append(f"Row {row_num}: stop missing/invalid required field(s): {', '.join(missing_fields)}")
            continue

        if dwell_minutes == 0 and timing == "floating":
            warnings.append(f"Row {row_num}: Fun Time = 0 with timing = floating")

        day_offset_raw = cell(row, "day_offset")
        day_offset = 0
        if day_offset_raw:
            try:
                day_offset = int(day_offset_raw)
            except ValueError:
                errors.append(f"Row {row_num}, column 'day_offset': {day_offset_raw!r} is not an integer")
                continue

        arrive_before_raw = cell(row, "arrive_before")
        arrive_before = None
        if arrive_before_raw:
            arrive_before = parse_duration(arrive_before_raw)
            if arrive_before is None:
                errors.append(
                    f"Row {row_num}, column 'arrive_before': {arrive_before_raw!r} is not a valid H:MM duration"
                )
                continue

        documents_raw = cell(row, "documents")
        links_raw = cell(row, "links")

        try:
            stop = Stop(
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
                fixed_time=fixed_time or None,
                arrive_before=arrive_before,
                daylight_required=parse_bool(cell(row, "daylight_required")),
                notes=cell(row, "notes") or None,
                price=cell(row, "price") or None,
                links=[links_raw] if links_raw else [],
                documents=[d.strip() for d in documents_raw.split(",") if d.strip()] or None,
            )
        except ValidationError as e:
            errors.append(f"Row {row_num}: {e}")
            continue

        current_day["stops"].append(stop)

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
