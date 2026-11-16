import { createHmac } from "crypto";
import { headers as incomingHeaders } from "next/headers";
import { cookies } from "next/headers";
import { NextResponse } from "next/server";

const API = process.env.API_URL || process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export const SESSION_COOKIE = "caliber_session";
const MAX_AGE = 60 * 60 * 24 * 30;

export async function readToken(): Promise<string> {
  const jar = await cookies();
  return jar.get(SESSION_COOKIE)?.value ?? "";
}

export function withSession(response: NextResponse, token: string) {
  response.cookies.set({
    name: SESSION_COOKIE,
    value: token,
    httpOnly: true,
    sameSite: "lax",
    secure: process.env.NODE_ENV === "production",
    path: "/",
    maxAge: MAX_AGE,
  });
  return response;
}

export function clearSession(response: NextResponse) {
  response.cookies.set({
    name: SESSION_COOKIE,
    value: "",
    httpOnly: true,
    sameSite: "lax",
    secure: process.env.NODE_ENV === "production",
    path: "/",
    maxAge: 0,
  });
  return response;
}

export async function authHeaders(request?: Request): Promise<Headers> {
  const source = request?.headers ?? await incomingHeaders();
  const result = new Headers({ "Content-Type": "application/json" });
  // Netlify overwrites this edge header. Never sign arbitrary X-Forwarded-For input.
  const ip = source.get("x-nf-client-connection-ip");
  const secret = process.env.AUTH_PROXY_SECRET;
  if (ip && secret) {
    const stamp = Math.floor(Date.now() / 1000).toString();
    result.set("x-caliber-client-ip", ip);
    result.set("x-caliber-client-time", stamp);
    result.set("x-caliber-client-signature", createHmac("sha256", secret).update(`${stamp}:${ip}`).digest("hex"));
  }
  return result;
}

export async function apiAuth(path: string, init: RequestInit = {}) {
  const token = await readToken();
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  return authFetch(path, { ...init, headers });
}

export async function authFetch(path: string, init: RequestInit = {}, request?: Request) {
  const headers = await authHeaders(request);
  new Headers(init.headers).forEach((v, k) => headers.set(k, v));
  try {
    return await fetch(`${API}${path}`, {
      ...init, headers, cache: "no-store", signal: AbortSignal.timeout(14000),
    });
  } catch {
    return NextResponse.json(
      { detail: "Account service is temporarily unavailable. Please try again." },
      { status: 503, headers: { "Retry-After": "5" } },
    );
  }
}

export async function objectBody(source: { json(): Promise<unknown> }): Promise<Record<string, unknown>> {
  const body = await source.json().catch(() => null);
  return body !== null && typeof body === "object" && !Array.isArray(body) ? body as Record<string, unknown> : {};
}

export function detail(data: { detail?: unknown }, fallback: string) {
  return typeof data.detail === "string" ? data.detail : fallback;
}
