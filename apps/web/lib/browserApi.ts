/** Browser-side API helpers. Always go through the Next.js proxy to avoid CORS. */

const BROWSER_API = "/api/backend";

export async function browserGet<T>(path: string): Promise<T> {
  const res = await fetch(`${BROWSER_API}${path}`, { cache: "no-store" });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${path} failed: ${res.status} ${text}`);
  }
  return res.json() as Promise<T>;
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
  return res.json() as Promise<T>;
}
