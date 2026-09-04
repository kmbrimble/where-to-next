"""CLI entry point: python -m etl --csv path/to.csv [--out-dir DIR]"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from .loaders import CsvLoader, SheetsLoader
from .parse import parse_rows
from .report import render_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="where-to-next ETL: stage 1 (parse + validate, no network)")
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--csv", type=Path, help="Path to a local CSV export of the Itinerary sheet")
    source_group.add_argument("--sheet-id", help="Google Sheet ID (reads GOOGLE_SHEETS_SA_KEY from env)")
    parser.add_argument("--out-dir", type=Path, default=Path("."), help="Where to write trip.json and report.md")
    args = parser.parse_args(argv)

    if args.csv:
        source = CsvLoader(args.csv)
        revision = hashlib.sha256(args.csv.read_bytes()).hexdigest()
    else:
        source = SheetsLoader(args.sheet_id)
        revision = None

    result = parse_rows(source)

    if result.trip is not None and revision is not None:
        result.trip.trip.source_revision = revision

    args.out_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.out_dir / "report.md"
    report_path.write_text(render_report(result), encoding="utf-8")

    if result.trip is None:
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
