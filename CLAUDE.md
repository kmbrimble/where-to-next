# where-to-next

Read `README.md` for full context before making changes — it has the current architecture
decisions, the schedule-engine design, and phase scope. This file is the quick-reference
summary of what must never be violated.

## What this app is

An offline-first, **read-only** PWA that turns a trip itinerary spreadsheet into a live
schedule view: what's next, when to leave, how much slack is left. It is not a planning
tool, not a booking engine, and not an input to the itinerary.

**The app never writes to the itinerary.** The Google Sheet is the source of truth; a
GitHub Action ETL turns it into validated JSON that the app fetches and caches. There is
no UI path, API endpoint, or Worker route that mutates itinerary content — only
app-local state (position, skip/done ticks, cache) is ever written by the app itself, into
Cloudflare D1, never back to the sheet.

## Architecture

| Layer | Choice |
|---|---|
| Source of truth | Google Sheet, ETL'd to static JSON |
| Frontend | Vite + React + TypeScript, PWA |
| Hosting | Cloudflare Pages |
| API | Cloudflare Worker (proxy, auth, position, notify) |
| Mutable app state | Cloudflare D1 |
| Map tiles | Protomaps PMTiles on R2 |
| Offline model | Last-good local snapshot always renders; network only refreshes it |

## Current phase

**Phase 1: structure only.** No itinerary data models, no schema, no schedule-engine logic
yet — those come in a separate change. What exists now:

- Vite + React + TS scaffold
- `vite-plugin-pwa` (manifest, service worker, icons)
- Dark/light mode via CSS custom properties (`src/index.css`), respecting
  `prefers-color-scheme` — no manual toggle yet, add one when a settings screen exists
- `src/lib/api.ts` — framework-free data-layer stub (fetch + localStorage cache, no
  itinerary schema yet), same pattern as `terriblebutler`'s `client/src/lib/api.ts`: plain
  TypeScript, no React/DOM-library imports, so it's swappable later
- Playwright, with the `data-testid` contract in `docs/TESTIDS.md` — extend that file
  before adding new testids, don't invent ad hoc ones in test files
- `.github/workflows/ci.yml` — build + test on push/PR, no deploy step yet

Don't add itinerary types, ETL code, the schedule cascade, auth, or map rendering until
asked — that's scoped for later changes per `README.md` section 10.
