# where-to-next
Trip itinerary tracker and manager

#Desired features (original version)
- mobile friendly
- front end hostable on github pages or html stored on mobile device/onedrive
- backend and user uploads stored on personal onedrive if possible so can be used offline
- take an excel/csv itinerary input as a database that can be updated and the app output updates live (or on manual refresh)
- location and date tracking so it know where in the itinerary you are
- displays time to leave, behind schedule, estaimted time to destination
- each destination is clickable that pulls up google maps driving directions (waze option preferable)
- clickable links to booking pdfs stored on onedrive
- can use google drive instead of onedrive if more flexible/reliable/easier to set up
- guest viewer version hosted on github pages - read only, lower detail version with broad location tracking ( show a location pin at an itinerary point, or at a "travelling" between two itinerary points, etc)
- ability to upload photos with comments against an itinerary location, have these stored on google/onedrive, so guests can view them. sort of a mini-blog but pinned to days and locations. editable by admin users only.
- guests be able to react to a photo or comment and add their own comments in response.
- adding accounts via an admin settings menu, choose admin or guest type account during creation
- itinerary is split by day. There is a home menu displayed on app startup or accessible by a sticky house button icon in the top left. The home menu shows each day as a selectable button, with the current day in a highlighted colour based on current date). The button text should be Day N (D MMM). There should be an All button that provides the full itinerary sectioned by day but all visible by srolling down. There should be a settings button (which is where the account creation for admin users foes and any other options)
- Ability to switch between dark and light mode
- user settings are remembered per user (not per device)
- long life access tokens (remember this device feature) as implemented in terriblebutler app issue #18 https://github.com/kmbrimble/terriblebutler/issues/18 via commit https://github.com/kmbrimble/terriblebutler/commit/a6bbba6
- ability to add location specific comments like price, opening times, snacks to bring, whatever (custom text field)
- check current traffic / eta for the next driving destination and adjust the leave time accordingly (do not use the "arrive by" feature as this regularly overstates the time required, only use the current time for current destination (not current location as user may be away from their vehicle) to next desination - if flagged as driving there. send mobile/browser notification to user if eta changes materially (> 10%) advising the suggested departure time to arrive at the next destiantion per the itinerary plan
- allow travel to select driving or walking (changes whether eta / departure time is calculated; walking directions should produce a google maps link set to walking mode)
- must work with apple carplay. i.e. the user can load the itinerary browser page on their mobile device and click a destination which then shows the driving directions on apple carplay
- there should be the ability to export the itinerary state including any document and image uploads, comments, reactions, etc to an archivable format at the user's request. Whether this is an itinerary.html with assets in a folder, a PDF document, Word document, or some other user-friendly visual format to allow viewing the mobile version offline in the future as a keepsake.

# where-to-next (current plan as at 23 Aug 2026 1640 AEST)

Trip itinerary tracker and manager.

A personal, offline-first PWA that turns a planned road-trip spreadsheet into a live
schedule engine: what's next, when to leave, how far behind we are, and whether we're
about to miss something we've paid for.

**Status:** planning. No code yet. (Day 0 – Day 23, ~276 stops).

---

## 1. What this is, and what it deliberately isn't

The spreadsheet has run three trips successfully. It is not being replaced because it's
bad — it's being replaced because it can't do the one thing that matters while actually
travelling: **recalculate.**

The workbook is a dependency chain, not a list of times. `Arrive = Depart + Travel ±
timezone delta`, `Depart = previous Arrive + dwell`. Everything cascades from one anchor
time per day. On paper that's a static printout. In an app it's a live engine.

**This project builds only the differentiated part.** Everything else is commodity and
already free elsewhere:

| Need | Solution |
|---|---|
| Live schedule / when to leave / slack to deadline | **Build this.** Nothing else does it. |
| Guest position tracking for family at home | **Build this.** Small, and Polarsteps is overkill. |
| Turn-by-turn navigation | Google Maps app, via deep link |
| Offline navigation maps | Google Maps offline areas, downloaded pre-departure |
| Flight delay alerts | Airline apps already installed |
| Place discovery, POI database, hotel search | Not needed — trip is already researched |
| Booking documents | PDFs already in OneDrive → copied to Google Drive, linked |

Explicit non-goals: it is not a Wanderlog clone, not a planning tool, not a booking
engine, and not an input to the itinerary. **The app is output-only.** Changes are made
in the source spreadsheet and flow forward.

---

## 2. Decisions made

### Architecture

| Layer | Decision |
|---|---|
| Source of truth | Google Sheets (migrated from `Canadia_2026_v2.xlsx`) |
| ETL | GitHub Action → schema-validated JSON → deploy. Fails loudly on bad rows. |
| Frontend | Vite + React + TypeScript, PWA |
| Hosting | Cloudflare Pages at `wheretonext.kiztigs.com` |
| API | One Cloudflare Worker (Maps proxy, auth, position, notify) |
| Mutable state | Cloudflare D1 (position, skip/done ticks, last sync) |
| Plan data | Static JSON, service-worker precached |
| Map tiles | Protomaps PMTiles on R2, rendered with MapLibre GL JS |
| Offline model | IndexedDB snapshot, always renders from last good local copy |

Cloudflare is currently on the free plan, used only for DNS and Zero Trust tunnels.
Workers / D1 / R2 / Pages all need enabling; R2 requires billing details on file even
for free-tier use. Verify current free-tier limits at signup.

### The core design rule

**The app always renders from the last good local snapshot.** Network is an enhancement
that refreshes it, never a prerequisite. Server down, aeroplane mode, or the Icefields
Parkway — the app still works and shows a "Data as of HH:MM" staleness badge.

This is why the home-server dependency was designed out of the critical path.

### Auth

Password-as-role. Two shared passwords (admin, guest), validated **in the Worker**,
issuing a JWT carrying `role`. No user table, no per-person accounts.

- Admin: full detail, reports position, can skip/tick stops
- Guest: read-only, 3 people, single shared password

Accepted trade-offs: cannot revoke one guest without rotating for all; no audit trail of
which admin skipped a stop. Acceptable at this scale.

The password endpoint must be rate-limited — it's an open brute-force target otherwise.

### Devices

- Work phone (iPhone 15) — primary, has the data allowance
- iPhone 17 Pro Max
- Samsung Z Fold 7 (partner)

Any device logged in with the admin token reports location; the app takes the most
recent report.

---

## 3. The schedule engine

This is the heart of the app.

### Cascade model

Store `dwell_minutes`, `travel_minutes`, `timezone`, a per-day anchor, and a
`fixed`/`floating` flag per stop. **Never store computed depart/arrive times as data** —
compute the cascade at render time. That is what makes recalculation possible.

Two modes, both required:
1. **Planned** — cascade from the day's anchor time (what the spreadsheet does today)
2. **Live** — cascade from `now` and actual GPS position

### Slack, not lateness

Materiality is not a threshold on "how late are we". It's **slack to the next
constraint**, and there is always a next constraint:

- an explicitly `fixed` stop (tour check-in, shuttle, flight), or
- sunset, for any stop flagged `daylight_required`, or
- hotel check-in / end of day, as the day's terminal constraint

```
slack = next_constraint_time − (now + Σ remaining travel + Σ remaining dwell)
```

Traffic-light logic, no arbitrary numbers:

| State | Condition | Meaning |
|---|---|---|
| 🟢 Green | `slack > smallest remaining dwell` | Fine. |
| 🟠 Amber | `0 < slack ≤ smallest remaining dwell` | One dropped stop absorbs it. |
| 🔴 Red | `slack < 0` | Must cut. Show exactly how many minutes. |

This satisfies both cases from planning: 10 minutes late matters when a whole dwell time
is at stake (amber), and 30 minutes late matters when a deadline is ahead (red) — same
formula, no special-casing.

When red, list the `floating` stops between here and the constraint with their dwell
times. **The app does not choose what to drop.** The human picks, hits skip, the cascade
re-runs. No priority ranking column is needed for v1.

### Rules

- **Rate limit:** live traffic recalculation at most once per 15 minutes, cached
  server-side and shared across all devices. Manual refresh button bypasses the cache.
- **Skipped stops** stay visible, greyed out, removed from the cascade.
- **Day rollover** at 02:00 local, so a 01:00 arrival still shows as the previous day.
- **Position** for the cascade comes from GPS, not from the last stop ticked.
- **Sunrise/sunset** computed from lat/lng + date (SunCalc), not the static lookup table.

---

## 4. Position tracking

Three sources, in priority order, each tagged with source and age:

1. **HA `device_tracker`** via the existing Home Assistant instance — the companion app
   does true background location on both iOS and Android
2. **Last position POSTed** by an admin device while the app was open — fallback if HA
   is unreachable
3. **Schedule-derived position** — where we *should* be, if both above are stale

Rationale: iOS gives web apps no background geolocation, so a PWA alone can only report
position when open, which is never while driving. HA solves that. Falling back keeps the
feature alive if the home server is down.

Guests see: `Day N`, the day's POI list, a map with pins and route, and current position
— highlighted on a POI if at one, or "Travelling to X…" if between two.

Granularity is deliberately coarse. This is not Life360.

---

## 5. Map view

Offline, using MapLibre GL JS + Protomaps PMTiles hosted on R2.

The key simplification: **no offline routing is needed.** Route polylines are
precomputed at ETL time (one Routes call per leg, one-off) and stored in the JSON. The
map only has to draw a stored line and some pins.

That means tiles can be capped at ~zoom 13 and clipped to a corridor around the route,
since actual navigation happens in Google Maps. Keeps the tile payload cacheable rather
than hundreds of MB.

Shows: today's POIs as pins, the route drawn between them, current position, visited or
passed POIs greyed out.

---

## 6. Navigation deep links

Two buttons per stop — a "G" chip (Google Maps) and a "W" chip (Waze), coloured letters
rather than the official logos.

**Google Maps is the default.** Waze has no offline maps — it depends on live data, and
while it will continue guiding on an already-loaded route after signal drops, it won't
recalculate if you deviate. Given the dead zones on this route (Duffey Lake Road,
Icefields Parkway, North Rim approach, Death Valley, Zion–Mt Carmel), Waze is the
"stuck in LA/Vegas traffic" option, not the primary.

Use stored `lat/lng` in preference to place names — many stops are roadside pullouts that
geocode badly. Deep link opens on the phone; CarPlay then displays the navigation
(via a CarlinKit wireless adapter). CarPlay cannot render web pages — the tap happens on
the handset.

Also supports a walking/driving mode toggle per stop, which just changes a URL parameter.

---

## 7. Google Maps Platform budget

The March 2025 restructure removed the universal $200 credit. Every SKU now has its own
monthly free allowance: **10,000 events for Essentials, 5,000 for Pro, 1,000 for
Enterprise**, resetting on the 1st at midnight US Pacific.

| Use | Tier | Free/month | Expected |
|---|---|---|---|
| Geocoding ~276 stops | Essentials | 10,000 | 276, one-off |
| Route polylines (build time) | Essentials | 10,000 | a few hundred, one-off |
| Compute Routes **with traffic** | Pro | 5,000 | ~1,350 for the whole trip |

Traffic-aware routing is what pushes into the Pro bucket, so **5,000 is the real
ceiling.** A Worker cron polling every 15 min from 06:00–20:00 is 56 calls/day →
~1,350 across 24 days, shared by all devices. Per-device polling would be ~3× that and
is the main way to blow the budget.

Mandatory on setup:
- **Hard quota limit** on Compute Routes (~200/day). Budget alerts only notify; quota
  limits are the actual cap.
- Keep the MapLibre/Google map instance alive across route changes — every new map
  instantiation is a billable load; pan and zoom within one are not.

A new billing account gets $300 welcome credit valid 91 days, which covers development
and the trip. No throwaway test account needed.

---

## 8. Notifications

Target: lock-screen push when ETA slips materially or a fixed deadline is at risk.

**Phase 1 — piggyback Home Assistant.** Worker calls the HA REST API →
`notify.mobile_app_*`. Zero push infrastructure, works on iOS and Android because the HA
companion app is native. Requires a Zero Trust service token or a bypass policy on the
notify endpoint, since `ha.kiztigs.com` sits behind Access.

**Phase 2 — proper Web Push** (VAPID + service worker) if HA proves flaky. Note: Android
web push works unrestricted; **iOS requires the PWA be added to the Home Screen**.

Trigger: ETA change >10%, or slack turning red. Suggest a departure time in the
notification body.

---

## 9. Data

### Source workbook problems to handle in ETL

The current sheet is messy and the ETL must fail loudly rather than ship garbage:

- `Location` carries three meanings: place, leg (`Vancouver to Whistler`), and drive
  duration (`6h 19m`)
- Row 45: hotel name `Listel` sitting in the **Date** column
- Row 179: columns shifted — `Plan=06:00`, `Price=Stanley Park`, `Notes=Yes`
- Day 0 uses text times (`1.00pm`); everywhere else uses real time values
- `Fun Time` mixes durations (`00:30`) with prose (`Zoo??`)
- ~180 phantom rows where formulas emit `Depart/Arrive = 08:00` with no plan
- `Price` is free text (`$25 CAD each`, `CAD369.57`, `Free!`)
- One `Notes` cell is four paragraphs of prose

### Columns to add

`lat`, `lng`, `place_id`, `fixed`/`floating`, `daylight_required`, `documents`
(Google Drive share URL), plus per-day checklist items.

### Timezones

Use IANA zone names (`America/Vancouver`, `America/Edmonton`, `America/Phoenix`), not
fixed UTC offsets. Arizona/Page does not observe DST and both Antelope Canyon operators
run on the Arizona clock — already correctly identified in the workbook. US DST ends
1 Nov 2026, after the trip, so no transition occurs mid-trip.

### Sequencing

Full ETL work starts ~2 weeks before departure, once the itinerary stabilises. Days
12–16 (Vegas/LA) and Day 22 (SF) are still unplanned and will be filled before
departure. **Test dataset: Days 2–4** (Vancouver → Whistler → Revelstoke) — the most
complete rows, and they include a timezone crossing and hard deadlines.

---

## 10. Feature scope

### Phase 1 — must work before 27 Sep

- [ ] Google Sheets → validated JSON ETL
- [ ] Day navigation: home menu, `Day N (D MMM)` buttons, current day highlighted, All view
- [ ] Stop list with times, notes, prices, links
- [ ] Schedule cascade (planned + live modes)
- [ ] Slack / red-amber-green status against next constraint
- [ ] Skip button, greyed-out skipped stops
- [ ] Google + Waze deep links, walking/driving toggle
- [ ] Hourly weather at next stop (Open-Meteo, free, no key)
- [ ] Full offline operation with staleness badge
- [ ] Dark / light mode
- [ ] Password-as-role auth

### Phase 2

- [ ] Offline map view (MapLibre + PMTiles on R2)
- [ ] Guest view + position tracking (HA, with fallbacks)
- [ ] Per-day checklist (park passes, timed-entry reservations, permits, packed lunch)
- [ ] Booking document links, with critical PDFs cached offline
- [ ] Notifications via HA

### Phase 3

- [ ] Web Push proper, if HA proves unreliable
- [ ] Archival export — self-contained `itinerary.html` + assets folder as a keepsake

### Dropped from the original README

Guest accounts, guest commenting, guest reactions, per-user account creation, photo
upload and photo blog, expense tracking, email/booking ingestion, flight delay tracking,
OneDrive as a backend.

---

## 11. Open questions

- Do Phase 1 notifications need the HA route, or is a foreground banner enough to start?
- Which specific booking PDFs must be readable offline? (Cache those, not all of them.)
- Does the partner's Samsung need the HA companion app installed for notifications?
- What granularity for the per-day checklist — free-text list, or typed items with
  tick-state persisted?
- Confirm PMTiles corridor extent and max zoom once the route is finalised.

---

## Related

- Auth pattern reference: [kmbrimble/terriblebutler#18](https://github.com/kmbrimble/terriblebutler/issues/18)
  (device tokens), commit [`a6bbba6`](https://github.com/kmbrimble/terriblebutler/commit/a6bbba6)
