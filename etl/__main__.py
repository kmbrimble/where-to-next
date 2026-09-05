"""CLI entry point: python -m etl --csv path/to.csv [--out-dir DIR]"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from .geocode import GeocodeClient, RequestBudget
from .loaders import CsvLoader, SheetsLoader, get_worksheet
from .locate import resolve_locations
from .parse import parse_rows
from .report import render_report
from .writeback import write_back


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="where-to-next ETL: stage 1 (parse + validate) + stage 2 (location)")
    source_group = parser.add_mutually_exclusive_group()
    source_group.add_argument("--csv", type=Path, help="Path to a local CSV export of the Itinerary sheet")
    source_group.add_argument(
        "--sheet-id", help="Google Sheet ID. Falls back to the GOOGLE_SHEET_ID env var if omitted."
    )
    parser.add_argument(
        "--worksheet",
        help="Worksheet/tab name to read (Sheets source only). "
        "Falls back to the WORKSHEET_NAME env var, then 'Itinerary v1'.",
    )
    parser.add_argument("--out-dir", type=Path, default=Path("etl"), help="Where to write trip.json and report.md")
    parser.add_argument(
        "--live", action="store_true",
        help="Actually geocode and (in a later piece) write back to the sheet. "
        "Default is dry run: zero network calls, zero sheet writes.",
    )
    parser.add_argument(
        "--allow-bulk", action="store_true",
        help="Lift the 300-geocoding-request safety cap for this run.",
    )
    parser.add_argument(
        "--no-writeback", action="store_true",
        help="Under --live, geocode but do not write id/lat/lng/place_id/resolved_from back to the sheet.",
    )
    args = parser.parse_args(argv)

    sheet_id = args.sheet_id or os.environ.get("GOOGLE_SHEET_ID")

    if args.csv:
        source = CsvLoader(args.csv)
        revision = hashlib.sha256(args.csv.read_bytes()).hexdigest()
    elif sheet_id:
        source = SheetsLoader(sheet_id, args.worksheet)
        revision = None
    else:
        parser.error("one of --csv, --sheet-id, or the GOOGLE_SHEET_ID env var is required")

    result = parse_rows(source)

    if result.trip is not None and revision is not None:
        result.trip.trip.source_revision = revision

    location = None
    if result.trip is not None:
        client = None
        budget = None
        if args.live:
            api_key = os.environ.get("GOOGLE_GEOCODING_KEY")
            if not api_key:
                parser.error("--live requires GOOGLE_GEOCODING_KEY to be set")
            client = GeocodeClient(api_key=api_key)
            budget = RequestBudget(allow_bulk=args.allow_bulk)

        try:
            location = resolve_locations(result.trip, live=args.live, client=client, budget=budget)
        except RuntimeError as e:
            result.errors.append(str(e))
            location = None
        else:
            result.errors.extend(location.errors)
            result.warnings.extend(location.warnings)

    writeback = None
    if result.trip is not None and sheet_id and not args.csv:
        worksheet = get_worksheet(sheet_id, args.worksheet) if (args.live and not args.no_writeback) else None
        writeback = write_back(
            result.trip,
            worksheet,
            original_row_count=result.row_count,
            original_plan_checksum=result.plan_checksum,
            live=args.live,
            no_writeback=args.no_writeback,
        )
        if writeback.aborted:
            result.errors.append(f"write-back aborted: {writeback.abort_reason}")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.out_dir / "report.md"
    report_path.write_text(render_report(result, location, live=args.live, writeback=writeback), encoding="utf-8")

    if result.trip is None or result.errors:
        print(f"ETL failed with {len(result.errors)} error(s); see {report_path}", file=sys.stderr)
        return 1

    trip_path = args.out_dir / "trip.json"
    payload = result.trip.model_dump(mode="json", by_alias=True)
    trip_path.write_text(
        json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {trip_path} and {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
