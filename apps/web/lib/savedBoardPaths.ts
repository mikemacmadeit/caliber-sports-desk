/** Board reads that are safe to reuse. Auth, billing, and simulations stay live. */

const EXACT = new Set([
  "/v1/props",
  "/v1/props/desk",
  "/v1/props/picks",
  "/v1/parlays/card",
  "/v1/ats",
  "/v1/anytime-td/replay",
  "/v1/anytime-td/research",
  "/v1/anytime-td/calls",
  "/v1/anytime-td/called",
  "/v1/desk",
  "/v1/days",
  "/v1/slate",
  "/v1/best-bets",
  "/v1/published/day",
  "/v1/shadow/track-record",
]);

export function boardRequestPath(pathname: string, search = ""): string {
  const normalized = pathname.startsWith("/") ? pathname : `/${pathname}`;
  const params = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
  const query = [...params.keys()]
    .sort()
    .map((key) => `${encodeURIComponent(key)}=${encodeURIComponent(params.get(key) ?? "")}`)
    .join("&");
  return query ? `${normalized}?${query}` : normalized;
}

export function canonicalBoardPath(requestPath: string): string {
  const splitAt = requestPath.indexOf("?");
  const pathname = splitAt === -1 ? requestPath : requestPath.slice(0, splitAt);
  const search = splitAt === -1 ? "" : requestPath.slice(splitAt);
  return boardRequestPath(pathname, search);
}

const PAYWALLED = new Set([
  "/v1/props",
  "/v1/props/desk",
  "/v1/props/picks",
  "/v1/parlays/card",
  "/v1/ats",
  "/v1/desk",
  "/v1/best-bets",
  "/v1/published/day",
  "/v1/published/history",
  "/v1/slate",
  "/v1/shadow/track-record",
]);

/** A saved copy of a paid board is safe only after the API marked it public. */
export function savedBoardIsPublic(requestPath: string, body: string): boolean {
  const pathname = requestPath.split("?")[0];
  if (pathname === "/v1/ats/track-record") return true;
  const parts = pathname.split("/");
  const game = parts[1] === "v1" && parts[2] === "games" && parts.length === 4;
  const anytime = pathname.startsWith("/v1/anytime-td/");
  if (!PAYWALLED.has(pathname) && !game && !anytime) return true;
  try { return JSON.parse(body)?.access === "free"; } catch { return false; }
}

export function isSavedBoardPath(requestPath: string): boolean {
  const pathname = requestPath.split("?")[0];
  if (EXACT.has(pathname)) return true;
  const parts = pathname.split("/");
  return parts[1] === "v1" && parts[2] === "games" && parts.length === 4 && parts[3].length > 0;
}
