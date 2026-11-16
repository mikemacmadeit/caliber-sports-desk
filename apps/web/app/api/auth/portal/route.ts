import { NextResponse } from "next/server";
import { apiAuth, detail, objectBody } from "@/lib/serverAuth";

export async function POST() {
  const res = await apiAuth("/v1/billing/portal", { method: "POST" });
  const data = await objectBody(res);
  if (!res.ok || typeof data.url !== "string" || !data.url) {
    return NextResponse.json({ detail: detail(data, "Billing is unavailable.") }, { status: res.ok ? 502 : res.status });
  }
  return NextResponse.json({ url: data.url });
}
