# Data Schema

How the Google Sheet is structured, what the ETL reads, and the JSON the app consumes.

**Status:** draft for review. Not yet implemented.

Companion to `README.md`. Read that first for scope and architecture.

---

## 1. Principles

**The sheet holds inputs. The app computes outputs.**

The workbook keeps its cascade formulas — they're how the itinerary gets planned, and
they stay useful. But the ETL **never reads a computed column**. It reads only:

- the day's anchor time
- per-stop `Travel` and `Fun Time` durations
- timezone
- the fixed/floating flag and any fixed time

`Depart`, `Arrive`, the drive-total row, and the sunrise/sunset column are all derived,
and all ignored. The app recomputes them from the same inputs. This is deliberate: two
implementations reading the same inputs can't silently disagree about the plan. If the
sheet and the app ever show different times, one of them has a bug — which is worth
knowing.

**Columns are matched by header name, never by letter.** Inserting, moving or reordering
columns must not break the ETL. Header text is normalised (lowercased, trimmed,
whitespace collapsed) before matching. A missing required header is a build failure; an
unrecognised header is ignored silently.

**Ignored columns**, retained in the sheet, skipped by the ETL:

| Header | Why ignored |
|---|---|
| `Sunrise/Sunset` | Computed from lat/lng + date instead |
| `Depart` | Formula |
| `Arrive` | Formula |
| *(unnamed opinions column)* | Planning notes, not app data |
| `Check-in Deadline (local)`, `Buffer to Deadline` | Superseded by `timing` + `fixed_time` |
| `OUT` and its adjacent columns | Rejected alternates, not shipped |

---

## 2. Sheet: `Itinerary`

Migrated in place. Existing columns keep their headers and formulas. New columns are
added anywhere — position is irrelevant.

### Existing columns

| Header | ETL reads | Notes |
|---|---|---|
| `Day` | ✅ | `Day N`, only on the day-header row |
| `Date` | ✅ | Real date value, not text |
| `Location` | ✅ | Meaning depends on `row_type` |
| `Sunrise/Sunset` | ❌ | |
| `Depart` | ❌ | Formula |
| `Travel` | ✅ | Duration, previous stop → this stop |
| `Arrive` | ❌ | Formula |
| `Fun Time` | ✅ | Dwell duration |
| `Plan` | ✅ | Stop title |
| `Address` | ✅ | **New.** See resolution rules below |
| `How` | ✅ | **New.** Travel mode for the leg *into* this stop |
| `Zone` | ✅ | IANA name directly, e.g. `America/Vancouver` |
| `Price` | ✅ | Free text, display only — not parsed |
| `Notes` | ✅ | Free text |
| `Links` | ✅ | URL |

### New columns

| Header | Type | Required | Notes |
|---|---|---|---|
| `row_type` | enum | ✅ all rows | See below — the one classification column that must be explicit, never inferred |
| `kind` | enum | optional | `poi`, `meal`, `activity`, `lodging`, `flight`, `transfer`. **Default `poi`.** Tag only the exceptions |
| `lat` | number | — | **ETL-written.** Resolved from `Address` |
| `lng` | number | — | **ETL-written.** Resolved from `Address` |
| `place_id` | text | — | **ETL-written** where available |
| `timing` | enum | optional | `fixed` or `floating`. **Default `floating`.** Tag only the genuinely fixed stops — a handful per day, not all ~276 |
| `fixed_time` | time | if `fixed` | Local wall-clock, e.g. `16:30` |
| `arrive_before` | duration | optional | Check-in lead time, e.g. `00:30` |
| `daylight_required` | bool | optional | Treats sunset as a constraint |
| `day_offset` | integer | optional | Calendar days past the day-header date. Default 0 |
| `documents` | text | optional | Comma-separated filename stems, overrides auto-match |

`row_type` stays mandatory and explicit: it's the column that stops row position from
silently determining meaning, which is the exact corruption this migration exists to
fix. `kind` and `timing` don't carry that risk — an un-tagged stop defaulting to
`poi`/`floating` is a display nicety and a scheduling default, not a data-corruption
vector — so they're optional and only the exceptions need hand-tagging.

### `Address` resolution

Precedence order, checked top to bottom — the first match wins:

