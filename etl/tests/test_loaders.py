"""Tests for SheetsLoader's worksheet-name resolution, using a fake gspread module.

SheetsLoader itself stays untested against a real sheet (needs live credentials) —
this only exercises the worksheet-selection logic, which is pure control flow.
"""
from __future__ import annotations

import json
import sys
import types

import pytest

from etl.loaders import DEFAULT_WORKSHEET_NAME, SheetsLoader


class FakeWorksheetNotFound(Exception):
    pass


class FakeWorksheet:
    def __init__(self, title: str, rows: list[list[str]]):
        self.title = title
        self._rows = rows

    def get_all_values(self) -> list[list[str]]:
        return self._rows


class FakeSpreadsheet:
    def __init__(self, worksheets: list[FakeWorksheet]):
        self._worksheets = worksheets

    def worksheet(self, name: str) -> FakeWorksheet:
        for ws in self._worksheets:
            if ws.title == name:
                return ws
        raise FakeWorksheetNotFound(name)

    def worksheets(self) -> list[FakeWorksheet]:
        return self._worksheets


class FakeClient:
    def __init__(self, spreadsheet: FakeSpreadsheet):
        self._spreadsheet = spreadsheet

    def open_by_key(self, sheet_id: str) -> FakeSpreadsheet:
        return self._spreadsheet


@pytest.fixture
def fake_gspread(monkeypatch):
    rows = [["row_type"], ["day_header"]]
    spreadsheet = FakeSpreadsheet([
        FakeWorksheet(DEFAULT_WORKSHEET_NAME, rows),
        FakeWorksheet("Checklist", [["day", "item"]]),
    ])

    module = types.ModuleType("gspread")
    module.service_account_from_dict = lambda key: FakeClient(spreadsheet)
    module.exceptions = types.SimpleNamespace(WorksheetNotFound=FakeWorksheetNotFound)
    monkeypatch.setitem(sys.modules, "gspread", module)

    monkeypatch.setenv("GOOGLE_SHEETS_SA_KEY", json.dumps({"type": "service_account"}))
    monkeypatch.setenv("GOOGLE_SHEET_ID", "fake-sheet-id")

    return rows


def test_default_worksheet_name_is_itinerary_v1(fake_gspread):
    load = SheetsLoader()
    assert load() == fake_gspread


def test_explicit_worksheet_flag_overrides_default(fake_gspread, monkeypatch):
    monkeypatch.setenv("WORKSHEET_NAME", "should not be used")
    load = SheetsLoader(worksheet_name="Checklist")
    assert load() == [["day", "item"]]


def test_worksheet_name_env_var_used_when_no_flag(fake_gspread, monkeypatch):
    monkeypatch.setenv("WORKSHEET_NAME", "Checklist")
    load = SheetsLoader()
    assert load() == [["day", "item"]]


def test_missing_worksheet_lists_available_names(fake_gspread):
    load = SheetsLoader(worksheet_name="Nonexistent Tab")
    with pytest.raises(ValueError) as exc_info:
        load()

    message = str(exc_info.value)
    assert "Nonexistent Tab" in message
    assert DEFAULT_WORKSHEET_NAME in message
    assert "Checklist" in message
