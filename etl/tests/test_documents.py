"""Tests for etl/documents.py — mocked Drive and R2, no live calls."""
from __future__ import annotations

from etl.documents import DocumentsReport, DriveClient, R2Client, compute_documents, parse_filename
from etl.models import Day, Stop, Trip, TripMeta


def make_stop(row_num: int, title: str, documents=None) -> Stop:
    return Stop(
        id=f"row{row_num}", seq=row_num, title=title, kind="poi", timezone="America/Vancouver",
        how="drive", travel_minutes=10, dwell_minutes=10, timing="floating", row_num=row_num,
        documents=documents,
    )


def make_trip() -> Trip:
    return Trip(trip=TripMeta(), days=[
        Day(day=1, date="2026-09-27", timezone="America/Vancouver", stops=[make_stop(2, "A")]),
        Day(day=2, date="2026-09-28", timezone="America/Vancouver", stops=[make_stop(3, "B")]),
        Day(day=3, date="2026-09-29", timezone="America/Vancouver", stops=[make_stop(4, "C", documents=["waiver-form"])]),
    ])


def drive_files(*names_and_meta):
    """names_and_meta: list of (name,) or (name, size, md5)."""
    files = []
    for i, item in enumerate(names_and_meta):
        if len(item) == 1:
            name, size, md5 = item[0], 100, "abc123"
        else:
            name, size, md5 = item
        files.append({"id": f"drive-{i}", "name": name, "size": size, "md5Checksum": md5})
    return files


def test_parse_filename_single_date():
    p = parse_filename("20260929 Shuttle - TAG SxS Shuttle Service 4pm")
    assert p.start_date == p.end_date == "2026-09-29"
    assert p.doc_type == "Shuttle"
    assert p.title == "TAG SxS Shuttle Service 4pm"


def test_parse_filename_extra_separator():
    p = parse_filename("20260927 - Flight - AC123 to YVR")
    assert p.start_date == "2026-09-27"
    assert p.doc_type == "Flight"
    assert p.title == "AC123 to YVR"


def test_parse_filename_date_range():
    p = parse_filename("20261006-20261009 Hotel - Forest Park Hotel")
    assert p.start_date == "2026-10-06"
    assert p.end_date == "2026-10-09"
    assert p.doc_type == "Hotel"
    assert p.title == "Forest Park Hotel"


def test_parse_filename_no_date_returns_none():
    assert parse_filename("waiver-form") is None


def test_parse_filename_invalid_date_returns_none():
    assert parse_filename("20261399 Hotel - Bad Date") is None


def test_ds_store_and_non_pdf_ignored():
    drive = DriveClient(list_fn=lambda: [
        {"id": "1", "name": ".DS_Store", "size": 0, "md5Checksum": ""},
        {"id": "2", "name": "notes.txt", "size": 10, "md5Checksum": "x"},
        {"id": "3", "name": "20260927 Flight - AC123.pdf", "size": 10, "md5Checksum": "x"},
    ])
    assert [f.name for f in drive.list_pdfs()] == ["20260927 Flight - AC123.pdf"]


def test_unmatched_document_warns_not_errors():
    drive = DriveClient(list_fn=lambda: drive_files(("stray-file.pdf",)))
    trip = make_trip()

    report = compute_documents(trip, live=False, drive=drive)

    assert report.unmatched == ["stray-file.pdf"]
    assert any("unmatched" in w for w in report.warnings)
    assert report.matched == 0


def test_single_date_matches_day():
    drive = DriveClient(list_fn=lambda: drive_files(("20260927 Flight - AC123.pdf",)))
    trip = make_trip()

    report = compute_documents(trip, live=False, drive=drive)

    assert report.matched == 1
    doc = trip.days[0].documents[0]
    assert doc.id == "20260927 Flight - AC123"
    assert doc.type == "Flight"
    assert doc.title == "AC123"
    assert doc.url == "/docs/20260927 Flight - AC123.pdf"


def test_date_range_matches_every_day_in_range():
    drive = DriveClient(list_fn=lambda: drive_files(("20260927-20260929 Hotel - Listel.pdf",)))
    trip = make_trip()

    compute_documents(trip, live=False, drive=drive)

    assert all(d.documents and d.documents[0].id == "20260927-20260929 Hotel - Listel" for d in trip.days)


def test_explicit_documents_column_takes_precedence():
    drive = DriveClient(list_fn=lambda: drive_files(("waiver-form.pdf",)))
    trip = make_trip()

    report = compute_documents(trip, live=False, drive=drive)

    assert report.matched == 1
    assert trip.days[2].documents[0].id == "waiver-form"
    # only the referencing day/stop got it, not every day
    assert trip.days[0].documents == []


def test_dry_run_uploads_nothing():
    calls = []
    drive = DriveClient(
        list_fn=lambda: drive_files(("20260927 Flight - AC123.pdf",)),
        download_fn=lambda fid: calls.append(fid) or b"pdf-bytes",
    )
    trip = make_trip()

    report = compute_documents(trip, live=False, drive=drive, r2=None)

    assert calls == []
    assert report.uploaded == 0 and report.skipped == 0


def test_idempotent_skip_when_size_and_md5_match():
    drive = DriveClient(
        list_fn=lambda: drive_files(("20260927 Flight - AC123.pdf", 555, "matching-md5")),
        download_fn=lambda fid: b"should not be fetched",
    )
    r2 = R2Client(
        account_id="acc", access_key="key", secret_key="secret",
        head_fn=lambda key: {"size": 555, "md5": "matching-md5"},
        put_fn=lambda key, body: (_ for _ in ()).throw(AssertionError("should not upload")),
    )
    trip = make_trip()

    report = compute_documents(trip, live=True, drive=drive, r2=r2)

    assert report.uploaded == 0
    assert report.skipped == 1


def test_upload_when_no_existing_object():
    uploaded = []
    drive = DriveClient(
        list_fn=lambda: drive_files(("20260927 Flight - AC123.pdf", 555, "md5")),
        download_fn=lambda fid: b"pdf-bytes",
    )
    r2 = R2Client(
        account_id="acc", access_key="key", secret_key="secret",
        head_fn=lambda key: None,
        put_fn=lambda key, body: uploaded.append((key, body)),
    )
    trip = make_trip()

    report = compute_documents(trip, live=True, drive=drive, r2=r2)

    assert report.uploaded == 1
    assert report.skipped == 0
    assert uploaded == [("20260927 Flight - AC123.pdf", b"pdf-bytes")]


def test_missing_r2_credentials_names_the_missing_vars():
    r2 = R2Client(account_id=None, access_key=None, secret_key=None)
    try:
        r2.sync("k", 1, "m", lambda: b"")
        raised = None
    except RuntimeError as e:
        raised = str(e)

    assert raised is not None
    assert "R2_ACCOUNT_ID" in raised
    assert "R2_ACCESS_KEY_ID" in raised
    assert "R2_SECRET_ACCESS_KEY" in raised
