import { authHeaders } from "@/lib/serverAuth";
import { NextResponse } from "next/server";
import { detail, withSession } from "@/lib/serverAuth";

const API = process.env.API_URL || process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export async function POST(request: Request) {
  const body = await request.json().catch(() => ({}));
  const res = await fetch(`${API}/v1/auth/reset`, {
    method: "POST",
    headers: await authHeaders(request),
    signal: AbortSignal.timeout(15000),
    body: JSON.stringify({ token: body.token ?? "", password: body.password ?? "" }),
    cache: "no-store",
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || !data.token) {
    return NextResponse.json({ detail: detail(data, "That reset link is no longer valid.") }, { status: res.status || 400 });
  }
  return withSession(NextResponse.json({ user: data.user }), data.token);
}
