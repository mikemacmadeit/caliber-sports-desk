import { authFetch, objectBody } from "@/lib/serverAuth";
import { NextResponse } from "next/server";
import { detail } from "@/lib/serverAuth";

export async function POST(request: Request) {
  const body = await objectBody(request);
  const res = await authFetch("/v1/auth/forgot", {
    method: "POST",
    body: JSON.stringify({ email: body.email ?? "" }),
  }, request);
  const data = await objectBody(res);
  if (!res.ok || data.ok !== true) {
    return NextResponse.json({ detail: detail(data, "Could not send the reset email.") }, { status: res.ok ? 502 : res.status });
  }
  return NextResponse.json({ ok: true });
}
