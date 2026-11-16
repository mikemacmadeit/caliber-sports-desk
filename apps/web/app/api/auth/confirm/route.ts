import { NextResponse } from "next/server";
import { apiAuth, detail, objectBody } from "@/lib/serverAuth";

export async function POST(request: Request) {
  const body = await objectBody(request);
  const res = await apiAuth("/v1/billing/confirm", {
    method: "POST",
    body: JSON.stringify({ session_id: body.session_id ?? "" }),
  });
  const data = await objectBody(res);
  if (!res.ok || data.user === null || typeof data.user !== "object") {
    return NextResponse.json({ detail: detail(data, "Could not confirm the subscription.") }, { status: res.ok ? 502 : res.status });
  }
  return NextResponse.json({ user: data.user });
}
