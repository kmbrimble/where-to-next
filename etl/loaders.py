"""Row sources for the ETL. Both return the same shape: list[list[str]], header row first.

SheetsLoader is untested against a real sheet until it runs in CI with real
credentials — kept thin and obvious so there's little surface for an untested bug to
hide in. Its Date-serial conversion (see sheets_serial_to_iso_date) is covered against
a mocked gspread, since it's pure arithmetic and doesn't need live credentials to test.
"""
from __future__ import annotations

import csv
import json
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

RowSource = Callable[[], list[list[str]]]

DEFAULT_WORKSHEET_NAME = "Itinerary v1"

# Google Sheets' date serial epoch: day 0 is 1899-12-30 (not 1900-01-01 — Sheets
# inherited Lotus 1-2-3's fictitious 1900 leap day, which shifts the epoch by a day).
SHEETS_EPOCH = date(1899, 12, 30)


def sheets_serial_to_iso_date(serial: float) -> str:
    return (SHEETS_EPOCH + timedelta(days=int(serial))).isoformat()


def CsvLoader(path: str | Path) -> RowSource:
    def load() -> list[list[str]]:
        with open(path, newline="", encoding="utf-8") as f:
            return [row for row in csv.reader(f)]

    return load


def SheetsLoader(sheet_id: str | None = None, worksheet_name: str | None = None) -> RowSource:
    def load() -> list[list[str]]:
        import gspread

        sid = sheet_id or os.environ["GOOGLE_SHEET_ID"]
        name = worksheet_name or os.environ.get("WORKSHEET_NAME") or DEFAULT_WORKSHEET_NAME
        key = json.loads(os.environ["GOOGLE_SHEETS_SA_KEY"])
        client = gspread.service_account_from_dict(key)
        spreadsheet = client.open_by_key(sid)
        try:
            sheet = spreadsheet.worksheet(name)
        except gspread.exceptions.WorksheetNotFound:
            available = [ws.title for ws in spreadsheet.worksheets()]
            raise ValueError(
                f"Worksheet {name!r} not found in spreadsheet {sid!r}. "
                f"Available worksheets: {available}"
            ) from None

        rows = sheet.get_all_values()
        if not rows:
            return rows

        header = rows[0]
        date_col = next(
            (i for i, h in enumerate(header) if h.strip().lower() == "date"),
            None,
        )
        if date_col is None:
            return rows

        # get_all_values() returns display-formatted strings (a real date serial like
        # 46292 can render as "27-Sep", dropping the year — display formatting is a
        # sheet-owner preference, not data). Re-read just the Date column unformatted
        # and convert any numeric serial back to a real ISO date. Non-numeric cells
        # (blank, or day-of-week labels on non-day_header rows) pass through untouched.
        unformatted = sheet.get_all_values(value_render_option="UNFORMATTED_VALUE")
        for row, raw_row in zip(rows[1:], unformatted[1:]):
            if date_col < len(raw_row) and isinstance(raw_row[date_col], (int, float)):
                row[date_col] = sheets_serial_to_iso_date(raw_row[date_col])

        return rows

    return load
