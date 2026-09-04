"""Row sources for the ETL. Both return the same shape: list[list[str]], header row first.

SheetsLoader is untested until it runs in CI with real credentials — kept thin and
obvious so there's little surface for an untested bug to hide in.
"""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Callable

RowSource = Callable[[], list[list[str]]]


def CsvLoader(path: str | Path) -> RowSource:
    def load() -> list[list[str]]:
        with open(path, newline="", encoding="utf-8") as f:
            return [row for row in csv.reader(f)]

    return load


def SheetsLoader(sheet_id: str | None = None) -> RowSource:
    def load() -> list[list[str]]:
        import gspread

        sid = sheet_id or os.environ["GOOGLE_SHEET_ID"]
        key = json.loads(os.environ["GOOGLE_SHEETS_SA_KEY"])
        client = gspread.service_account_from_dict(key)
        sheet = client.open_by_key(sid).worksheet("Itinerary")
        return sheet.get_all_values()

    return load
