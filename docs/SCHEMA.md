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
- per-stop `travel` and `dwell` durations
- timezone
- the fixed/floating flag and any fixed time

`Depart`, `Arrive`, the drive-total row, and the sunrise/sunset column are all derived,
and all ignored. The app recomputes them from the same inputs. This is deliberate: two
implementations that read the same inputs can't silently disagree about the plan. If the
sheet and the app ever show different times, one of them has a bug — which is worth
knowing.

**Ignored columns**, retained in the sheet, skipped by the ETL:

| Col | Name | Why ignored |
|---|---|---|
| D | Sunrise/Sunset | Computed from lat/lng + date instead |
| E | Depart | Computed |
| G | Arrive | Computed |
| J | *(unnamed)* | Planning opinions, not app data |
| P, Q | Check-in Deadline / Buffer | Superseded by `timing` + `fixed_time` |
| U, V, W | OUT | Rejected alternates, not shipped |

---

## 2. Sheet: `Itinerary`

Migrated in place from `Itinerary v1`. Existing columns keep their positions and
formulas. New columns are appended so nothing shifts.

### Existing columns (unchanged)

| Col | Name | ETL reads | Notes |
|---|---|---|---|
| A | Day | ✅ | `Day N`, only on the day-header row |
| B | Date | ✅ | Date value, not text |
| C | Location | ✅ | Overloaded — see `row_type` below |
| D | Sunrise/Sunset | ❌ | |
| E | Depart | ❌ | Formula |
| F | Travel | ✅ | Duration, previous stop → this stop |
| G | Arrive | ❌ | Formula |
| H | Fun Time | ✅ | Dwell duration |
| I | Plan | ✅ | Stop title |
| J | *(unnamed)* | ❌ | |
| K | Zone | ✅ | Short code, resolved via `Time Zones` tab |
| L | Price | ✅ | Free text, display only — not parsed |
| M | Notes | ✅ | Free text |
| N | Links | ✅ | URL |
| P, Q | Check-in / Buffer | ❌ | |
| U–W | OUT | ❌ | |

### New columns

| Col | Name | Type | Required | Notes |
|---|---|---|---|---|
| AC | `row_type` | enum | ✅ | See below |
| AD | `kind` | enum | stops only | `poi`, `meal`, `activity`, `lodging`, `flight`, `transfer` |
| AE | `lat` | number | stops only | Decimal degrees |
| AF | `lng` | number | stops only | Decimal degrees |
| AG | `place_id` | text | optional | Google Place ID where one exists |
| AH | `timing` | enum | ✅ stops | `fixed` or `floating` |
| AI | `fixed_time` | time | if `fixed` | Local wall-clock, e.g. `16:30` |
| AJ | `arrive_before` | duration | optional | Check-in lead time, e.g. `00:30` |
| AK | `daylight_required` | bool | optional | Treats sunset as a constraint |

### `row_type`

The sheet is currently typed by row position, which is how a shifted row becomes silent
corruption. Making it explicit costs one column and breaks no formulas.

| Value | Meaning | Key columns |
|---|---|---|
| `day_header` | Start of a day | A=Day N, B=date, C=start location |
| `leg` | The day's journey label | B=date, C=`Vancouver to Whistler` |
| `drive_total` | Total driving for the day | C=`1h 29m` — ignored, recomputed |
| `stop` | An itinerary item | F, H, I, K + all new columns |
| `lodging` | Where you sleep that night | I=hotel name |
| `day_end` | Terminal row | C=end location |
| `blank` | Spacer | — |

Rows with `row_type` empty are **rejected**, not guessed at.

### Timing semantics

- `floating` — cascades. Arrival is whatever the chain produces.
- `fixed` — an immovable time. `fixed_time` is when the thing happens;
  `arrive_before` is how early you must be there.

Example: the TAG SxS tour is `fixed`, `fixed_time = 16:30`, `arrive_before = 00:30`, so
the effective deadline is 16:00. This is also the constraint the slack calculation runs
against.

Every day must have **at least one** constraint. If nothing is explicitly `fixed`, the
`lodging` row's check-in time becomes the terminal constraint.

---

## 3. Sheet: `Checklist`

One row per item, keyed by day. Free text, no tick-state (nothing persists).

| Col | Name | Notes |
|---|---|---|
| A | `day` | Integer, matches `Day N` |
| B | `item` | Free text |

Example: `2` / `Parks Canada pass — buy before Banff`

---

## 4. Sheet: `Time Zones`

Existing tab, one column added. The offset column stays so the sheet's XLOOKUP formulas
keep working; the ETL reads only the IANA column.

| Col | Name | ETL | Example |
|---|---|---|---|
| A | Code | ✅ | `PT` |
| B | Offset (hours) | ❌ | `-7` |
| C | `iana` | ✅ | `America/Vancouver` |

Needed values: `America/Vancouver`, `America/Edmonton`, `America/Phoenix`,
`America/Los_Angeles`, `America/Denver`.

**Arizona is the trap and it's already handled in the sheet** — Page/Antelope Canyon runs
on `America/Phoenix`, which does not observe DST, while neighbouring Utah does. US DST
ends 1 Nov 2026, after the trip, so no transition occurs mid-trip — but use IANA zones
anyway so this doesn't silently break on a future trip.

---

## 5. Booking documents

