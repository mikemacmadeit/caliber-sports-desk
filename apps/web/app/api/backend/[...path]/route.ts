import { NextRequest, NextResponse } from "next/server";

const API = process.env.API_URL || process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

async function proxy(request: NextRequest, path: string[]) {
  const token = request.cookies.get("caliber_session")?.value;
  if (request.method === "POST" && request.headers.get("origin") && request.headers.get("origin") !== request.nextUrl.origin) {
    return NextResponse.json({ detail: "Request origin is not allowed." }, { status: 403 });
  }
  const url = new URL(`${API}/${path.map(encodeURIComponent).join("/")}`);
  url.search = request.nextUrl.search;
  const headers = new Headers();
  const contentType = request.headers.get("content-type");
  if (contentType) headers.set("content-type", contentType);
  if (token) headers.set("authorization", `Bearer ${token}`);
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 20000);
  try {
    const res = await fetch(url, {
      method: request.method, headers, cache: "no-store", signal: ctrl.signal,
      body: request.method === "GET" ? undefined : await request.text(),
    });
    const body = await res.arrayBuffer();
    return new NextResponse(body, { status: res.status, headers: {
      "Content-Type": res.headers.get("content-type") || "application/json",
      "Cache-Control": "private, no-store", "Vary": "Cookie",
      ...(res.headers.get("x-request-id") ? { "X-Request-ID": res.headers.get("x-request-id")! } : {}),
      ...(res.headers.get("server-timing") ? { "Server-Timing": res.headers.get("server-timing")! } : {}),
      ...(res.headers.get("retry-after") ? { "Retry-After": res.headers.get("retry-after")! } : {}),
    } });
  } catch {
    return NextResponse.json({ detail: "The board is temporarily unavailable. Please try again." }, { status: 503 });
  } finally { clearTimeout(timer); }
}

type Ctx = { params: Promise<{ path: string[] }> };
export async function GET(request: NextRequest, ctx: Ctx) { return proxy(request, (await ctx.params).path); }
export async function POST(request: NextRequest, ctx: Ctx) { return proxy(request, (await ctx.params).path); }
