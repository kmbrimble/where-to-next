# Infrastructure

Cloudflare resources for where-to-next, and how to recreate them from scratch.

**Account:** Kizmcd@gmail.com's Account (`faf4c1e76f048bd01e63e2a9e5d28dd8`)
**Zone:** `kiztigs.com` (`f643c7e972ac335386a5b064aa0851ca`) — has other production DNS
records and Zero Trust tunnel routes in active use. Nothing on this zone was touched
except adding `wheretonext.kiztigs.com`.
**Plan:** Free, for every resource below. No per-seat or per-month line item was enabled.

> ### ⚠️ Two steps in this setup cannot be done via API — plan for them
>
> 1. **Enabling R2 on the account** (Dashboard → R2 → accept terms). Every
>    `r2_bucket_create` call fails with `10042` until this is done once, by a human, in
>    a browser.
> 2. **Authorizing the Cloudflare Pages GitHub App** for the account (Dashboard →
>    Workers & Pages → Create → Pages → Connect to Git → authorize GitHub). Every
>    attempt to create a git-connected Pages project via the API fails with `8000011`
>    until this is done once, by a human, in a browser.
>
> Both errors are non-obvious about their actual cause. Do these two dashboard steps
> *before* attempting to script the rest of this setup, or expect to burn time
> diagnosing them from a generic-sounding error code.

---

## R2 buckets

Two buckets, deliberately separate, because they have different access requirements.

### `where-to-next-docs` — private

- Holds booking PDFs: names, addresses, confirmation numbers.
- **No public access enabled** (no r2.dev domain, no custom domain). Default R2 buckets
  are private; nothing was done to expose this one.
- Intended access path: an authenticated Worker route added later, once auth exists. No
  such route exists yet — Phase 1 scope stops at proving the bucket exists.

### `where-to-next-tiles` — public read

- Holds Protomaps PMTiles vector tiles (OpenStreetMap derived).
- Public via a **custom domain, `tiles.kiztigs.com`**, attached with:
  `POST /accounts/{account_id}/r2/buckets/where-to-next-tiles/domains/custom
  {"domain": "tiles.kiztigs.com", "zoneId": "<kiztigs.com zone id>", "enabled": true}`
- **Why not r2.dev:** the bucket was originally served from the managed `r2.dev`
  subdomain. Cloudflare documents `pub-*.r2.dev` as rate-limited and unsuitable for
  production, and — the more important reason for this app — it bypasses Cloudflare's
  CDN cache entirely, so every tile request hits R2 directly instead of being served
  from the edge. Moved to `tiles.kiztigs.com` and the managed domain was then disabled
  (`PUT .../domains/managed {"enabled": false}`); the old `pub-*.r2.dev` URL now returns
  `401` and is dead.
- **R2 auto-created the DNS record** for this one (unlike Pages, see below) — a
  `CNAME tiles.kiztigs.com → public.r2.dev` (proxied) appeared on the zone as soon as
  the custom domain was attached, tagged `read_only: true` with `r2_bucket:
  where-to-next-tiles` in its metadata. No DNS edit was needed by hand.
- **A zone Cache Rule was required to actually get cache HITs.** R2 objects carry no
  `Cache-Control` header by default, so Cloudflare classifies every response
  `cf-cache-status: DYNAMIC` and never caches it — attaching the custom domain alone
  does not fix this. Added one rule to the zone's `http_request_cache_settings` phase,
  scoped to the exact hostname (not a wildcard, since this zone also serves Immich, Home
  Assistant, the camera UI, and a kiosk — a broader match risks caching content that
  should never be cached):
  ```
  expression: http.host eq "tiles.kiztigs.com"
  action: set_cache_settings
  action_parameters: { cache: true, edge_ttl: { mode: "override_origin", default: 2592000 } }
  ```
  30-day edge TTL, because tiles are immutable — a rebuilt PMTiles archive is uploaded
  under a new object key, never overwritten in place, so there's no staleness to guard
  against. This makes the cache correctness a property of the hostname, not of whatever
  tool last wrote to the bucket (the ETL, a manual re-upload, anything else) — putting
  `Cache-Control` on upload instead would have made caching silently depend on every
  future uploader remembering to set it.
