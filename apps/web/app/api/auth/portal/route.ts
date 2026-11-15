import { NextResponse } from "next/server";
import { apiAuth, detail } from "@/lib/serverAuth";

export async function POST() {
  const res = await apiAuth("/v1/billing/portal", { method: "POST" });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    return NextResponse.json({ detail: detail(data, "Billing is unavailable.") }, { status: res.status });
  }
  return NextResponse.json({ url: data.url });
}
