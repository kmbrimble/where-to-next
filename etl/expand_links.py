"""One-off migration: resolve short Maps links (maps.app.goo.gl / goo.gl/maps found
in Links or Notes) into a permanent "lat, lng" Address value.

This is NOT part of the regular ETL. Google rate-limits (HTTP 429) rapid short-link
redirect follows, which makes following them on every ETL run both slow and hostile
to the account — see docs/SCHEMA.md's Address-resolution notes. Run this once per
batch of short links, by hand, with a generous delay between requests. Once a
coordinate pair is in Address, it's permanent, exact, and the user's own hand-placed
pin: no geocoding and no redirect-following ever touches that row again.

Usage: python -m etl.expand_links [--live] [--delay 3] [--overwrite-address]
"""
from __future__ import annotations

import argparse
import os
import time
from dataclasses import dataclass, field

from .geocode import ShortLinkResolver
from .loaders import SheetsLoader, get_worksheet
from .locate import extract_coords_from_maps_url, find_maps_urls, is_short_maps_link
from .parse import normalize_header


def _col_letter(index: int) -> str:
    letters = ""
    index += 1
    while index > 0:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def find_short_link_candidates(rows: list[list[str]]) -> list[dict]:
    """rows: list[list[str]], header first. One entry per row with a short link in
    Links or Notes — the first short link found on that row is used."""
    header = rows[0]
    index: dict[str, int] = {}
    for i, h in enumerate(header):
        index.setdefault(normalize_header(h), i)

    missing = [c for c in ("links", "notes", "plan", "address") if c not in index]
    if missing:
        raise ValueError(f"Missing required header(s) for expand-links: {sorted(missing)}")

    def cell(row: list[str], name: str) -> str:
        i = index[name]
        return row[i].strip() if i < len(row) else ""

    candidates = []
    for row_num, row in enumerate(rows[1:], start=2):
        urls = find_maps_urls(cell(row, "links")) + find_maps_urls(cell(row, "notes"))
        short_urls = [u for u in urls if is_short_maps_link(u)]
        if not short_urls:
            continue
        candidates.append({
            "row_num": row_num,
            "title": cell(row, "plan"),
            "url": short_urls[0],
            "address": cell(row, "address"),
        })
    return candidates, index


def resolve_with_429_backoff(resolver: ShortLinkResolver, url: str, *, base_delay: float, max_retries: int = 3):
    """Exponential backoff specifically for HTTP 429. Any other failure is returned
    as-is (ShortLinkResolver already does one internal retry for those).

    ShortLinkResolver caches by URL, so without clearing that cache between
    attempts, resolve() would just keep returning the same stale 429 result
    instead of actually re-attempting the follow — forget() undoes that.
    """
    attempt = 0
    while True:
        result = resolver.resolve(url)
        if result.ok or result.error != "HTTP 429" or attempt >= max_retries:
            return result
        attempt += 1
        time.sleep(base_delay * (2 ** attempt))
        resolver.forget(url)


@dataclass
class ExpandLinksReport:
    rows: list = field(default_factory=list)  # dicts: row_num, title, url, status, detail
    rate_limited: bool = False
    remaining: int = 0


def expand_links(
    candidates: list[dict], *, live: bool, resolver: ShortLinkResolver | None = None,
    overwrite_address: bool = False, base_delay: float = 3.0, worksheet=None, address_col: int | None = None,
) -> ExpandLinksReport:
    report = ExpandLinksReport()

    for i, c in enumerate(candidates):
        row = {"row_num": c["row_num"], "title": c["title"], "url": c["url"]}

        if not live:
            row["status"] = "would resolve"
            report.rows.append(row)
            continue

        result = resolve_with_429_backoff(resolver, c["url"], base_delay=base_delay)

        if not result.ok and result.error == "HTTP 429":
            report.rate_limited = True
            report.remaining = len(candidates) - i - 1  # exclude this (aborted) row itself
            row["status"] = "aborted"
            row["detail"] = "rate-limited (HTTP 429) after retries — stopping rather than hammering Google"
            report.rows.append(row)
            return report

        if not result.ok:
            row["status"] = "failed"
            row["detail"] = result.error
            report.rows.append(row)
            continue

        coords = extract_coords_from_maps_url(result.url)
        if not coords:
            row["status"] = "failed"
            row["detail"] = f"redirect resolved but no coordinates in {result.url!r}"
            report.rows.append(row)
            continue

        lat, lng, low_confidence = coords
        row["lat"], row["lng"] = lat, lng
        viewport_note = "coordinates taken from map viewport, not the place marker — verify" if low_confidence else None

        if c["address"] and not overwrite_address:
            row["status"] = "skipped"
            row["detail"] = f"Address already set ({c['address']!r}) — pass --overwrite-address to replace"
            report.rows.append(row)
            continue

        if worksheet is not None:
            worksheet.batch_update([{
                "range": f"{_col_letter(address_col)}{c['row_num']}",
                "values": [[f"{lat}, {lng}"]],
            }])
        row["status"] = "written"
        if viewport_note:
            row["detail"] = viewport_note
        report.rows.append(row)

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="One-off migration: resolve short Maps links into a permanent Address coordinate pair."
    )
    parser.add_argument("--sheet-id", help="Falls back to GOOGLE_SHEET_ID env var.")
    parser.add_argument("--worksheet", help="Falls back to WORKSHEET_NAME env var, then 'Itinerary v1'.")
    parser.add_argument("--live", action="store_true", help="Actually follow links and write. Default is dry run.")
    parser.add_argument("--delay", type=float, default=3.0, help="Seconds between requests (default 3).")
    parser.add_argument(
        "--overwrite-address", action="store_true",
        help="Replace a non-empty Address. Default: never touch a row that already has one.",
    )
    args = parser.parse_args(argv)

    sheet_id = args.sheet_id or os.environ.get("GOOGLE_SHEET_ID")
    if not sheet_id:
        parser.error("--sheet-id or GOOGLE_SHEET_ID is required")

    rows = SheetsLoader(sheet_id, args.worksheet)()
    candidates, index = find_short_link_candidates(rows)

    resolver = None
    worksheet = None
    if args.live:
        resolver = ShortLinkResolver(delay=args.delay)
        worksheet = get_worksheet(sheet_id, args.worksheet)

    report = expand_links(
        candidates, live=args.live, resolver=resolver, overwrite_address=args.overwrite_address,
        base_delay=args.delay, worksheet=worksheet, address_col=index.get("address"),
    )

    print(f"{'DRY RUN' if not args.live else 'LIVE'} — {len(candidates)} short-link row(s) found")
    for row in report.rows:
        line = f"Row {row['row_num']} {row['title']!r}: {row['url']}"
        if "lat" in row:
            line += f" -> ({row['lat']}, {row['lng']})"
        line += f" [{row['status']}]"
        if row.get("detail"):
            line += f" — {row['detail']}"
        print(line)

    if report.rate_limited:
        print(f"\nStopped after rate-limiting. {len(report.rows) - 1} done, {report.remaining} remaining.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
