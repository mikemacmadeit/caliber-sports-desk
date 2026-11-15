import { NextResponse } from "next/server";
import { apiAuth } from "@/lib/serverAuth";

export async function GET() {
  const res = await apiAuth("/v1/auth/me");
  const data = await res.json().catch(() => ({ user: null }));
  return NextResponse.json(res.ok ? data : { user: null });
}
