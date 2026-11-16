/** Personal boards always come from the authenticated API. No persisted browser copies. */
const BROWSER_API = "/api/backend";
const calendarCache = new Map<string, { expires: number; value: unknown }>();
function cacheablePath(path: string) { return path.split("?")[0] === "/v1/days"; }
export function peekBrowserCache<T>(path: string): T | null {
  const hit = calendarCache.get(path);
  return cacheablePath(path) && hit && hit.expires > Date.now() ? hit.value as T : null;
}
export function rememberBrowserCache(path: string, value: unknown, ttlMs = 30000) {
  if (cacheablePath(path)) calendarCache.set(path, { expires: Date.now() + Math.min(ttlMs, 30000), value });
}
export async function browserGet<T>(path: string, signal?: AbortSignal): Promise<T> {
  const hit = peekBrowserCache<T>(path);
  if (hit) return hit;
  const res = await fetch(`${BROWSER_API}${path}`, { signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(15000)]) : AbortSignal.timeout(15000), cache: "no-store" });
  if (!res.ok) throw new Error("The board is temporarily unavailable. Please try again.");
  const value = await res.json() as T;
  rememberBrowserCache(path, value);
  return value;
}
export async function browserPost<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BROWSER_API}${path}`, { method: "POST", cache: "no-store", signal: AbortSignal.timeout(15000),
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!res.ok) throw new Error("That request could not be completed. Please try again.");
  return res.json() as Promise<T>;
}
