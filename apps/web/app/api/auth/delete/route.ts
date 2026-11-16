import { NextResponse } from "next/server";
import { apiAuth, clearSession } from "@/lib/serverAuth";
export async function POST(request: Request) {
  try {
    const res = await apiAuth("/v1/auth/delete", { method: "POST", body: await request.text() || "{}" });
    const data = await res.json();
    const response = NextResponse.json(data, { status: res.status });
    return res.ok ? clearSession(response) : response;
  } catch { return NextResponse.json({ detail: "Account service is temporarily unavailable. Please try again." }, { status: 503 }); }
}