- **Verified**, with a fresh test object each time (deleted after):
  - Plain `GET` → `200`, `Accept-Ranges: bytes` present.
  - First `GET` after upload → `cf-cache-status: MISS`; a second `GET` on the same
    object → `cf-cache-status: HIT`. Confirms the Cache Rule is doing something, not
    just present.
  - `Range: bytes=0-3` → `206 Partial Content` with a correct `Content-Range` header,
    both on the uncached (MISS) and cached (HIT) path. **Caveat:** unlike a direct R2
    response, Cloudflare's edge does not repeat `Accept-Ranges: bytes` on the `206`
    response itself — only on plain `200`/`HEAD` responses. The range mechanics
    (`206` + correct byte range) work regardless; only that one header is absent on the
    partial response. PMTiles clients detect range support from a prior `GET`/`HEAD`,
    not from the `206` response, so this doesn't affect functionality — noted here so
    it isn't mistaken for a broken deployment later.

### Recreate

```
npx wrangler r2 bucket create where-to-next-docs
npx wrangler r2 bucket create where-to-next-tiles
```

Then attach `tiles.kiztigs.com` as a custom domain on `where-to-next-tiles` (Dashboard →
R2 → where-to-next-tiles → Settings → Custom Domains, or the API call above) and add the
Cache Rule described above — the custom domain step alone does not make tiles cacheable.

> **⚠️ Manual dashboard step, no API equivalent:** R2 must be enabled once for the
> account before any bucket can be created — Dashboard → R2 → accept terms (may also
> require billing details on file, even for free-tier usage). Every `r2_bucket_create`
> call fails with `10042: Please enable R2 through the Cloudflare Dashboard` until this
> is done. See also the Pages section below for the second such manual step.

---

## D1 database

| Field | Value |
|---|---|
| Name | `where-to-next` |
| UUID | `274df483-6d18-4ab1-8ad0-20fdaf0281e2` |
| Tables | none — created empty, deliberately. The mutable-state schema (position,
  skip/done ticks, last sync — see `README.md` §2, §4) is not designed yet. Do not add
  tables speculatively; the next change that needs D1 designs and migrates it. |

### Recreate

```
npx wrangler d1 create where-to-next
```

---

## Worker: `where-to-next-api`

