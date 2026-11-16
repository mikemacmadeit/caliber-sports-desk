import { NextResponse } from "next/server";
import { apiAuth } from "@/lib/serverAuth";

export async function GET() {
  try {
    const res = await apiAuth("/v1/auth/me");
    if (!res.ok) return NextResponse.json({ detail: "Your account is temporarily unavailable." }, { status: 503 });
    return NextResponse.json(await res.json());
  } catch {
    return NextResponse.json({ detail: "Your account is temporarily unavailable." }, { status: 503 });
  }
}
