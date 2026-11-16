"use client";
import { Suspense, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
function Verify() {
  const token = useSearchParams().get("token");
  const [message, setMessage] = useState("");
  const [pending, setPending] = useState(false);
  async function verify() {
    setPending(true);
    try {
      const res = await fetch("/api/auth/verify", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token }) });
      const data = await res.json();
      setMessage(res.ok ? "Your email is verified. You can return to your account." : data.detail || "That link could not be verified.");
      if (res.ok) window.history.replaceState(null, "", "/verify");
    } catch { setMessage("Please try again shortly."); }
    finally { setPending(false); }
  }
  return <section className="plate max-w-xl space-y-4 p-6"><h1 className="font-display text-3xl">Verify your email</h1>
    {token ? <button type="button" disabled={pending} onClick={verify} className="min-h-11 bg-gold px-4 text-ink-950">{pending ? "Verifying…" : "Verify email"}</button> : <p>Open the verification link from your email.</p>}
    <p role="status">{message}</p><Link href="/account" className="text-gold">Return to account</Link></section>;
}
export default function VerifyPage() { return <Suspense fallback={<p>Loading…</p>}><Verify /></Suspense>; }
