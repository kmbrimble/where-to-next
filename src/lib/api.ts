// Plain TypeScript, no React or DOM-library imports — same rule as terriblebutler's
// client/src/lib/api.ts: only ambient Web-standard globals (fetch, localStorage), so this
// layer can be swapped (different backend, React Native) without touching UI code.
//
// This app is read-only and offline-first: fetchItinerary() always resolves, falling back
// to the last cached snapshot when the network is unavailable. There is no write path.

const SNAPSHOT_KEY = 'wtn_itinerary_snapshot'
const SNAPSHOT_FETCHED_AT_KEY = 'wtn_itinerary_fetched_at'

// Shape is intentionally unknown here — the itinerary schema lands in a later change.
// Callers should treat this as opaque until that lands.
export type ItinerarySnapshot = unknown

export interface CachedItinerary {
  data: ItinerarySnapshot
  fetchedAt: string
}

function readCachedItinerary(): CachedItinerary | null {
  const raw = localStorage.getItem(SNAPSHOT_KEY)
  const fetchedAt = localStorage.getItem(SNAPSHOT_FETCHED_AT_KEY)
  if (!raw || !fetchedAt) return null
  try {
    return { data: JSON.parse(raw), fetchedAt }
  } catch {
    return null
  }
}

function writeCachedItinerary(data: ItinerarySnapshot): void {
  localStorage.setItem(SNAPSHOT_KEY, JSON.stringify(data))
  localStorage.setItem(SNAPSHOT_FETCHED_AT_KEY, new Date().toISOString())
}

// Fetches the latest published itinerary JSON, caching it for offline use. On failure,
// falls back to the last cached snapshot rather than throwing, per the "always render from
// the last good local copy" rule — the caller should show the returned fetchedAt as the
// staleness badge and surface a soft warning if `fromCache` is true.
export async function fetchItinerary(url: string): Promise<CachedItinerary & { fromCache: boolean }> {
  try {
    const res = await fetch(url)
    if (!res.ok) throw new Error(`Itinerary fetch failed: ${res.status}`)
    const data: ItinerarySnapshot = await res.json()
    writeCachedItinerary(data)
    return { data, fetchedAt: new Date().toISOString(), fromCache: false }
  } catch (err) {
    const cached = readCachedItinerary()
    if (cached) return { ...cached, fromCache: true }
    throw err
  }
}

export function getCachedItinerary(): CachedItinerary | null {
  return readCachedItinerary()
}
