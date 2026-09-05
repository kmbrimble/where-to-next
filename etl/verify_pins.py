"""Standalone tool (like etl/expand_links.py, not part of the main ETL pipeline):
builds a "Pin Verification" worksheet in the master spreadsheet — one row per
parsed stop, sorted so the most suspicious pins (farthest from anything else on
their day) are at the top, so the user can eyeball every geocoded pin by hand —
and applies the user's hand-typed corrections back into the Itinerary tab.

Usage: python -m etl.verify_pins build [--live]
       python -m etl.verify_pins apply [--live]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import quote

from .loaders import SheetsLoader, get_worksheet
from .locate import TIMEZONE_BOUNDING_BOXES, _haversine_km
from .parse import normalize_header, parse_rows
from .writeback import _col_letter

WORKSHEET_NAME = "Pin Verification"

HEADER = [
    "id", "day", "seq", "plan", "address", "resolved_from", "current_coords",
    "km_to_nearest", "check_pin", "search_again", "corrected_coords", "applied",
]

# Columns the user (or a later apply step) writes into by hand — a refresh must
# never overwrite these, or it destroys in-progress manual verification work.
PRESERVED_COLUMNS = ("corrected_coords", "applied")


def _check_pin_formula(lat: float, lng: float) -> str:
    url = f"https://www.google.com/maps/search/?api=1&query={lat},{lng}"
    return f'=HYPERLINK("{url}","check pin")'


def _search_again_formula(plan: str, address: str | None) -> str:
    query = f"{plan} {address or ''}".strip()
    url = "https://www.google.com/maps/search/?api=1&query=" + quote(query, safe="")
    return f'=HYPERLINK("{url}","search again")'


def compute_rows(trip) -> list[dict]:
    """One dict per stop, key = HEADER name. corrected_coords/applied start
    blank — merge_preserved() fills them in from any prior worksheet content.

    Raises RuntimeError if any stop only has a synthetic placeholder id
    (has_real_id=False) rather than one actually written to the sheet — such an
    id can never be found by `apply`'s fresh re-read, which silently blocked a
    118-row correction batch on 2 rows before this check existed. Run the main
    ETL --live (with write-back) first to assign real ids to every row.
    """
    synthetic = [
        f"Row {s.row_num} ({s.title!r}, id={s.id!r})"
        for day in trip.days for s in day.stops if not s.has_real_id
    ]
    if synthetic:
        raise RuntimeError(
            f"{len(synthetic)} stop(s) have a synthetic placeholder id, not a real sheet id — "
            f"run the main ETL --live first to assign ids: " + "; ".join(synthetic)
        )

    rows: list[dict] = []
    for day in trip.days:
        located = [s for s in day.stops if s.lat is not None and s.lng is not None]
        for stop in day.stops:
            nearest_km = None
            if stop.lat is not None and stop.lng is not None:
                for other in located:
                    if other is stop:
                        continue
                    d = _haversine_km(stop.lat, stop.lng, other.lat, other.lng)
                    if nearest_km is None or d < nearest_km:
                        nearest_km = d

            current_coords = f"{stop.lat}, {stop.lng}" if stop.lat is not None else ""
            check_pin = _check_pin_formula(stop.lat, stop.lng) if stop.lat is not None else ""

            rows.append({
                "id": stop.id,
                "day": day.day,
                "seq": stop.seq,
                "plan": stop.title,
                "address": stop.address or "",
                "resolved_from": stop.resolved_from or "",
                "current_coords": current_coords,
                "km_to_nearest": round(nearest_km, 2) if nearest_km is not None else "",
                "check_pin": check_pin,
                "search_again": _search_again_formula(stop.title, stop.address),
                "corrected_coords": "",
                "applied": "",
            })

    # Most suspicious (farthest from any same-day neighbour) first; blank
    # km_to_nearest (single-stop days, unresolved stops) sorts last.
    rows.sort(key=lambda r: (r["km_to_nearest"] == "", -(r["km_to_nearest"] or 0)))
    return rows


def merge_preserved(rows: list[dict], existing_values: list[list[str]]) -> list[dict]:
    """existing_values: raw worksheet rows (header first), from a prior build.
    Carries corrected_coords/applied forward by id — a refresh must never lose
    verification work the user already did."""
    if not existing_values:
        return rows
    existing_header = existing_values[0]
    try:
        id_col = existing_header.index("id")
        preserved_cols = {name: existing_header.index(name) for name in PRESERVED_COLUMNS if name in existing_header}
    except ValueError:
        return rows

    by_id = {}
    for raw_row in existing_values[1:]:
        if id_col < len(raw_row):
            by_id[raw_row[id_col]] = raw_row

    for row in rows:
        old = by_id.get(row["id"])
        if not old:
            continue
        for name, col in preserved_cols.items():
            if col < len(old) and old[col]:
                row[name] = old[col]
    return rows


@dataclass
class VerifyPinsReport:
    row_count: int
    days: list[int]
    would_write: bool


def build(trip, *, live: bool, worksheet=None) -> VerifyPinsReport:
    """worksheet: a gspread-like Worksheet — needs .get_all_values(), .clear(),
    .update(). None is fine under dry run (no network at all, matching the rest
    of this ETL's dry-run contract)."""
    rows = compute_rows(trip)

    if live:
        existing = worksheet.get_all_values()
        rows = merge_preserved(rows, existing)
        worksheet.clear()
        values = [HEADER] + [[row[col] for col in HEADER] for row in rows]
        worksheet.update(values, value_input_option="USER_ENTERED")

    days = sorted({row["day"] for row in rows})
    return VerifyPinsReport(row_count=len(rows), days=days, would_write=not live)


# Tolerates quotes, whitespace, a degree symbol, and a leading/trailing paren —
# whatever shape a hand-pasted "right click -> coordinates" value comes in.
CORRECTED_COORD_RE = re.compile(
    r"^\(?\s*([+-]?\d+(?:\.\d+)?)\s*°?\s*,\s*([+-]?\d+(?:\.\d+)?)\s*°?\s*\)?$"
)


def parse_corrected_coords(raw: str) -> tuple[float, float]:
    """Raises ValueError with a message specific enough to act on."""
    v = raw.strip().strip("\"'").strip()
    if re.match(r"^https?://", v, re.IGNORECASE) or "maps.app.goo.gl" in v or "goo.gl" in v:
        raise ValueError(
            f"{raw!r} looks like a Maps URL, not coordinates — paste the 'lat, lng' "
            f"pair from a right-click, not the link"
        )
    m = CORRECTED_COORD_RE.match(v)
    if not m:
        raise ValueError(f"{raw!r} doesn't parse as a 'lat, lng' pair")
    lat, lng = float(m.group(1)), float(m.group(2))
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise ValueError(f"{raw!r} is out of range for latitude/longitude")
    return lat, lng


def trip_bounding_box(trip) -> tuple[float, float, float, float] | None:
    """Union of the known timezone boxes touched by this trip. None if none of
    the trip's timezones have a known box (skip the check rather than reject
    everything)."""
    zones = {day.timezone for day in trip.days} | {s.timezone for day in trip.days for s in day.stops}
    boxes = [TIMEZONE_BOUNDING_BOXES[z] for z in zones if z in TIMEZONE_BOUNDING_BOXES]
    if not boxes:
        return None
    return (
        min(b[0] for b in boxes), max(b[1] for b in boxes),
        min(b[2] for b in boxes), max(b[3] for b in boxes),
    )


def rows_from_values(values: list[list[str]]) -> list[dict]:
    """Raw worksheet rows (header first) -> list of {column_name: value} dicts."""
    if not values:
        return []
    header = values[0]
    return [{name: (raw[i] if i < len(raw) else "") for i, name in enumerate(header)} for raw in values[1:]]


@dataclass
class ApplyReport:
    would_apply: list = field(default_factory=list)
    rejected: list = field(default_factory=list)
    skipped_already_applied: list = field(default_factory=list)
    applied: int = 0
    aborted: bool = False
    abort_reason: str | None = None


def validate_pin_rows(pin_rows: list[dict], trip) -> tuple[list[tuple[str, float, float]], list[str], list[str]]:
    """Returns (candidates, rejected reasons, skipped-already-applied notes).
    candidates: list of (id, lat, lng). Pure — no network, no mutation."""
    id_to_stop = {s.id: s for day in trip.days for s in day.stops}
    box = trip_bounding_box(trip)

    candidates: list[tuple[str, float, float]] = []
    rejected: list[str] = []
    skipped: list[str] = []

    for row in pin_rows:
        corrected = (row.get("corrected_coords") or "").strip()
        if not corrected:
            continue
        applied_val = (row.get("applied") or "").strip()
        row_id = row.get("id", "")
        if applied_val:
            skipped.append(f"id={row_id}: already applied at {applied_val}")
            continue

        try:
            lat, lng = parse_corrected_coords(corrected)
        except ValueError as e:
            rejected.append(f"id={row_id}: {e}")
            continue

        if box is not None:
            lat_min, lat_max, lng_min, lng_max = box
            if not (lat_min <= lat <= lat_max and lng_min <= lng <= lng_max):
                rejected.append(f"id={row_id}: ({lat}, {lng}) is outside the trip's bounding box")
                continue

        if row_id not in id_to_stop:
            rejected.append(f"id={row_id}: no matching stop found in the parsed Itinerary")
            continue

        candidates.append((row_id, lat, lng))

    return candidates, rejected, skipped


def apply_corrections(trip, pin_rows: list[dict], *, live: bool, itinerary_ws=None, pin_ws=None, now: str | None = None) -> ApplyReport:
    """VALIDATE FIRST, all-or-nothing: if any row fails validation, nothing is
    written — a single typo must never leave the sheet half-applied."""
    candidates, rejected, skipped = validate_pin_rows(pin_rows, trip)
    would_apply = [f"id={rid}: Address -> \"{lat}, {lng}\"; lat/lng/place_id cleared" for rid, lat, lng in candidates]

    if rejected:
        return ApplyReport(
            would_apply=would_apply, rejected=rejected, skipped_already_applied=skipped,
            aborted=True, abort_reason=f"{len(rejected)} row(s) failed validation — nothing written",
        )

    if not live or not candidates:
        return ApplyReport(would_apply=would_apply, rejected=rejected, skipped_already_applied=skipped)

    timestamp = now or datetime.now(timezone.utc).isoformat()

    # Re-read row positions immediately before writing — the sheet may be open
    # in a browser and rows may have moved since the Pin Verification build.
    fresh_rows = itinerary_ws.get_all_values()
    header, data_rows = fresh_rows[0], fresh_rows[1:]
    index: dict[str, int] = {}
    for i, h in enumerate(header):
        index.setdefault(normalize_header(h), i)

    id_idx = index["id"]
    id_to_row: dict[str, int] = {}
    for offset, row in enumerate(data_rows):
        rid = row[id_idx] if id_idx < len(row) else ""
        if rid:
            id_to_row[rid] = offset + 2

    itinerary_updates: list[dict] = []
    applied_ids: list[str] = []
    for rid, lat, lng in candidates:
        row_num = id_to_row.get(rid)
        if row_num is None:
            rejected.append(f"id={rid}: id no longer found in the Itinerary tab on re-read")
            continue
        itinerary_updates.append({"range": f"{_col_letter(index['address'])}{row_num}", "values": [[f"{lat}, {lng}"]]})
        for col in ("lat", "lng", "place_id"):
            itinerary_updates.append({"range": f"{_col_letter(index[col])}{row_num}", "values": [[""]]})
        applied_ids.append(rid)

    if rejected:
        # A row vanished between validation and write (sheet edited concurrently)
        # — same all-or-nothing guarantee applies to the re-read, not just the
        # first validation pass.
        return ApplyReport(
            would_apply=would_apply, rejected=rejected, skipped_already_applied=skipped,
            aborted=True, abort_reason="id(s) not found on re-read — nothing written",
        )

    itinerary_ws.batch_update(itinerary_updates)

    pin_fresh = pin_ws.get_all_values()
    pin_header, pin_data = pin_fresh[0], pin_fresh[1:]
    pin_id_idx = pin_header.index("id")
    applied_col_idx = pin_header.index("applied")
    pin_updates = [
        {"range": f"{_col_letter(applied_col_idx)}{offset + 2}", "values": [[timestamp]]}
        for offset, row in enumerate(pin_data)
        if (row[pin_id_idx] if pin_id_idx < len(row) else "") in applied_ids
    ]
    if pin_updates:
        pin_ws.batch_update(pin_updates)

    return ApplyReport(
        would_apply=would_apply, rejected=rejected, skipped_already_applied=skipped, applied=len(applied_ids),
    )


def get_or_create_worksheet(spreadsheet):
    import gspread

    try:
        return spreadsheet.worksheet(WORKSHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=WORKSHEET_NAME, rows=1000, cols=len(HEADER))


def _open_spreadsheet(sheet_id: str):
    import gspread

    key = json.loads(os.environ["GOOGLE_SHEETS_SA_KEY"])
    client = gspread.service_account_from_dict(key)
    return client.open_by_key(sheet_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build/apply the 'Pin Verification' worksheet (docs/SCHEMA.md).")
    parser.add_argument("command", choices=["build", "apply"])
    parser.add_argument("--sheet-id", help="Falls back to GOOGLE_SHEET_ID env var.")
    parser.add_argument("--worksheet", help="Source itinerary tab. Falls back to WORKSHEET_NAME env var, then 'Itinerary v1'.")
    parser.add_argument(
        "--live", action="store_true",
        help="Actually write. Default is dry run: reports what would happen, writes nothing.",
    )
    args = parser.parse_args(argv)

    sheet_id = args.sheet_id or os.environ.get("GOOGLE_SHEET_ID")
    if not sheet_id:
        parser.error("--sheet-id or GOOGLE_SHEET_ID is required")

    result = parse_rows(SheetsLoader(sheet_id, args.worksheet))
    if result.trip is None:
        print(f"Parse failed with {len(result.errors)} error(s)", file=sys.stderr)
        return 1

    if args.command == "build":
        worksheet = None
        if args.live:
            worksheet = get_or_create_worksheet(_open_spreadsheet(sheet_id))

        try:
            report = build(result.trip, live=args.live, worksheet=worksheet)
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            return 1

        print(f"{'LIVE' if args.live else 'DRY RUN'} — {report.row_count} row(s) across days {report.days}")
        if not args.live:
            print("Nothing written. Pass --live to create/refresh the 'Pin Verification' worksheet.")
        return 0

    # apply
    spreadsheet = _open_spreadsheet(sheet_id)
    pin_ws = spreadsheet.worksheet(WORKSHEET_NAME)  # must already exist — run `build` first
    pin_rows = rows_from_values(pin_ws.get_all_values())

    itinerary_ws = get_worksheet(sheet_id, args.worksheet) if args.live else None
    report = apply_corrections(
        result.trip, pin_rows, live=args.live, itinerary_ws=itinerary_ws, pin_ws=pin_ws if args.live else None,
    )

    print(f"{'LIVE' if args.live else 'DRY RUN'} — {len(report.would_apply)} row(s) to apply, "
          f"{len(report.skipped_already_applied)} already applied, {len(report.rejected)} rejected")
    for r in report.rejected:
        print(f"REJECTED: {r}")
    if report.aborted:
        print(f"ABORTED: {report.abort_reason}", file=sys.stderr)
        return 1
    if args.live:
        print(f"Applied {report.applied} row(s).")
    else:
        for w in report.would_apply:
            print(f"would apply: {w}")
        print("Nothing written. Pass --live to apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