Minimal proof-of-deploy Worker. No routes beyond `/health`, no auth, no business logic,
no bindings wired up yet (the bindings below are declared in `wrangler.toml` for when
real routes are added, but the deployed script doesn't reference them).

- Source: `worker/index.ts`
- Deployed via the Cloudflare API (module upload), since no interactive `wrangler login`
  session was available in this environment
- **workers.dev subdomain enabled**: `https://where-to-next-api.kizmcd.workers.dev`
- **Verified:** `GET /health` → `200 {"status":"ok"}`. Any other path → `404`.
- No custom route/domain attached to the Worker — it isn't part of the request path from
  `wheretonext.kiztigs.com` yet. That wiring happens when the Worker gains actual API
  routes (Maps proxy, auth, position, notify — see `README.md` §2).

### Recreate

```
npx wrangler deploy
npx wrangler deployments list where-to-next-api   # confirm
npx wrangler triggers deploy   # or toggle workers.dev in the dashboard
```

---

## Pages project: `where-to-next`

| Field | Value |
|---|---|
| Project ID | `8fa9efa6-64e2-4863-8b09-bfb804270114` |
| Repo | `kmbrimble/where-to-next`, branch `main` |
| Build command | `npm run build` |
| Output directory | `dist` |
| `*.pages.dev` subdomain | `where-to-next.pages.dev` |
| Custom domain | `wheretonext.kiztigs.com` |

> **⚠️ Manual dashboard step, no API equivalent:** creating a git-connected Pages
> project via the API requires the Cloudflare Pages GitHub App to already be authorized
> for the account — that's a one-time browser OAuth flow with no API equivalent. This
> account hadn't done that step, so **the GitHub App authorization was done
> interactively, in the Cloudflare dashboard** (Workers & Pages → Create → Pages →
> Connect to Git → authorize GitHub → grant access to `kmbrimble/where-to-next`).
> Skipping this and going straight to `POST .../pages/projects` with a `github` source
> fails with `8000011: internal issue with your Cloudflare Pages Git installation` —
> not an obviously-named error for "the GitHub App was never installed." Anyone
> recreating this should do the dashboard connect step *first*, expect to lose time to
> this error otherwise. Once authorization existed, the project itself, its build
> config, the custom domain, and the first deployment were all created via the API.

The custom domain was attached via
`POST /accounts/{account_id}/pages/projects/where-to-next/domains {"name": "wheretonext.kiztigs.com"}`.
**Discrepancy from Cloudflare's documentation:** the docs imply this auto-creates the
required DNS record, and it did not — the domain sat in `status: pending` with
`verification_data.error_message: "CNAME record not set"` until a
`CNAME wheretonext.kiztigs.com → where-to-next.pages.dev` (proxied) was added
explicitly. (Contrast with the R2 custom domain above, which *did* auto-create its
CNAME — the two features behave differently here.) This was the only DNS record added
to the zone for Pages; nothing existing was touched. The first production deployment
was triggered manually (ad-hoc, from the HEAD of `main`) since connecting a project via
the API — unlike the dashboard flow — doesn't auto-trigger an initial build.

**Verified 2026-08-30, after cert issuance finished:** `verification_data.status`,
`validation_data.status`, and top-level `status` are all now `active`. It briefly showed
`pending` immediately after creation while the dedicated Google-issued cert was being
provisioned — that's expected and resolved on its own within a couple of minutes; no
action was needed.

### Recreate

1. Dashboard → Workers & Pages → Create → Pages → Connect to Git (one-time GitHub App
   authorization if not already done for the account).
2. Select `kmbrimble/where-to-next`, branch `main`.
3. Build command `npm run build`, output directory `dist`.
4. Add custom domain `wheretonext.kiztigs.com` (Pages project → Custom domains).
5. Add the `CNAME wheretonext.kiztigs.com → where-to-next.pages.dev` DNS record
   yourself if Cloudflare doesn't create it automatically — check the domain's status
   before assuming it did.

No deploy step was added to `.github/workflows/ci.yml` — Pages builds directly from the
GitHub push/webhook, independent of the CI workflow, which stays build-and-test only.

---

## `wrangler.toml`

Committed, no secrets in it (D1 UUID and bucket names are not secrets — they're
account-scoped resource identifiers, meaningless without the paired API token). Binding
names (`DB`, `DOCS`, `TILES`) chosen now so future Worker code has a stable interface;
no code reads them yet.

```toml
name = "where-to-next-api"
main = "worker/index.ts"
compatibility_date = "2026-08-30"

[[d1_databases]]
binding = "DB"
database_name = "where-to-next"
database_id = "274df483-6d18-4ab1-8ad0-20fdaf0281e2"

[[r2_buckets]]
binding = "DOCS"
bucket_name = "where-to-next-docs"

[[r2_buckets]]
binding = "TILES"
bucket_name = "where-to-next-tiles"
```

---

## What's deliberately not done

- No Worker routes beyond `/health` — no auth, no D1 reads/writes, no R2 proxy. That's
  scoped to the phase that designs the mutable-state schema and the auth model
  (`README.md` §2, Phase 1 checklist).
- No custom domain on the Worker — it isn't part of any request path yet.
- No KV, Durable Objects, Queues, or anything else not explicitly asked for.
