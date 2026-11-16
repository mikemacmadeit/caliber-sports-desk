import { authHeaders } from "@/lib/serverAuth";
import { NextResponse } from "next/server";
import { detail } from "@/lib/serverAuth";

const API = process.env.API_URL || process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export async function POST(request: Request) {
  const body = await request.json().catch(() => ({}));
  const res = await fetch(`${API}/v1/auth/forgot`, {
    method: "POST",
    headers: await authHeaders(request),
    signal: AbortSignal.timeout(15000),
    body: JSON.stringify({ email: body.email ?? "" }),
    cache: "no-store",
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    return NextResponse.json({ detail: detail(data, "Could not send the reset email.") }, { status: res.status || 503 });
  }
  return NextResponse.json({ ok: true });
}
