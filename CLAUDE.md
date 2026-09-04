# where-to-next

Read `README.md` for full context before making changes — architecture decisions, the
schedule-engine design, and phase scope. `docs/SCHEMA.md` is the sheet/`trip.json`
contract; `docs/INFRASTRUCTURE.md` is the Cloudflare estate. This file is the
quick-reference summary of what must never be violated.

## What this project is

An offline-first, **read-only** PWA that turns a trip itinerary spreadsheet into a live
schedule view: what's next, when to leave, how much slack is left. Not a planning tool,
not a booking engine, not an input to the itinerary.

| Layer | Choice | Lives in |
|---|---|---|
| Source of truth | Google Sheet (`Itinerary v1` tab) | external |
| ETL | Python 3.11 + pydantic → `trip.json` | `etl/` |
| Frontend | Vite + React + TS, PWA | `src/`, `tests/` |
| Hosting | Cloudflare Pages | external |
| API | Cloudflare Worker | `worker/`, `wrangler.toml` |
| Mutable app state | Cloudflare D1 (no tables yet) | external |
| Map tiles | Protomaps PMTiles on R2 | external |

**Built so far:** the PWA scaffold (dark/light via CSS custom properties, `vite-plugin-pwa`,
the `data-testid` contract in `docs/TESTIDS.md`) and **ETL stage 1** — parse and validate
the Itinerary sheet, no network calls. Not built: geocoding, sheet write-back, sunrise/
sunset, route legs, documents, the schedule cascade, auth, and map rendering. Don't add
those until asked; they are scoped per `README.md` §10.

## Test command

    npm run build && npm test                    # frontend: tsc + vite build, then Playwright
    .venv/bin/python -m pytest etl -q            # ETL: 37 tests

**The build is not optional.** `npm test` runs Playwright against `vite preview`, which
serves whatever is already in `dist/` — verified 4 Sep 2026 that breaking `src/App.tsx`
and running `npm test` alone still passes. A red baseline proved with `npm test` on its
own is not a red baseline.

ETL venv setup (the container has no system `pip`; `python3 -m venv` supplies one):

    python3 -m venv .venv && .venv/bin/pip install -r etl/requirements.txt

`.venv/` is gitignored. `.github/workflows/ci.yml` runs the Node half only —
**the ETL pytest suite runs only in `.github/workflows/etl.yml`, which is
`workflow_dispatch` only**, so no push or PR exercises it.

## Deploy and verify

Push to `main` → **Cloudflare Pages builds from its own GitHub integration**
(`npm run build` → `dist`), independent of CI. There is deliberately no deploy step in
`.github/workflows/ci.yml`; its absence is correct, don't add one.

**The Pages build reports nothing back to GitHub** — verified 4 Sep 2026: no GitHub
deployments and no commit statuses on `main`. A green `gh run watch` means CI passed, not
that the deploy succeeded. Verify by fetching `https://wheretonext.kiztigs.com/` and
checking the hashed asset name matches a local `npm run build`.

- **Worker** is not auto-deployed — `npx wrangler deploy` by hand.
- **ETL** has no deploy path at all: `workflow_dispatch` only, and it uploads
  `trip.json` / `report.md` as a run artifact. Nothing publishes `trip.json` yet.

## Non-negotiable constraints

- **The app never writes to the itinerary.** `src/lib/api.ts` has no write path and no
  Worker route mutates itinerary content. The Sheet is the source of truth; changes flow
  one way. Don't add a write path.
- **The ETL never reads a computed column** — `Depart`, `Arrive`, `Sunrise/Sunset` are
  absent from `REQUIRED_HEADERS` in `etl/parse.py` on purpose. The app recomputes the
  cascade from the same inputs, so the sheet and the app can't silently disagree
  (`docs/SCHEMA.md` §1).
- **Columns are matched by normalised header name, never by position** —
  `normalize_header()` and the `index` dict in `etl/parse.py`. Positional indexing is the
  exact corruption this migration exists to remove.
- **Empty `row_type` is a warning + skip; present-but-invalid is a hard error**
  (`etl/parse.py`, `docs/SCHEMA.md` §2 and §7). Skipping is what lets the sheet be
  migrated one range of days at a time rather than all ~276 rows at once. Tightening the
  empty case to an error would strand the migration.
- **`TripMeta.generated_at` stays null.** It's declared in `etl/models.py` and set by
  nothing. Populating it with `now()` breaks
  `test_deterministic_output_same_bytes_twice` and churns the deploy on every run.
- **`wrangler.toml` declares `DB` / `DOCS` / `TILES` bindings that `worker/index.ts`
  doesn't reference.** Stable names reserved for later routes
  (`docs/INFRASTRUCTURE.md`). Not dead config to tidy away.
- **`src/lib/api.ts` imports nothing from React or any DOM library** — plain TypeScript
  over ambient web globals only, the same rule as `terriblebutler`'s
  `client/src/lib/api.ts`, so the data layer stays swappable. Not an oversight.
- **Playwright selects on `data-testid` only.** Extend `docs/TESTIDS.md` first; don't
  invent ids inside a test file, and don't switch a test to a CSS or text selector.
- **D1 `where-to-next` has zero tables, deliberately.** The mutable-state schema isn't
  designed yet; the change that needs D1 designs and migrates it. Don't add tables
  speculatively.

## Code review

### Exposure

Per surface — an ETL-only diff and a frontend diff are not the same risk:

