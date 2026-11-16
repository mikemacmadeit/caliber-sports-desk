import { authFetch, objectBody } from "@/lib/serverAuth";
import { NextResponse } from "next/server";
import { detail, withSession } from "@/lib/serverAuth";

export async function POST(request: Request) {
  const body = await objectBody(request);
  const res = await authFetch("/v1/auth/reset", {
    method: "POST",
    body: JSON.stringify({ token: body.token ?? "", password: body.password ?? "" }),
  }, request);
  const data = await objectBody(res);
  if (!res.ok || typeof data.token !== "string" || !data.token) {
    return NextResponse.json({ detail: detail(data, "That reset link is no longer valid.") }, { status: res.ok ? 502 : res.status });
  }
  return withSession(NextResponse.json({ user: data.user }), data.token);
}
