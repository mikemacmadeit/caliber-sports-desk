"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { PageHeader } from "@/components/PageHeader";

type User = {
  email: string;
  email_verified?: boolean;
  subscribed: boolean;
  subscription_status: string;
  admin?: boolean;
  billing_available?: boolean;
};

export default function AccountPage() {
  return (
    <Suspense fallback={<p className="text-sm text-mute">Loading the account.</p>}>
      <AccountPanel />
    </Suspense>
  );
}

function AccountPanel() {
  const params = useSearchParams();
  const router = useRouter();
  const [user, setUser] = useState<User | null | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  const [deletePassword, setDeletePassword] = useState("");
  const [notice, setNotice] = useState("");
  const [pending, setPending] = useState(false);

  async function load() {
    const res = await fetch("/api/auth/me", { cache: "no-store" });
    if (!res.ok) throw new Error("Your account is temporarily unavailable.");
    const data = await res.json().catch(() => ({ user: null }));
    setUser(data.user ?? null);
    return data.user as User | null;
  }

  useEffect(() => {
    let cancel = false;
    (async () => {
      const current = await load();
      const sessionId = params.get("session_id");
      if (!cancel && sessionId && current) {
        const res = await fetch("/api/auth/confirm", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session_id: sessionId }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) setError(typeof data.detail === "string" ? data.detail : "Could not confirm the payment.");
        else setUser(data.user ?? current);
        router.replace("/account");
      }
    })().catch(() => { if (!cancel) { setError("Could not load your account. Please refresh the page."); setUser(null); } });
    return () => {
      cancel = true;
    };
  }, [params, router]);

  async function accountAction(action: "verification" | "delete") {
    setPending(true); setError(null); setNotice("");
    try {
      const res = await fetch(`/api/auth/${action}`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(action === "delete" ? { password: deletePassword } : {}) });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Please try again.");
      if (action === "delete") { router.push("/"); router.refresh(); }
      else setNotice("Verification email sent. Open it to confirm your address.");
    } catch (error) { setError(error instanceof Error ? error.message : "Please try again."); }
    finally { setPending(false); }
  }

  async function openBilling(path: string) {
    setPending(true);
    setError(null);
    try {
      const res = await fetch(path, { method: "POST", signal: AbortSignal.timeout(20000) });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.url) throw new Error(typeof data.detail === "string" ? data.detail : "Billing is temporarily unavailable.");
      window.location.href = data.url;
    } catch (error) {
      setError(error instanceof Error ? error.message : "Please try again.");
    } finally { setPending(false); }
  }
  const startCheckout = () => openBilling("/api/auth/checkout");
  const openPortal = () => openBilling("/api/auth/portal");

  async function logout() {
    try {
      const res = await fetch("/api/auth/logout", { method: "POST" });
      if (!res.ok) throw new Error("Could not log out. Please try again.");
      router.push("/");
      router.refresh();
    } catch { setError("Could not log out. Please try again."); }
  }

  return (
    <div className="space-y-8">
      <PageHeader kicker="Account" title="The desk" lede="One plan. $19.99 a month. The call before kickoff." />
      {error && !user ? <p role="alert" className="text-heat">{error}</p> : null}
      {user === undefined ? <p className="text-sm text-mute">Loading the account.</p> : null}
      {user === null ? (
        <div className="plate max-w-md space-y-4 px-5 py-6">
          <p className="text-sm text-paper/80">Log in to subscribe, or create an account.</p>
          <div className="flex flex-wrap gap-3">
            <Link href="/login" className="inline-flex min-h-11 items-center bg-gold px-4 font-display text-xl uppercase tracking-wide text-ink-950">
              Log in
            </Link>
            <Link href="/signup" className="inline-flex min-h-11 items-center px-4 font-display text-xl uppercase tracking-wide text-paper">
              Sign up
            </Link>
          </div>
          <details className="border-t border-white/10 pt-4 text-sm"><summary className="cursor-pointer text-mute">Delete account</summary>
            <p className="mt-3">This removes your login and contact details. Billing audit records remain. Cancel any renewing subscription in billing first.</p>
            <form onSubmit={(event) => { event.preventDefault(); accountAction("delete"); }} className="mt-3 space-y-3">
              <label className="block">Confirm your password<input className="field-input mt-2" type="password" required maxLength={200} autoComplete="current-password" value={deletePassword} onChange={event => setDeletePassword(event.target.value)} /></label>
              <button type="submit" disabled={pending} className="min-h-11 border border-red-400 px-4 text-red-300">Permanently remove my account access</button>
            </form>
          </details>
        </div>
      ) : null}
      {user ? (
        <div className="plate max-w-md space-y-4 px-5 py-6">
          <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-mute">{user.email}</p>
          <p className="font-display text-4xl uppercase leading-none tracking-wide">
            {user.admin ? "Admin" : user.subscribed ? "Subscribed" : "Not subscribed"}
          </p>
          <p className="text-sm leading-relaxed text-paper/70">
            {user.admin
              ? "This desk account sees every call, before kickoff and after."
              : user.subscribed
                ? "The call is open before kickoff. Manage or cancel the monthly plan in billing."
                : "Finals stay on the ledger. Subscribe to see the call before kickoff."}
          </p>
          {!user.email_verified && !user.admin ? <div className="space-y-2 text-sm"><p>Verify your email before starting a subscription.</p><button type="button" disabled={pending} onClick={() => accountAction("verification")} className="min-h-11 text-gold">Send verification email</button></div> : null}
          {notice ? <p role="status">{notice}</p> : null}
          {error ? <p role="alert" className="text-sm text-heat">{error}</p> : null}
          <div className="flex flex-wrap gap-3">
            {user.admin ? null : user.billing_available ? (
              <button
                type="button"
                onClick={openPortal}
                disabled={pending}
                className="inline-flex min-h-11 items-center bg-gold px-4 font-display text-xl uppercase tracking-wide text-ink-950 disabled:opacity-60"
              >
                Billing
              </button>
            ) : (
              <button
                type="button"
                onClick={startCheckout}
                disabled={pending}
                className="inline-flex min-h-11 items-center bg-gold px-4 font-display text-xl uppercase tracking-wide text-ink-950 disabled:opacity-60"
              >
                Subscribe — $19.99
              </button>
            )}
            <button
              type="button"
              onClick={logout}
              className="inline-flex min-h-11 items-center px-4 font-display text-xl uppercase tracking-wide text-paper"
            >
              Log out
            </button>
          </div>
          <details className="border-t border-white/10 pt-4 text-sm"><summary className="cursor-pointer text-mute">Delete account</summary>
            <p className="mt-3">This removes your login and contact details. Billing audit records remain. Cancel any renewing subscription in billing first.</p>
            <form onSubmit={(event) => { event.preventDefault(); accountAction("delete"); }} className="mt-3 space-y-3">
              <label className="block">Confirm your password<input className="field-input mt-2" type="password" required maxLength={200} autoComplete="current-password" value={deletePassword} onChange={event => setDeletePassword(event.target.value)} /></label>
              <button type="submit" disabled={pending} className="min-h-11 border border-red-400 px-4 text-red-300">Permanently remove my account access</button>
            </form>
          </details>
        </div>
      ) : null}
    </div>
  );
}
