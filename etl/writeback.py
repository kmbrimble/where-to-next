"""Sheet write-back for stage-2 location resolution.

Writes ONLY id, lat, lng, place_id, resolved_from — never Address, never any other
column. Address is the human's source of truth and the whole correction mechanism
(SCHEMA.md §3) depends on it staying untouched. Only runs under --live, and only
when --no-writeback isn't passed; dry run and --no-writeback both just report what
WOULD be written.

Unverified against a real sheet — see the commit/PR for exactly what the tests
below do and don't prove.
"""
from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field

from .parse import normalize_header

WRITEBACK_COLUMNS = ["id", "lat", "lng", "place_id", "resolved_from"]


def generate_id(row_num: int) -> str:
    """Opaque, URL-safe, collision-resistant id. Written once and never
    regenerated. Seeded by row position at the moment of generation, not by stop
    content — editing a stop's title/address later must never change its id.
    """
    digest = hashlib.blake2b(f"stop:{row_num}".encode(), digest_size=5).digest()
    return base64.b32encode(digest).decode().rstrip("=").lower()


def plan_checksum(plan_values: list[str]) -> str:
    return hashlib.sha256("\n".join(plan_values).encode()).hexdigest()


def _col_letter(index: int) -> str:
    letters = ""
    index += 1
    while index > 0:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


@dataclass
class WritebackReport:
    would_write: list
    cells_written: int = 0
    ids_assigned: list = field(default_factory=list)
    unmatched: list = field(default_factory=list)
    aborted: bool = False
    abort_reason: str | None = None


def write_back(
    trip,
    worksheet,
    *,
    original_row_count: int,
    original_plan_checksum: str,
    live: bool,
    no_writeback: bool = False,
) -> WritebackReport:
    """worksheet: a gspread-like object exposing get_all_values() and
    batch_update(list[{"range": "A1", "values": [[...]]}])."""
    # Every stop gets an id, regardless of resolution status — id is a stable row
    # identity for D1 state, not a signal that a pin exists. A stop that never
    # resolves to coordinates must still be addressable (e.g. by Pin Verification
    # apply, which matches by id) rather than being silently stuck on a synthetic
    # placeholder id that was never written to the sheet.
    all_stops = [s for day in trip.days for s in day.stops]

    would_write = [
        f"row={s.row_num} id={s.id if s.has_real_id else '<new>'} lat={s.lat} lng={s.lng} "
        f"place_id={s.place_id} resolved_from={s.resolved_from}"
        for s in all_stops
    ]

    if not live or no_writeback:
        return WritebackReport(would_write=would_write)

    fresh_rows = worksheet.get_all_values()
    if not fresh_rows:
        return WritebackReport(would_write=would_write, aborted=True, abort_reason="sheet is empty on re-read")

    header, data_rows = fresh_rows[0], fresh_rows[1:]
    index: dict[str, int] = {}
    for i, h in enumerate(header):
        index.setdefault(normalize_header(h), i)

    missing = [c for c in WRITEBACK_COLUMNS if c not in index]
    if missing:
        return WritebackReport(
            would_write=would_write, aborted=True,
            abort_reason=f"sheet is missing required column(s) for write-back: {sorted(missing)}",
        )

    plan_idx = index.get("plan")
    fresh_plan_values = [row[plan_idx] if plan_idx is not None and plan_idx < len(row) else "" for row in data_rows]

    # A row inserted or removed between the initial read and now would silently
    # shift every subsequent row_num — refuse to write positionally rather than
    # risk landing correct data on the wrong row.
    if len(data_rows) != original_row_count or plan_checksum(fresh_plan_values) != original_plan_checksum:
        return WritebackReport(
            would_write=would_write, aborted=True,
            abort_reason="sheet shape changed between read and write (row count or Plan column differs)",
        )

    id_idx = index["id"]
    fresh_id_by_row: dict[int, str] = {}
    for offset, row in enumerate(data_rows):
        row_num = offset + 2
        fresh_id_by_row[row_num] = row[id_idx] if id_idx < len(row) else ""
    id_to_row = {v: k for k, v in fresh_id_by_row.items() if v}

    def fresh_cell(row_num: int, col_name: str) -> str:
        row = data_rows[row_num - 2] if 0 <= row_num - 2 < len(data_rows) else []
        i = index[col_name]
        return row[i] if i < len(row) else ""

    updates: list[dict] = []
    cells_written = 0
    ids_assigned: list[str] = []
    unmatched: list[str] = []

    for stop in all_stops:
        if stop.has_real_id:
            current_row = id_to_row.get(stop.id)
            if current_row is None:
                unmatched.append(f"Row {stop.row_num} (id={stop.id}): id no longer found in the sheet")
                continue
        else:
            # No id yet — the shape check above already confirmed nothing moved,
            # so matching by the original row_num is safe for this one pass only.
            current_row = stop.row_num
            if fresh_id_by_row.get(current_row):
                unmatched.append(f"Row {current_row}: id appeared between read and write — skipped")
                continue
            new_id = generate_id(current_row)
            stop.id = new_id
            ids_assigned.append(f"Row {current_row}: assigned id={new_id}")

        for col_name, value in (
            ("id", stop.id), ("lat", stop.lat), ("lng", stop.lng),
            ("place_id", stop.place_id or ""), ("resolved_from", stop.resolved_from),
        ):
            str_value = "" if value is None else str(value)
            if fresh_cell(current_row, col_name) == str_value:
                continue  # already correct — idempotent no-op
            updates.append({"range": f"{_col_letter(index[col_name])}{current_row}", "values": [[str_value]]})
            cells_written += 1

    if updates:
        worksheet.batch_update(updates)

    return WritebackReport(
        would_write=would_write, cells_written=cells_written, ids_assigned=ids_assigned, unmatched=unmatched,
    )
