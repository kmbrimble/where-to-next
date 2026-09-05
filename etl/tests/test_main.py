"""CLI-level tests for source-argument resolution (--csv / --sheet-id / GOOGLE_SHEET_ID)."""
from __future__ import annotations

import json

import pytest

import etl.__main__ as main_module


def test_sheet_id_env_var_used_when_no_flag_given(tmp_path, monkeypatch):
    captured = {}

    def fake_sheets_loader(sheet_id, worksheet_name):
        captured["sheet_id"] = sheet_id
        captured["worksheet_name"] = worksheet_name

        def load():
            return [
                ["Day", "Date", "Location", "Travel", "Fun Time", "Plan", "Address", "How",
                 "Zone", "Price", "Notes", "Links", "row_type", "fixed_time"],
                ["Day 1", "2026-09-27", "Vancouver", "", "", "", "", "", "America/Vancouver",
                 "", "", "", "day_header", "07:00"],
                ["", "", "", "0:20", "1:00", "Stanley Park", "", "drive", "America/Vancouver",
                 "", "", "", "stop", ""],
                ["", "", "", "", "", "Listel", "", "", "America/Vancouver", "", "", "",
                 "lodging", "15:00"],
                ["", "", "Vancouver", "", "", "", "", "", "", "", "", "", "day_end", ""],
            ]

        return load

    monkeypatch.setattr(main_module, "SheetsLoader", fake_sheets_loader)
    monkeypatch.setenv("GOOGLE_SHEET_ID", "sheet-from-env")

    exit_code = main_module.main(["--out-dir", str(tmp_path)])

    assert exit_code == 0
    assert captured["sheet_id"] == "sheet-from-env"


def test_explicit_sheet_id_flag_overrides_env(tmp_path, monkeypatch):
    captured = {}

    def fake_sheets_loader(sheet_id, worksheet_name):
        captured["sheet_id"] = sheet_id
        return lambda: (_ for _ in ()).throw(RuntimeError("not reached, only checking source selection"))

    monkeypatch.setattr(main_module, "SheetsLoader", fake_sheets_loader)
    monkeypatch.setenv("GOOGLE_SHEET_ID", "sheet-from-env")

    with pytest.raises(RuntimeError):
        main_module.main(["--sheet-id", "explicit-sheet", "--out-dir", str(tmp_path)])

    assert captured["sheet_id"] == "explicit-sheet"


def test_no_csv_no_sheet_id_no_env_var_errors_clearly(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_SHEET_ID", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        main_module.main(["--out-dir", str(tmp_path)])

    assert exc_info.value.code == 2


def test_csv_and_sheet_id_together_still_rejected(tmp_path):
    with pytest.raises(SystemExit) as exc_info:
        main_module.main(["--csv", "x.csv", "--sheet-id", "y", "--out-dir", str(tmp_path)])

    assert exc_info.value.code == 2


def test_live_cli_actually_wires_a_short_link_resolver_end_to_end(tmp_path, monkeypatch):
    """Regression for the missing wiring: __main__.py must construct a real
    ShortLinkResolver and pass it through to resolve_locations() under --live.
    A resolve_locations()-level test alone can't catch this, since it always takes
    an explicit short_link_resolver argument regardless of what the CLI does.
    """
    def fake_sheets_loader(sheet_id, worksheet_name):
        def load():
            return [
                ["Day", "Date", "Location", "Travel", "Fun Time", "Plan", "Address", "How",
                 "Zone", "Price", "Notes", "Links", "row_type", "kind", "timing", "fixed_time"],
                ["Day 1", "2026-09-27", "Vancouver", "", "", "", "", "", "America/Vancouver",
                 "", "", "", "day_header", "", "", "07:00"],
                ["", "", "", "0:20", "1:00", "Short Link Stop", "", "drive", "America/Vancouver",
                 "", "", "https://maps.app.goo.gl/abc123", "stop", "poi", "fixed", "09:00"],
                ["", "", "", "", "", "Listel", "", "", "America/Vancouver", "", "", "",
                 "lodging", "", "", "15:00"],
                ["", "", "Vancouver", "", "", "", "", "", "", "", "", "", "day_end", "", "", ""],
            ]
        return load

    resolve_calls = []

    from etl.geocode import ShortLinkResult

    class SpyShortLinkResolver:
        def __init__(self):
            pass

        def resolve(self, url):
            resolve_calls.append(url)
            return ShortLinkResult(url="https://www.google.com/maps/@49.6725,-123.1583,15z")

    monkeypatch.setattr(main_module, "SheetsLoader", fake_sheets_loader)
    monkeypatch.setattr(main_module, "ShortLinkResolver", SpyShortLinkResolver)
    monkeypatch.setenv("GOOGLE_SHEET_ID", "sheet-from-env")
    monkeypatch.setenv("GOOGLE_GEOCODING_KEY", "fake-key")

    exit_code = main_module.main(["--live", "--no-writeback", "--out-dir", str(tmp_path)])

    assert exit_code == 0
    assert resolve_calls == ["https://maps.app.goo.gl/abc123"]  # resolver was actually called

    trip = json.loads((tmp_path / "trip.json").read_text())
    stop = trip["days"][0]["stops"][0]
    assert stop["lat"] == 49.6725
    assert stop["lng"] == -123.1583
    assert stop["resolved_from"] == "maps_link"
