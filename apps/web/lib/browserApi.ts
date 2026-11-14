/** Browser-side API helpers. Always go through the Next.js proxy to avoid CORS. */

const BROWSER_API = "/api/backend";

/** Reuse non-price research across navigation; current prices always reach the API. */
const BOARD_TTL_MS = 10 * 60 * 1000;
const boardCache = new Map<string, { expires: number; value: unknown }>();

function cacheablePath(path: string): boolean {
  const pathname = path.split("?")[0];
  return pathname === "/v1/days" || pathname === "/v1/anytime-td/research";
}

export function peekBrowserCache<T>(path: string): T | null {
  const hit = boardCache.get(path);
  if (!hit || hit.expires <= Date.now()) return null;
  return hit.value as T;
}

export async function browserGet<T>(path: string, signal?: AbortSignal): Promise<T> {
  if (cacheablePath(path)) {
    const hit = boardCache.get(path);
    if (hit && hit.expires > Date.now()) {
      return hit.value as T;
    }
  }
  const res = await fetch(`${BROWSER_API}${path}`, {
    signal,
    // Current opportunities must be reverified by the API.
    cache: "no-store",
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${path} failed: ${res.status} ${text}`);
  }
  const value = (await res.json()) as T;
  if (cacheablePath(path)) {
    boardCache.set(path, { expires: Date.now() + BOARD_TTL_MS, value });
  }
  return value;
}

export async function browserPost<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BROWSER_API}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    cache: "no-store",
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${path} failed: ${res.status} ${text}`);
  }
  return res.json() as T;
}