| # | Condition | Treatment | API call |
|---|---|---|---|
| a | `Address` parses as a coordinate pair, e.g. `49.6725, -123.1583` | Parsed directly as lat/lng | **None** |
| b | `Address` parses as a **global** plus code, e.g. `9535R9RG+9X` | Decoded offline (`openlocationcode`), centre of the cell | **None** |
| b′ | `Address` parses as a **compound** plus code, e.g. `4VMF+42 Whistler, BC` | The locality has to be resolved to recover the missing leading digits | Geocoding |
| c | A **long** Google Maps URL in `Links` or `Notes` yields coordinates | Extracted from the URL (`@lat,lng` or `!3d!4d`) | **None** |
| c′ | Only a **short** Maps link (`maps.app.goo.gl`, `goo.gl/maps`) is present | Not followed by the main ETL — see "One-time short-link migration" below | None (falls through to (d) if there's also an Address, otherwise unresolved with a warning) |
| d | `Address` is an address string | Geocoded | Geocoding |
| e | Nothing usable | Unresolved | None |

**(c) sits above (d) deliberately.** A Maps URL in the sheet was hand-copied by the
user from Google Maps, pointing at the exact spot they meant — that's stronger
evidence than geocoding an address string, which can return a confidently wrong
answer. Row 63 ("Giant Cedars", a real row in this sheet) geocoded to a point near
Lake Superior, Ontario, ~1800km from Revelstoke, BC, where it belongs.

### One-time short-link migration

**The main ETL never calls `maps.app.goo.gl` or follows any short link.** Google
rate-limits (HTTP 429) rapid redirect follows, so doing this on every run is both
slow and hostile to the account — confirmed directly: a live run following 27 short
links in quick succession got HTTP 429 on all of them.

Instead, `python -m etl.expand_links` is a **separate, one-time migration script**,
run by hand, not part of the build:

- Finds every row with a short link in `Links` or `Notes`, follows each one with a
  generous delay (default 3s, `--delay`) and exponential backoff on 429 (up to 3
  retries), and stops the whole run rather than grinding through a 429 storm.
- On success, writes `"lat, lng"` directly into that row's **`Address`** column —
  never `lat`/`lng`/`place_id` — and only if `Address` is currently empty (or
  `--overwrite-address` is passed).
- **Once a coordinate pair is in `Address`, it's permanent and exact: the user's own
  hand-placed pin.** From then on it resolves via rule (a) above — parsed directly,
  no geocoding, no redirect-following, no per-run network cost, ever again.
- `--dry-run` (default) makes zero network calls and reports exactly what would be
  written.

### Cache policy

Once a stop has a cached `lat`/`lng`/`place_id` (previously written back by the ETL),
what happens next depends on how cheap re-checking it is:

- **Coordinate pairs and global plus codes** (a, b) resolve offline for free, so they
  keep self-verifying on every run: if the cached value disagrees with what the
  Address deterministically decodes to, the **cache** is wrong — it's overwritten and
  a warning is logged. Costs nothing to check, so it's always checked.
- **Compound plus codes, address strings, and Maps links** (b′, c, d) are trusted
  once cached, with **no re-verification and no network call** — the cache simply
  wins, regardless of what the Address column classifies as. Earlier versions of this
  ETL re-verified these on every run to catch cache/Address drift if someone dragged
  content cells and left the ETL-written columns behind; row matching is now done by
  the opaque `id` column instead (§4), which makes that guard redundant. In steady
  state this is the difference between ~55 network calls/run forever and near zero.
- **`--reverify`** ignores every cache and re-resolves everything from source. This is
  the escape hatch for when a cache is suspected wrong — it trades the steady-state
  savings above for a full re-check, on demand rather than by default.
- **Clearing `lat`/`lng` in the sheet** still forces re-resolution on the very next
  run without needing `--reverify` — an empty cache is simply not a cache. This is the
  documented, everyday correction mechanism (see Write-back below) and doesn't depend
  on the escape hatch at all.

Plus signs URL-encode to `%2B` and spaces to `%20`; getting this wrong fails
silently with a plausible wrong answer rather than an error.

**Prefer global plus codes over compound ones.** A compound code (`CWC8+R9 Mountain
View, CA, USA`) depends on Google resolving the locality string, which reintroduces
exactly the ambiguity plus codes exist to avoid — and several stops here are nowhere
near a named locality. Global codes are unambiguous worldwide.

**Coordinates pasted directly cost nothing and can't be misread.** For the ~40 roadside
pullouts where name geocoding returns a plausible wrong answer, drop a pin in Google
Maps and paste either the coordinates or the plus code. Both are exact.

### Write-back

The ETL has **Editor** access and writes resolved `lat`, `lng` and `place_id` back into
the sheet. It never overwrites a non-empty `lat`/`lng` — hand-corrected coordinates are
authoritative and survive every subsequent run. To force re-resolution, clear the cells.

`Address` is never modified by the ETL.

The sheet may be open and edited in a browser while the ETL runs, and a row inserted
mid-run would otherwise cause correct data to be written to the wrong row. To guard
against this, write-back re-reads the `id` column immediately before writing and maps
ids to current row positions, rather than trusting row indices captured at read time.

### `How`

Travel mode for the leg arriving at this stop. Drives three things:

| Value | Deep link mode | Route polyline | Live traffic recalculation |
|---|---|---|---|
| `drive` | `driving` | ✅ computed | ✅ yes |
| `walk` | `walking` | ✅ computed | ❌ no |
| `taxi` | `driving` | ✅ computed | ❌ no — arrival isn't yours to control |
| `train` | `transit` | ❌ | ❌ no — scheduled, not traffic-affected |
| `shuttle` | none | ❌ | ❌ no |
| `plane` | none | ❌ straight line only | ❌ no |
| `transit` | `transit` | ❌ | ❌ no — catch-all for compound public-transit legs |

**Only `drive` legs are recalculated against live traffic.** A flight's block time does
not change because the I-15 is congested, and silently re-timing one would corrupt the
whole day's cascade. This is the main reason the column exists.

**`transit` is a catch-all**, not one specific mode — the sheet has real compound legs
like `Walk + Aquabus` and `Bus & walk` that don't map to any single value here. The ETL
normalises both of those literal strings to `transit` rather than rejecting them; add
more aliases here if further compound values turn up.

### `row_type`

The sheet is currently typed by row position, which is how a shifted row becomes silent
corruption. Making it explicit costs one column and breaks no formulas.

| Value | Meaning | Key columns |
|---|---|---|
| `day_header` | Start of a day, and optionally its first stop | `Day`, `Date`, `Location` = start location; `Plan` + the usual stop columns if present |
| `stop` | An itinerary item | `Travel`, `Fun Time`, `Plan`, `Zone` + new columns |
| `lodging` | Where you sleep that night | `Plan` = hotel name |
| `day_end` | Terminal row, and optionally its last stop | `Location` = end location; `Plan` + the usual stop columns if present |
| `blank` | Spacer | — |

An empty `row_type` is a **warning**, and the row is skipped — not guessed at, and not
a build failure. This is deliberate: it's what lets the sheet be migrated
incrementally, tagging one range of days at a time rather than all ~276 rows at once. A
`row_type` that's present but not one of the values above **is** a build failure — a
typo shouldn't be silently treated the same as "not yet migrated."

**`leg` and `drive_total` were removed from this enum.** Both looked structural — a
journey label, a recomputed total — but the real sheet packs an actual stop into the
same row (`Plan` holds a real stop title while `Location` holds the journey label or
drive duration), so a row type that treated the row as pure structure was silently
dropping real data. Those rows are retyped `stop` instead; `Location` on a `stop` row
is simply ignored, whatever it contains. `day.leg` is now **derived**, not read: see
below.

**`day_header` and `day_end` are dual-purpose, for the same reason.** Some days have a
real stop timed to the start or end of the day (e.g. a sunrise shoot on the
`day_header` row itself) — the sheet packs the day-structure columns and the stop
columns into one row rather than adding a separate row. When `Plan` is non-empty on
either, the row is read as a normal `stop` **in addition to** its structural role: the
`day_header` stop becomes the day's first stop, the `day_end` stop becomes its last.
The day's `fixed_time` cell and the embedded stop's `fixed_time` are the same cell,
read once — they aren't two competing values, because on these rows they're the same
moment (the day begins or ends exactly when that stop happens). A `day_header` or
`day_end` row with an empty `Plan` behaves exactly as before. `blank` rows still warn
on a non-empty `Plan` — unlike the other structural types, a `blank` row was never
meant to carry anything.

### `day_offset`

Times in the sheet are wall-clock only, with no date attached. That works on paper but
breaks whenever a leg crosses midnight or the date line.

Day 0 is the case: NZ 24 departs Auckland 20:00 and arrives Vancouver 13:00 after
thirteen hours — correct in wall-clock terms, but eastbound across the date line the
arrival lands on a different calendar relationship than elapsed time implies. The app
cannot place that on a timeline without being told.

Set `day_offset` on any stop whose local date differs from the day-header date. `-1`,
`0` and `+1` are the realistic values. Default is `0`.

### Timing semantics

- `floating` — cascades. Arrival is whatever the chain produces.
- `fixed` — an immovable time. `fixed_time` is when the thing happens; `arrive_before`
  is how early you must be there.

Example: the TAG SxS tour is `fixed`, `fixed_time = 16:30`, `arrive_before = 00:30`, so
the effective deadline is 16:00. This is also what the slack calculation runs against.

Every day must have **at least one** constraint. If nothing is explicitly `fixed`, the
`lodging` row's check-in time becomes the terminal constraint.

---

## 3. Sheet: `Checklist`

One row per item, keyed by day. Free text, no tick-state (nothing persists).

| Header | Notes |
|---|---|
| `day` | Integer, matches `Day N` |
| `item` | Free text |

Example: `2` / `Parks Canada pass — buy before Banff`

---

## 4. Sheet: `Time Zones`

Existing tab, unchanged shape. It still exists so the sheet's own XLOOKUP formulas keep
working, but column A now holds IANA names directly instead of shortcodes — there is no
longer a code-to-IANA mapping step, and the previously-planned `iana` column is no longer
needed.

| Header | ETL | Example |
|---|---|---|
| `Code` | ✅ | `America/Vancouver` |
| Offset (hours) | ❌ | `-7` |

Required rows — note this trip starts in Australia and transits New Zealand, so it is not
a North America–only zone set:

- `Australia/Brisbane`
- `Pacific/Auckland`
- `America/Vancouver`
- `America/Edmonton`
- `America/Phoenix`
- `America/Los_Angeles`

`America/Brisbane` is wrong — Brisbane is `Australia/Brisbane`.

`America/Vancouver` and `America/Los_Angeles` were previously collapsed into one `PT`
code; they are now correctly distinct, which resolves an ambiguity flagged in the
previous version of this document.

**Arizona is the trap and the sheet already handles it** — Page and both Antelope Canyon
operators run on `America/Phoenix`, which does not observe DST, while neighbouring Utah
does. US DST ends 1 Nov 2026, after the trip, so no transition occurs mid-trip. Use IANA
zones regardless so this doesn't silently break on a future trip.

---

## 5. Booking documents

Matched to days automatically from the Drive folder using the filename convention:

```
YYYYMMDD Type - Description.pdf
```

- `YYYYMMDD` → matched to the day's date
- `Type` → `Hotel`, `Flight`, `Car`, `Tour`, `Shuttle`, `Activity`, `Waiver`
- Remainder → display title

Three mechanisms, in precedence order:

1. **Explicit `documents` column** — comma-separated filename stems on any row. Attaches
   a document to a specific stop, or to extra days for a multi-night booking.
2. **Date-range filename** — `20261006-20261009 Hotel - X.pdf` attaches to every day in
   the range.
3. **Single-date filename** — the default.

Parser notes:
- Tolerate an optional extra separator after the date (`20260927 - Flight - …`)
- Ignore `.DS_Store` and any non-PDF
- Multiple documents per day are expected
- An unmatched document is a **warning**, not an error — a stray PDF shouldn't break a
  build

**The convention is load-bearing.** One of the 27 files was already four days wrong and
named after the wrong hotel (`20261002 Hotel - Forest Lodge.pdf` was in fact Forest Park
Hotel, Jasper, 6–7 Oct). A mistyped date silently attaches a booking to the wrong day.
Spot-check the remaining files against their contents before trusting auto-match.

At build time the ETL copies all PDFs into the private `where-to-next-docs` R2 bucket
(~27 files, ~7 MB) so the app fetches them via authenticated Worker proxy, caches them
offline, and has no runtime Drive dependency.

---

## 6. Output: `trip.json`

One file for the whole trip. At ~276 stops this is roughly 150–250 KB, ~40 KB gzipped —
small enough to ship whole and precache. No lazy loading, no per-day splitting.

```jsonc
{
  "trip": {
    "title": "Canada + US 2026",
    "start_date": "2026-09-27",
    "end_date": "2026-10-20",
    "generated_at": "2026-08-30T04:12:00Z",
    "source_revision": "<sheet revision id>"
  },

  "days": [
    {
      "day": 2,
      "date": "2026-09-29",
      "leg": "Vancouver to Whistler",
      "start_location": "Vancouver",
      "end_location": "Whistler",
      "timezone": "America/Vancouver",
      "anchor_time": "07:00",
      "sunrise": "07:08",
      "sunset": "18:56",

      "lodging": {
        "name": "Listel Whistler",
        "check_in": "15:00",
        "notes": "Possibly has guest coin-operated laundry"
      },

      "checklist": ["TAG SxS waiver — printed copy"],

      "documents": [
        {
          "id": "20260929-shuttle-tag-sxs",
          "type": "Shuttle",
          "title": "TAG SxS Shuttle Service 4:00pm",
          "url": "/docs/20260929-shuttle-tag-sxs.pdf"
        }
      ],

      "stops": [
        {
          "id": "d02-s03",
          "seq": 3,
          "title": "Shannon Falls",
          "kind": "poi",
          "lat": 49.6725,
          "lng": -123.1583,
          "place_id": "ChIJ...",
          "address_source": "coordinates",
          "timezone": "America/Vancouver",
          "day_offset": 0,
          "how": "drive",
          "travel_minutes": 18,
          "dwell_minutes": 30,
          "timing": "floating",
          "daylight_required": false,
          "notes": "1km loop",
          "price": "Free",
          "links": ["https://..."]
        },
        {
          "id": "d02-s12",
          "seq": 12,
          "title": "TAG SxS Tour",
          "kind": "activity",
          "lat": 50.1163,
          "lng": -122.9574,
          "address_source": "geocoded",
          "timezone": "America/Vancouver",
          "day_offset": 0,
          "how": "drive",
          "travel_minutes": 12,
          "dwell_minutes": 210,
          "timing": "fixed",
          "fixed_time": "16:30",
          "arrive_before": 30,
          "price": "CAD 369.57",
          "documents": ["20260929-shuttle-tag-sxs"]
        }
      ],

      "legs": [
        {
          "from": "d02-s02",
          "to": "d02-s03",
          "how": "drive",
          "distance_m": 14200,
          "duration_s": 1080,
          "polyline": "<encoded>"
        }
      ]
    }
  ]
}
```

### Notes on the shape

- **`day.leg` is derived, not read** — composed as `"<start_location> to <end_location>"`
  from the day's `day_header` and `day_end` rows (§2's `row_type` note explains why
  there's no `leg` row to read it from anymore). If start and end are the same, `leg`
  is just that single location, not `"X to X"`.
- **Durations are integer minutes.** No `HH:MM` strings, no floats.
- **Times are local wall-clock strings** paired with an IANA `timezone`. Never UTC, never
  a fixed offset.
- **`address_source`** records how coordinates were obtained (`coordinates`, `plus_code`,
  `geocoded`, `manual`) so the app can flag low-confidence pins and the validation report
  can list what needs eyeballing.
- **`sunrise`/`sunset` are computed** at build time using `astral` — pure local
  computation, no network call. **Each endpoint comes from a different stop, in that
  stop's own timezone:** `sunrise` from the day's *first stop, in stop order, with
  resolved coordinates*, using that stop's timezone; `sunset` from the *last* such
  stop, using its own timezone. Rationale: you see sunrise where you wake up and
  sunset where you end up — on a day that doesn't move, both resolve to the same
  stop and nothing changes, but on a day that crosses a timezone (Day 0: Brisbane to
  Vancouver; Day 10: Jasper to Hope), computing both from the day's single declared
  timezone put a real place's sun time on a different place's clock. That was the
  original (wrong) rule — computing from the day's declared timezone regardless of
  which stop supplied the coordinates — and it produced sunrise/sunset times up to
  90 minutes off from the sheet's own static lookup on exactly those two days.
  `sunrise_location`/`sunset_location` record which stop and timezone each came from
  (`"<title> (<timezone>)"`), so a travel day's numbers are self-explanatory rather
  than confusing. A day whose two endpoints land in different timezones gets an
  explicit warning naming both — that's a genuine signal about the day's shape, not
  noise. If no stop on a day has resolved coordinates yet, both stay `null` (with
  their `_location` fields) and the ETL warns.
  Any stop with `daylight_required = true` additionally gets its **own** `sunset`
  computed from its **own** coordinates and **own** timezone, for the same reason —
  this is built now even though the sheet's `daylight_required` column is currently
  empty for every row, so expect zero of these until it's populated.
  **This replaces the `Sunrise_Sunset_Data` tab entirely** — the ETL never read it and
  now never needs to; it stays in the sheet purely for the user's own reference. A
  location/date combination where the sun never rises or sets (polar day/night — not
  relevant to this trip's latitudes, but `astral` raises rather than returning `None`)
  is caught and treated the same as unresolvable coordinates: `null` plus a warning,
  not a crash.
- **`legs` polylines are precomputed** at build time — one Routes call per `drive` or
  `walk` leg, a few hundred one-off, well inside the Essentials free tier. This is what
  lets the offline map draw routes with no runtime routing.
- **A `ZERO_RESULTS` leg is a warning, not a build failure.** Google's Routes API
  occasionally can't find a route between two real points (e.g. two viewpoints with no
  connecting road in its graph) — usually a sign the coordinates are wrong, not that the
  whole trip is unbuildable. That leg falls back to the same straight-line geometry the
  non-routed modes (`train`/`shuttle`/`plane`/`transit`) already use, with `duration_s`
  left `null`, and a warning naming the leg. One unroutable leg must not withhold the
  entire published trip; the warning is the more useful signal.
- **`price` is display-only free text.** Deliberately not parsed; no expense tracking.

---

## 7. Validation

The ETL **fails the build** on any of these. Silent coercion produced the current mess;
loud failure is the point.

**Errors (build fails):**
- A required header missing from the sheet
- `row_type` present but not in the enum (empty is a warning, not an error — see §2)
- A `stop` row missing `Plan`, `Travel`, `Fun Time`, or resolvable coordinates
- `kind` present but not in the enum (blank is fine — defaults to `poi`)
- `timing` present but not in the enum (blank is fine — defaults to `floating`)
- `timing = fixed` without `fixed_time`
- `How` not in the enum
- `Travel` or `Fun Time` not parseable as a duration (catches text like `1.00pm`, `Zoo??`)
- `Zone` value not a valid IANA timezone identifier
- Day numbers not contiguous

**Warnings (build passes, logged to the report):**
- `row_type` empty — row skipped, not yet migrated
- A stop with `Fun Time = 0` and `timing = floating`
- A day whose cascade already overruns its own constraints as planned
- A day with no constraint — nothing `fixed` and no `lodging` check-in. **Temporarily
  downgraded from an error**: the sheet has no `fixed_time` column yet, so as an error
  this fails every day by construction, and the schedule engine that actually depends
  on a day having a constraint doesn't exist yet either. Revert to an error once both
  of those land.
- A document whose date matches no day
- A `blank` row with non-empty `Plan` — `blank` is the only row type left with no
  legitimate reason to carry a stop, so content there usually means `row_type` was
  mistyped. (`day_header` and `day_end` legitimately carry `Plan` now — see §2.)
- `resolved_from = geocoded` — flags every stop whose pin was inferred rather than given
- A geocode result, or a self-resolved coordinate pair, falling outside the trip's
  timezone bounding box. **The bad coordinate is discarded, not kept** — the stop's
  `lat`/`lng`/`place_id` are set to `null` and `resolved_from` becomes `unresolved`,
  same as a stop with no Address at all. This is a warning rather than a build
  failure so one bad geocode doesn't block every other stop's `trip.json`; the wrong
  coordinate still never reaches the app. The warning states the rejected coordinates
  and how far outside the box they fell.
- A `geocoded` (address-string) result returning `APPROXIMATE` or `GEOMETRIC_CENTER`
  precision. **Not** raised for `plus_code` results — a plus code always decodes to
  the center of its grid cell, so `GEOMETRIC_CENTER` there is the format working
  correctly, not low confidence. `APPROXIMATE` and `GEOMETRIC_CENTER` are reported
  separately: `APPROXIMATE` means Google found no specific feature and guessed at an
  area, which is materially worse than `GEOMETRIC_CENTER`.

### Validation report

Every run emits `etl/report.md` alongside `trip.json`: rows parsed by type, all warnings,
every stop that was geocoded rather than given exact coordinates, and any unmatched
documents. This lands in the PR diff and is the review surface — read it rather than
diffing 250 KB of JSON.

### Safety guard

The ETL **refuses to run above a fixed geocoding request count per invocation** (default
300) unless an explicit `--allow-bulk` flag is passed. Maps daily quota caps are not
adjustable on this account, so this guard is the only hard stop between a bad loop and
the credit balance.

---

## 8. Migration checklist

One-off, before the ETL can run. Known damage in the current workbook:

- [x] Convert to a native Google Sheet — done, XLOOKUP formulas verified intact
- [x] Add `Address` and `How` columns
- [ ] Add the remaining new columns and the `Checklist` tab. `kind` and `timing` are
      optional — leave them blank except for the exceptions (a non-`poi` kind, or a
      genuinely `fixed` stop); the ETL defaults the rest to `poi`/`floating`
- [ ] Replace shortcode values in the `Zone` column with IANA names, and update column A
      of the `Time Zones` tab to match so the XLOOKUP formulas continue to resolve
- [ ] Populate `row_type` for every row — **do this first**; the phantom rows and the
      shifted row become visually obvious once it's done
- [ ] **Row 45:** hotel name `Listel` is in the `Date` column. Move to the lodging row.
- [ ] **Row 179:** columns shifted — `Plan=06:00`, `Price=Stanley Park`, `Notes=Yes`,
      `Links=$18 parking`. Realign.
- [ ] **Day 0:** text times (`1.00pm`, `2.30pm`) → real time values; set `day_offset` on
      the trans-Pacific legs
- [ ] **`Fun Time`:** prose in a duration column (`Zoo??`, `Stanley park?`) → move to Notes
- [ ] **~180 phantom rows** emitting `Depart/Arrive = 08:00` with no plan →
      `row_type = blank`
- [ ] Populate `Address` for the test-subset stops; paste coordinates or plus codes for
      roadside pullouts rather than relying on name geocoding
- [ ] Spot-check the 26 remaining booking PDFs against their filenames

### Test dataset

**Days 2–4** (Vancouver → Whistler → Revelstoke) plus **Day 18** (15 Oct, Page AZ).

Days 2–4 are the most complete rows and include a timezone crossing. Day 18 is the
stress test: two Antelope Canyon tours in one day, both with hard check-in deadlines, on
Arizona time, with a drive between them. If the slack engine handles Day 18 it handles
everything.

Day 0 is worth adding once `day_offset` is implemented — it's the only date-line case
and nothing else exercises it.

---

## 9. Implementation notes

- ETL is **Python**, in `etl/` with its own `requirements.txt` and a separate CI job —
  don't mix it into the Node toolchain.
- Sheet access via a **service account with Editor** on the sheet only, for coordinate
  write-back. JSON key as the `GOOGLE_SHEETS_SA_KEY` Actions secret. Geocoding key as
  `GOOGLE_GEOCODING_KEY`.
- Suggested libraries: `gspread` (Sheets), `pydantic` (validation), `astral`
  (sunrise/sunset), `polyline` (encoding).
- The ETL is idempotent and produces a deterministic `trip.json` — same inputs, same
  bytes, so a no-op sheet edit doesn't churn the deploy.
- **No clean intermediate sheet.** Considered and rejected: corrections made in a
  generated sheet get overwritten on the next run, since the dirty sheet remains the
  source of truth. `trip.json` in the repo already gives diffable, reviewable output with
  history, and `report.md` covers the inspectability the idea was reaching for. Coordinate
  write-back into the source sheet is what makes corrections persist.
