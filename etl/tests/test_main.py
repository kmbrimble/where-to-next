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


def test_live_cli_never_touches_short_links_directs_to_expand_links_instead(tmp_path, monkeypatch):
    """The main ETL must never follow a short Maps link (Google 429-rate-limits
    rapid redirect follows) — that's etl/expand_links.py's job, run separately.
    A stop with only a short link and no Address must come out unresolved with a
    warning pointing at expand_links, not a resolved coordinate.
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

    assert not hasattr(main_module, "ShortLinkResolver"), "main ETL must not import ShortLinkResolver at all"

    monkeypatch.setattr(main_module, "SheetsLoader", fake_sheets_loader)
    monkeypatch.setenv("GOOGLE_SHEET_ID", "sheet-from-env")
    monkeypatch.setenv("GOOGLE_GEOCODING_KEY", "fake-key")

    exit_code = main_module.main(["--live", "--no-writeback", "--out-dir", str(tmp_path)])

    # trip.json isn't written (unresolved stop is a warning, not an error, but the
    # geocoding wiring above has no client call to make here so the run succeeds)
    assert exit_code == 0
    report_text = (tmp_path / "report.md").read_text()
    assert "expand_links" in report_text