**No sheet column.** Documents are matched to days automatically from the Drive folder
using the existing filename convention:

```
YYYYMMDD Type - Description.pdf
```

- `YYYYMMDD` → matched to the day's date
- `Type` → `Hotel`, `Flight`, `Car`, `Tour`, `Shuttle`, `Activity`, `Waiver`
- Remainder → display title

Parser notes:
- Tolerate an optional extra separator after the date (`20260927 - Flight - …`)
- Ignore `.DS_Store` and any non-PDF
- Multiple documents per day are expected and all attach to that day
- **Unresolved:** two hotels on 2026-10-02 (Hotel Arts Calgary, Forest Lodge). Needs a
  rule — either one is superseded and should be moved out of the folder, or both are
  valid and the day carries two.

At build time the ETL copies all PDFs into R2 (~27 files, ~7 MB total) so the app fetches
them same-origin, caches them offline, and has no runtime Drive dependency.

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

      "checklist": [
        "TAG SxS waiver — printed copy"
      ],

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
          "timezone": "America/Vancouver",
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
          "timezone": "America/Vancouver",
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

- **Durations are integer minutes.** No `HH:MM` strings, no floats.
- **Times are local wall-clock strings** paired with an IANA `timezone`. Never UTC, never
  a fixed offset.
- **`sunrise`/`sunset` are computed** at build time from the day's primary lat/lng using
  `astral`. Any stop with `daylight_required` gets its own sunset computed from its own
  coordinates.
- **`legs` polylines are precomputed** at build time from the Routes API — one call per
  leg, a few hundred one-off, well inside the Essentials free tier. This is what lets the
  offline map draw routes without any runtime routing.
- **`price` is display-only free text.** Deliberately not parsed; no expense tracking.
- **`documents` on a stop** is an optional array of document ids, for the cases where a
  booking belongs to a specific stop rather than the whole day.

---

## 7. Validation

The ETL **fails the build** on any of these. Silent coercion is what produced the current
mess; loud failure is the point.

**Errors (build fails):**
- `row_type` missing or not in the enum
- A `stop` row missing `title`, `travel`, `dwell`, `timing`, `lat`, or `lng`
- `timing = fixed` without `fixed_time`
- `travel` or `dwell` not parseable as a duration (catches text like `1.00pm`, `Zoo??`)
- `lat`/`lng` outside the trip bounding box (roughly 32–55 °N, 100–130 °W)
- Zone code not present in the `Time Zones` tab
- Day numbers not contiguous
- A day with no constraint — nothing `fixed` and no `lodging` check-in

**Warnings (build passes, logged):**
- A stop with `dwell = 0` and `timing = floating`
- A day whose cascade already overruns its own constraints as planned
- A document whose date matches no day
- A stop with no `place_id` (geocoding fell back to raw coordinates)

---

## 8. Migration checklist

One-off, before the ETL can run. Known damage in the current workbook:

- [ ] Convert `Canadia 2026 v2.xlsx` → native Google Sheet, and **verify the XLOOKUP
      formulas survived** the conversion
- [ ] Add the new columns (AC–AK), `Checklist` tab, and `iana` column
- [ ] Populate `row_type` for every row
- [ ] **Row 45:** hotel name `Listel` is in column B (Date). Move to the lodging row.
- [ ] **Row 179:** columns shifted — `Plan=06:00`, `Price=Stanley Park`, `Notes=Yes`,
      `Links=$18 parking`. Realign.
- [ ] **Day 0:** text times (`1.00pm`, `2.30pm`) → real time values
- [ ] **Column H:** prose in a duration column (`Zoo??`, `Stanley park?`) → move to Notes
- [ ] **Rows 40–42:** cascade broken, `Arrive` showing `00:00` — these are Alexander
      Falls, the TAG SxS tour and Vallea Lumina. The last two are `fixed` anyway, so this
      resolves itself once `timing` is set.
- [ ] **~180 phantom rows** emitting `Depart/Arrive = 08:00` with no plan → `row_type = blank`
- [ ] Batch geocode ~276 stops → `lat`, `lng`, `place_id`; **hand-verify roadside
      pullouts**, which geocode badly (Tantalus Lookout, Duffey Lake Viewpoint, Archer
      Point, Tunnel Point)
- [ ] Resolve the duplicate 2026-10-02 hotel bookings

### Test dataset

**Days 2–4** (Vancouver → Whistler → Revelstoke) plus **Day 18** (15 Oct, Page AZ).

Days 2–4 are the most complete rows and include a timezone crossing. Day 18 is the
stress test: two Antelope Canyon tours in one day, both with hard check-in deadlines,
on Arizona time with a drive between them. If the slack engine handles Day 18, it
handles everything.

---

## 9. Implementation notes

- ETL is **Python**, in `etl/` with its own `requirements.txt` and a separate CI job —
  don't mix it into the Node toolchain.
- Sheet access via a **service account** with read-only sharing; JSON key as a GitHub
  Actions secret. Not publish-to-web, since Notes and Price carry booking references.
- Suggested libraries: `gspread` (Sheets), `pydantic` (validation), `astral`
  (sunrise/sunset), `polyline` (encoding).
- The ETL is idempotent and produces a deterministic `trip.json` — same inputs, same
  bytes, so a no-op sheet edit doesn't churn the deploy.
