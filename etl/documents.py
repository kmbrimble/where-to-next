"""Stage 3 piece 3: match booking-confirmation PDFs in a private Drive folder to
days/stops by filename convention, then mirror matched files into the private R2
bucket so the app can fetch them via an authenticated Worker proxy. See
docs/SCHEMA.md §5 (matching convention) and §6 (documents shape in trip.json).

NOT part of the regular ETL pipeline — like etl/expand_links.py, run this by hand
against an already-built trip.json (Drive listing is a separate, privacy-sensitive
credential from Sheets/Geocoding/Routes, and uploads write to a private bucket).

Usage: python -m etl.documents --trip-json etl/trip.json [--live]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

from .models import Document, Trip

# Common short words dropped when shortening a title to a slug — keeps the
# slug's word budget spent on the words that actually distinguish one
# document from another (see document_slug()).
SLUG_STOPWORDS = {"a", "an", "and", "the", "of", "for", "to", "with", "your", "on", "in", "at", "by", "from"}
SLUG_SPLIT_RE = re.compile(r"[^a-z0-9]+")


def _slug_words(text: str) -> list[str]:
    ascii_text = text.encode("ascii", "ignore").decode("ascii").lower()
    return [w for w in SLUG_SPLIT_RE.split(ascii_text) if w]


def document_slug(start_date: str | None, doc_type: str, title: str, max_title_words: int = 4) -> str:
    """Stable, URL-safe id: <yyyymmdd>-<type>-<shortened title>, ASCII lowercase,
    hyphen-separated. This is used as the trip.json document id, the R2 object
    key, and the /docs/<slug>.pdf url — a raw Drive filename (spaces, parens, a
    literal '#') is not URL-safe and a '#' truncates the url at a browser's URL
    fragment delimiter, which is the bug this replaces."""
    parts = []
    if start_date:
        parts.append(start_date.replace("-", ""))
    parts.extend(_slug_words(doc_type))
    title_words = [w for w in _slug_words(title) if w not in SLUG_STOPWORDS][:max_title_words]
    parts.extend(title_words or ["doc"])
    return "-".join(parts)


def _disambiguator(original_stem: str) -> str:
    """Short, deterministic suffix for a slug collision — derived from the
    original filename alone (not from processing order), so which file gets
    which suffix never changes between runs regardless of Drive listing order."""
    return hashlib.sha1(original_stem.encode()).hexdigest()[:6]

DRIVE_FOLDER_ID = "12Yt0lhoraEJnv5UBJf614r6mUS3MtvbF"
R2_BUCKET = "where-to-next-docs"

# Date-range form must be tried before single-date, or the single-date regex
# would swallow the range's first date and leave a stray "-YYYYMMDD" in rest.
DATE_RANGE_RE = re.compile(r"^(\d{8})-(\d{8})[\s-]*(.*)$")
SINGLE_DATE_RE = re.compile(r"^(\d{8})[\s-]*(.*)$")


@dataclass
class DriveFile:
    id: str
    name: str  # includes .pdf
    size: int
    md5: str


@dataclass
class ParsedFilename:
    start_date: str
    end_date: str
    doc_type: str
    title: str


def _iso(yyyymmdd: str) -> str:
    return date(int(yyyymmdd[0:4]), int(yyyymmdd[4:6]), int(yyyymmdd[6:8])).isoformat()


def parse_filename(stem: str) -> ParsedFilename | None:
    """stem is the filename without its .pdf extension."""
    m = DATE_RANGE_RE.match(stem)
    try:
        if m:
            start, end = _iso(m.group(1)), _iso(m.group(2))
            rest = m.group(3)
        else:
            m = SINGLE_DATE_RE.match(stem)
            if not m:
                return None
            start = end = _iso(m.group(1))
            rest = m.group(2)
    except ValueError:
        return None  # 8 digits that aren't a real calendar date

    doc_type, _, title = rest.partition(" - ")
    return ParsedFilename(start_date=start, end_date=end, doc_type=doc_type.strip() or "Unknown", title=title.strip())


def _dates_between(start_iso: str, end_iso: str) -> list[str]:
    d, end = date.fromisoformat(start_iso), date.fromisoformat(end_iso)
    out = []
    while d <= end:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


class DriveClient:
    """Lists/downloads from one Drive folder with drive.readonly scope. Listing
    is a read and safe under dry run; download is only used for a live upload."""

    def __init__(self, folder_id: str = DRIVE_FOLDER_ID, list_fn: Callable[[], list[dict]] | None = None,
                 download_fn: Callable[[str], bytes] | None = None):
        self._folder_id = folder_id
        self._list_fn = list_fn
        self._download_fn = download_fn

    def list_pdfs(self) -> list[DriveFile]:
        list_fn = self._list_fn or self._drive_list
        files = list_fn()
        return [
            DriveFile(id=f["id"], name=f["name"], size=int(f.get("size", 0)), md5=f.get("md5Checksum", ""))
            for f in files
            if f["name"] != ".DS_Store" and f["name"].lower().endswith(".pdf")
        ]

    def download(self, file_id: str) -> bytes:
        download_fn = self._download_fn or self._drive_download
        return download_fn(file_id)

    def _credentials(self, scopes: list[str]):
        from google.oauth2 import service_account

        info = json.loads(os.environ["GOOGLE_SHEETS_SA_KEY"])
        return service_account.Credentials.from_service_account_info(info, scopes=scopes)

    def _drive_list(self) -> list[dict]:
        from googleapiclient.discovery import build

        service = build("drive", "v3", credentials=self._credentials(
            ["https://www.googleapis.com/auth/drive.readonly"]
        ))
        files, page_token = [], None
        while True:
            resp = service.files().list(
                q=f"'{self._folder_id}' in parents and trashed = false",
                fields="nextPageToken, files(id, name, size, md5Checksum)",
                pageToken=page_token,
            ).execute()
            files.extend(resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return files

    def _drive_download(self, file_id: str) -> bytes:
        import io

        from googleapiclient.discovery import build
        from googleapiclient.http import MediaIoBaseDownload

        service = build("drive", "v3", credentials=self._credentials(
            ["https://www.googleapis.com/auth/drive.readonly"]
        ))
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, service.files().get_media(fileId=file_id))
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buf.getvalue()


class R2Client:
    """Uploads to the PRIVATE where-to-next-docs bucket only — never make it
    public, never attach a custom domain, never reuse the tiles bucket."""

    REQUIRED_ENV = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")

    def __init__(self, account_id: str | None = None, access_key: str | None = None,
                 secret_key: str | None = None, head_fn: Callable[[str], dict | None] | None = None,
                 put_fn: Callable[[str, bytes], None] | None = None):
        self._account_id = account_id or os.environ.get("R2_ACCOUNT_ID")
        self._access_key = access_key or os.environ.get("R2_ACCESS_KEY_ID")
        self._secret_key = secret_key or os.environ.get("R2_SECRET_ACCESS_KEY")
        self._head_fn = head_fn
        self._put_fn = put_fn
        self.uploaded = 0
        self.skipped = 0

    def _require_creds(self) -> None:
        env = {"R2_ACCOUNT_ID": self._account_id, "R2_ACCESS_KEY_ID": self._access_key,
               "R2_SECRET_ACCESS_KEY": self._secret_key}
        missing = [name for name, val in env.items() if not val]
        if missing:
            raise RuntimeError(f"R2 upload requires environment variables: {', '.join(missing)}")

    def _boto_client(self):
        import boto3

        return boto3.client(
            "s3",
            endpoint_url=f"https://{self._account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
        )

    def _boto_head(self, key: str) -> dict | None:
        from botocore.exceptions import ClientError

        try:
            head = self._boto_client().head_object(Bucket=R2_BUCKET, Key=key)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
                return None
            raise
        return {"size": head["ContentLength"], "md5": head["ETag"].strip('"')}

    def _boto_put(self, key: str, body: bytes) -> None:
        self._boto_client().put_object(Bucket=R2_BUCKET, Key=key, Body=body, ContentType="application/pdf")

    def sync(self, key: str, size: int, md5: str, fetch_body: Callable[[], bytes]) -> str:
        """Uploads fetch_body() to `key` unless an object already there matches
        size and md5 (idempotent). Returns "uploaded" or "skipped"."""
        self._require_creds()
        existing = (self._head_fn or self._boto_head)(key)
        if existing and existing["size"] == size and existing["md5"] == md5:
            self.skipped += 1
            return "skipped"
        (self._put_fn or self._boto_put)(key, fetch_body())
        self.uploaded += 1
        return "uploaded"


@dataclass
class DocumentsReport:
    matched: int = 0
    uploaded: int = 0
    skipped: int = 0
    unmatched: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    old_keys: list[str] = field(default_factory=list)  # pre-slug filename keys, now orphaned in R2


def compute_documents(trip, *, live: bool, drive: DriveClient, r2: R2Client | None = None) -> DocumentsReport:
    report = DocumentsReport()
    days_by_date = {day.date: day for day in trip.days}

    stops_by_stem: dict[str, list] = {}
    for day in trip.days:
        for stop in day.stops:
            for stem in stop.documents or []:
                stops_by_stem.setdefault(stem, []).append(day)

    # Pass 1: match each Drive file to its target day(s) — matching still keys
    # off the raw filename stem (explicit `documents` column values are typed
    # against the actual filename, not a slug).
    candidates = []
    for f in drive.list_pdfs():
        stem = f.name[:-4]  # strip ".pdf" — list_pdfs already filtered to .pdf
        parsed = parse_filename(stem)
        doc_type = parsed.doc_type if parsed else "Unknown"
        title = (parsed.title if parsed else "") or stem

        explicit_days = stops_by_stem.get(stem)
        if explicit_days:
            seen = set()
            target_days = [d for d in explicit_days if not (id(d) in seen or seen.add(id(d)))]
        elif parsed:
            target_days = [days_by_date[d] for d in _dates_between(parsed.start_date, parsed.end_date) if d in days_by_date]
            if not target_days:
                report.warnings.append(
                    f"{f.name}: no day in the trip matches {parsed.start_date}..{parsed.end_date} — unmatched"
                )
                report.unmatched.append(f.name)
                continue
        else:
            report.warnings.append(
                f"{f.name}: doesn't match the date-prefixed naming convention and isn't "
                f"referenced by any stop's documents column — unmatched"
            )
            report.unmatched.append(f.name)
            continue

        base_slug = document_slug(parsed.start_date if parsed else None, doc_type, title)
        candidates.append((f, stem, doc_type, title, target_days, base_slug))

    # Pass 2: resolve slug collisions. The disambiguator is derived from each
    # file's own original stem, so which colliding file gets which final slug
    # never depends on Drive's listing order — only on which files exist.
    by_base_slug: dict[str, list] = {}
    for c in candidates:
        by_base_slug.setdefault(c[5], []).append(c)

    for base_slug, group in by_base_slug.items():
        if len(group) <= 1:
            continue
        names = ", ".join(c[0].name for c in group)
        report.warnings.append(f"slug {base_slug!r} collided between {len(group)} files ({names}) — disambiguated")

    # Pass 3: build documents and (optionally) upload.
    for f, stem, doc_type, title, target_days, base_slug in candidates:
        slug = base_slug if len(by_base_slug[base_slug]) == 1 else f"{base_slug}-{_disambiguator(stem)}"

        document = Document(id=slug, type=doc_type, title=title, url=f"/docs/{slug}.pdf")
        for day in target_days:
            if not any(d.id == document.id for d in day.documents):
                day.documents.append(document)
        report.matched += 1
        report.old_keys.append(f.name)

        if live:
            outcome = r2.sync(f"{slug}.pdf", f.size, f.md5, lambda file_id=f.id: drive.download(file_id))
            if outcome == "uploaded":
                report.uploaded += 1
            else:
                report.skipped += 1

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Match and upload booking-confirmation PDFs (docs/SCHEMA.md §5)")
    parser.add_argument("--trip-json", type=Path, default=Path("etl/trip.json"), help="trip.json to read and update in place")
    parser.add_argument(
        "--live", action="store_true",
        help="Actually upload to R2. Default is dry run: list what would match, zero network writes.",
    )
    args = parser.parse_args(argv)

    trip = Trip.model_validate(json.loads(args.trip_json.read_text()))

    r2 = R2Client() if args.live else None
    try:
        report = compute_documents(trip, live=args.live, drive=DriveClient(), r2=r2)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1

    print(f"{'LIVE' if args.live else 'DRY RUN'} — matched {report.matched}, unmatched {len(report.unmatched)}")
    if args.live:
        print(f"uploaded {report.uploaded}, skipped (already current in R2) {report.skipped}")
    for w in report.warnings:
        print(f"WARNING: {w}")
    if report.old_keys:
        print(f"Old (pre-slug) R2 keys, now orphaned if this bucket had prior uploads ({len(report.old_keys)}):")
        for k in report.old_keys:
            print(f"  {k}")

    args.trip_json.write_text(
        json.dumps(trip.model_dump(mode="json", by_alias=True), sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
