# Infrastructure

Cloudflare resources for where-to-next, and how to recreate them from scratch.

**Account:** Kizmcd@gmail.com's Account (`faf4c1e76f048bd01e63e2a9e5d28dd8`)
**Zone:** `kiztigs.com` (`f643c7e972ac335386a5b064aa0851ca`) — has other production DNS
records and Zero Trust tunnel routes in active use. Nothing on this zone was touched
except adding `wheretonext.kiztigs.com`.
**Plan:** Free, for every resource below. No per-seat or per-month line item was enabled.

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
- Public via the R2 **managed `r2.dev` domain**, enabled through:
  `PUT /accounts/{account_id}/r2/buckets/where-to-next-tiles/domains/managed {"enabled": true}`
- Public URL: `https://pub-71583315e9414153a5552ccb98e51de1.r2.dev`
- **Verified:** a test object returned `200` on a plain GET and `206 Partial Content`
  with `Accept-Ranges: bytes` on a `Range: bytes=0-3` request — PMTiles needs range
  request support to do partial reads of a single large tile archive, and R2 provides
  this natively with no extra configuration. Test object was deleted after verification.
- No custom domain attached yet (`tiles.wheretonext.kiztigs.com` or similar) — the
  r2.dev domain is enough to prove the path works. Add a custom domain when the map
  view (Phase 2) actually needs a stable, brandable URL.

### Recreate

```
npx wrangler r2 bucket create where-to-next-docs
npx wrangler r2 bucket create where-to-next-tiles
npx wrangler r2 bucket dev-url enable where-to-next-tiles
```

(R2 must be enabled once for the account first, via Dashboard → R2 → accept terms. This
gate is manual and can't be done by API/wrangler.)

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

Creating a git-connected Pages project via the API requires the Cloudflare Pages GitHub
App to already be authorized for the account — that's a one-time browser OAuth flow with
no API equivalent. This account hadn't done that step yet, so **the GitHub App
authorization was done interactively, in the Cloudflare dashboard** (Workers & Pages →
Create → Pages → Connect to Git → authorize GitHub → grant access to
`kmbrimble/where-to-next`). Once that authorization existed, the project itself, its
build config, the custom domain, and the first deployment were all created via the API.

The custom domain was attached via
`POST /accounts/{account_id}/pages/projects/where-to-next/domains {"name": "wheretonext.kiztigs.com"}`.
**Unlike the documentation implies, Cloudflare did not auto-create the DNS record** —
the domain sat in `status: pending` with `verification_data.error_message: "CNAME record
not set"` until a `CNAME wheretonext.kiztigs.com → where-to-next.pages.dev` (proxied) was
added explicitly. This was the only DNS record added to the zone; nothing existing was
touched. The first production deployment was triggered manually (ad-hoc, from the HEAD
of `main`) since connecting a project via the API — unlike the dashboard flow — doesn't
auto-trigger an initial build.

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
- No custom domain on the `where-to-next-tiles` bucket or the Worker — only Pages has
  one, since that's the only piece a user hits directly right now.
- No KV, Durable Objects, Queues, or anything else not explicitly asked for.
