import { NextResponse } from "next/server";
import { apiAuth, detail } from "@/lib/serverAuth";

export async function POST(request: Request) {
  const body = await request.json().catch(() => ({}));
  const res = await apiAuth("/v1/billing/confirm", {
    method: "POST",
    body: JSON.stringify({ session_id: body.session_id ?? "" }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    return NextResponse.json({ detail: detail(data, "Could not confirm the subscription.") }, { status: res.status });
  }
  return NextResponse.json({ user: data.user });
}
