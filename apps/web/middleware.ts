import { NextRequest, NextResponse } from "next/server";

export function middleware(request: NextRequest) {
  if (request.method !== "GET" && request.method !== "HEAD") {
    const origin = request.headers.get("origin");
    if (origin && origin !== request.nextUrl.origin) {
      return NextResponse.json({ detail: "Request origin is not allowed." }, { status: 403 });
    }
  }
  const response = NextResponse.next();
  response.headers.set("Cache-Control", "private, no-store");
  response.headers.set("Vary", "Cookie");
  return response;
}

export const config = { matcher: ["/api/auth/:path*", "/api/backend/:path*", "/account", "/reset", "/forgot", "/verify"] };
