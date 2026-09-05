"""Standalone tool (like etl/expand_links.py, not part of the main ETL pipeline):
builds a "Pin Verification" worksheet in the master spreadsheet — one row per
parsed stop, sorted so the most suspicious pins (farthest from anything else on
their day) are at the top, so the user can eyeball every geocoded pin by hand.

Usage: python -m etl.verify_pins build [--live]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from urllib.parse import quote

from .loaders import SheetsLoader
from .locate import _haversine_km
from .parse import parse_rows

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
    blank — merge_preserved() fills them in from any prior worksheet content."""
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
    parser = argparse.ArgumentParser(description="Build the 'Pin Verification' worksheet (docs/SCHEMA.md).")
    parser.add_argument("command", choices=["build"])
    parser.add_argument("--sheet-id", help="Falls back to GOOGLE_SHEET_ID env var.")
    parser.add_argument("--worksheet", help="Source itinerary tab. Falls back to WORKSHEET_NAME env var, then 'Itinerary v1'.")
    parser.add_argument(
        "--live", action="store_true",
        help="Actually create/refresh the 'Pin Verification' worksheet. Default is dry run: zero network calls.",
    )
    args = parser.parse_args(argv)

    sheet_id = args.sheet_id or os.environ.get("GOOGLE_SHEET_ID")
    if not sheet_id:
        parser.error("--sheet-id or GOOGLE_SHEET_ID is required")

    result = parse_rows(SheetsLoader(sheet_id, args.worksheet))
    if result.trip is None:
        print(f"Parse failed with {len(result.errors)} error(s)", file=sys.stderr)
        return 1

    worksheet = None
    if args.live:
        worksheet = get_or_create_worksheet(_open_spreadsheet(sheet_id))

    report = build(result.trip, live=args.live, worksheet=worksheet)

    print(f"{'LIVE' if args.live else 'DRY RUN'} — {report.row_count} row(s) across days {report.days}")
    if not args.live:
        print("Nothing written. Pass --live to create/refresh the 'Pin Verification' worksheet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
