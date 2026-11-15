"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { PageHeader } from "@/components/PageHeader";

type User = {
  email: string;
  subscribed: boolean;
  subscription_status: string;
  admin?: boolean;
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
  const [pending, setPending] = useState(false);

  async function load() {
    const res = await fetch("/api/auth/me", { cache: "no-store" });
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
    })();
    return () => {
      cancel = true;
    };
  }, [params, router]);

  async function startCheckout() {
    setPending(true);
    setError(null);
    const res = await fetch("/api/auth/checkout", { method: "POST" });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.url) {
      setError(typeof data.detail === "string" ? data.detail : "Checkout is unavailable.");
      setPending(false);
      return;
    }
    window.location.href = data.url;
  }

  async function openPortal() {
    setPending(true);
    setError(null);
    const res = await fetch("/api/auth/portal", { method: "POST" });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.url) {
      setError(typeof data.detail === "string" ? data.detail : "Billing is unavailable.");
      setPending(false);
      return;
    }
    window.location.href = data.url;
  }

  async function logout() {
    await fetch("/api/auth/logout", { method: "POST" });
    router.push("/");
    router.refresh();
  }

  return (
    <div className="space-y-8">
      <PageHeader kicker="Account" title="The desk" lede="One plan. $19.99 a month. The call before kickoff." />
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
          {error ? <p className="text-sm text-heat">{error}</p> : null}
          <div className="flex flex-wrap gap-3">
            {user.admin ? null : user.subscribed ? (
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
        </div>
      ) : null}
    </div>
  );
}