- **`src/` (the PWA)** — internet-facing, **unauthenticated**. Cloudflare Pages at
  `wheretonext.kiztigs.com`, plus `where-to-next.pages.dev` and a `*.pages.dev` URL for
  every preview deployment. No Cloudflare Access in front. Verified 4 Sep 2026: plain
  `GET /` → 200, no login redirect. `score.py -e 3`.
- **`worker/`** — internet-facing, **unauthenticated** at
  `https://where-to-next-api.kizmcd.workers.dev` (workers.dev subdomain enabled;
  `/health` answers publicly). No custom domain, not yet in the Pages request path.
  `-e 3`.
- **`etl/`** — not network-reachable. Runs in GitHub Actions only. `-e 0`.
- R2 `where-to-next-tiles` is public read by design via `tiles.kiztigs.com` (static
  OSM-derived tiles, no repo code). R2 `where-to-next-docs` is private with no access
  route yet.

Auth is designed (`README.md` §2, password-as-role JWT in the Worker) and **not built**.

### Modules that own data users rely on

- **`etl/parse.py`** — owns the entire itinerary: every stop, duration, timezone and
  constraint the app will ever show. Its failure mode is silent loss, not a crash: an
  empty `row_type` is skipped with a warning, and any per-row error `continue`s past that
  row. The guard is that `parse_rows` returns `trip=None` unless `errors` is empty, so a
  bad run publishes nothing rather than a partial trip. **A change that weakens that gate
  is a data-loss change.**
- **`etl/models.py`** — the `trip.json` contract everything downstream reads.
- **`src/lib/api.ts`** — owns the offline snapshot in `localStorage`. In a dead zone it is
  the only copy of the itinerary; `fetchItinerary()` must keep resolving from cache rather
  than throwing.
- **No money, inventory or audit-trail module exists yet.** Two are coming and should be
  scored as data-grade when they land: sheet **write-back** of resolved `lat`/`lng`
  (`docs/SCHEMA.md` §2) is the only path that could damage the user's source of truth, and
  the **geocoding request-count guard** (`docs/SCHEMA.md` §7) is the only hard stop on
  spend, because Maps quotas are not adjustable on this Google Cloud account.

### Infrastructure this repo depends on but does not contain

`docs/INFRASTRUCTURE.md` is the reference. Nothing here implements, and nothing here
should:

- **Cloudflare Pages project `where-to-next`** — build command, output dir, custom domain
  and the GitHub connection all live in Cloudflare. That's why there's no deploy workflow.
- **DNS on the `kiztigs.com` zone** — `CNAME wheretonext.kiztigs.com →
  where-to-next.pages.dev`, added by hand; TLS terminated by Cloudflare.
- **A zone Cache Rule** scoped to `http.host eq "tiles.kiztigs.com"` (30-day edge TTL) is
  what makes tiles cacheable. R2 sends no `Cache-Control` and nothing in this repo sets
  one, deliberately.
- **The R2 custom domain** on `where-to-next-tiles`, plus its auto-created CNAME.
- **Real Cloudflare resources** behind the `wrangler.toml` bindings — D1
  `274df483-6d18-4ab1-8ad0-20fdaf0281e2` and two R2 buckets. The repo only names them.
- **GitHub Actions config** — `vars.GOOGLE_SHEET_ID` and `secrets.GOOGLE_SHEETS_SA_KEY`;
  the Google service account holds Editor on the sheet. Nothing here creates or validates
  them.
- **The Google Sheet itself**, its `Itinerary v1` worksheet name, and the Google Drive
  `Itinerary_2026` booking-PDF folder.
- **Two Google Maps API keys**, each restricted to one API, held outside the repo (Routes
  as a Worker secret, Geocoding as an Actions secret).
- Planned: **Home Assistant** at `ha.kiztigs.com` behind Cloudflare Access, for
  notifications (`README.md` §8).

So "nothing in this repo implements X" for hosting, TLS, DNS, caching, secret supply or
edge auth is a repo-scope artefact — route it to `UNVERIFIABLE FROM THIS REPO` and apply
`--infra`. **The reverse also holds:** there is genuinely no auth anywhere yet, in the
repo or in front of it, so a finding that the app is unauthenticated is true and is not
an infra blind spot.

### Test reality

- **`etl/parse.py` is the only well-tested module** — 37 pytest tests, 80% statement and
  branch coverage measured 4 Sep 2026 (project total 84%). Real error branches are
  exercised: bad durations, prose in `Fun Time`, shifted rows, invalid timezone,
  non-contiguous days, `fixed` without `fixed_time`, missing headers, empty vs invalid
  `row_type`.
- **Untested branches in `etl/parse.py`, all reachable** — treat a change touching any of
  them as `-t 2`/`-t 3`: `day_offset` and `arrive_before` parsing (and their error paths);
  the `drive_total` row type and the `day_end` timezone fallback; lodging with a missing
  name or unparseable check-in; a stop missing `Plan` or with a blank `How`; every
  `day_header` failure (no `Day N`, missing `Date`, bad `Zone`, bad anchor time); a row
  before any `day_header`; the day-timezone-from-first-stop fallback and the no-timezone
  error; the empty-sheet and no-`day_header`-rows cases.
- **`etl/loaders.py`** is near-fully covered, but only against a fake `gspread`. It has
  never run against a real sheet — the module says so itself.
- **`src/` takes the happy path and only the happy path.** One Playwright test asserting
  three testids are visible. **`src/lib/api.ts` has zero tests and there is no JS/TS
  unit-test runner installed** — its offline fallback (fetch fails → serve cached
  snapshot), its `JSON.parse` catch and its `localStorage` writes are all unexercised. Any
  change there is `-t 3`.
- **`worker/index.ts`** has no tests and no harness.
