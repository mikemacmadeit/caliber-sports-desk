import { NextResponse } from "next/server";
import { apiAuth, clearSession } from "@/lib/serverAuth";

export async function POST() {
  await apiAuth("/v1/auth/logout", { method: "POST" }).catch(() => null);
  return clearSession(NextResponse.json({ ok: true }));
}
