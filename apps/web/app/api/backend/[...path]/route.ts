import { NextRequest, NextResponse } from "next/server";
import { readToken } from "@/lib/serverAuth";

const API = process.env.API_URL || process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

async function proxy(request: NextRequest, path: string[]) {
  const token = await readToken();
  const url = new URL(`${API}/${path.map(encodeURIComponent).join("/")}`);
  url.search = request.nextUrl.search;
  const headers = new Headers();
  const contentType = request.headers.get("content-type");
  if (contentType) headers.set("content-type", contentType);
  if (token) headers.set("authorization", `Bearer ${token}`);
  const isGet = request.method === "GET" || request.method === "HEAD";
  const boardPath = path.join("/");
  const cacheable =
    isGet &&
    (boardPath.startsWith("v1/props") ||
      boardPath.startsWith("v1/ats") ||
      boardPath.startsWith("v1/days") ||
      boardPath.startsWith("v1/anytime-td") ||
      boardPath.startsWith("v1/parlays/card"));
  const res = await fetch(url, {
    method: request.method,
    headers,
    body: isGet ? undefined : await request.text(),
    // Saved boards are fine to reuse briefly at the edge / browser.
    cache: cacheable ? "force-cache" : "no-store",
    next: cacheable ? { revalidate: 300 } : undefined,
  });
  const outHeaders = new Headers();
  outHeaders.set("content-type", res.headers.get("content-type") || "application/json");
  if (cacheable && res.ok) {
    outHeaders.set("Cache-Control", "public, max-age=60, s-maxage=300, stale-while-revalidate=600");
  } else {
    outHeaders.set("Cache-Control", "no-store");
  }
  return new NextResponse(await res.arrayBuffer(), {
    status: res.status,
    headers: outHeaders,
  });
}

type Ctx = { params: Promise<{ path: string[] }> };

export async function GET(request: NextRequest, ctx: Ctx) {
  return proxy(request, (await ctx.params).path);
}

export async function POST(request: NextRequest, ctx: Ctx) {
  return proxy(request, (await ctx.params).path);
}
