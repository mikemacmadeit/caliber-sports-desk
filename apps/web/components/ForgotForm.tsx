"use client";

import Link from "next/link";
import { useState } from "react";

export function ForgotForm() {
  const [email, setEmail] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [sent, setSent] = useState(false);
  const [pending, setPending] = useState(false);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setPending(true);
    setError(null);
    try {
      const res = await fetch("/api/auth/forgot", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setError(typeof data.detail === "string" ? data.detail : "That didn’t work.");
        setPending(false);
        return;
      }
      setSent(true);
    } catch {
      setError("The desk didn’t answer. Try again.");
    }
    setPending(false);
  }

  if (sent) {
    return (
      <div className="plate max-w-md space-y-4 px-5 py-6">
        <p className="text-sm text-paper/80">If that email has an account, the reset link is on its way.</p>
        <Link href="/login" className="inline-flex min-h-11 items-center font-display text-xl uppercase tracking-wide text-gold">
          Back to log in
        </Link>
      </div>
    );
  }

  return (
    <form onSubmit={onSubmit} className="plate max-w-md space-y-4 px-5 py-6">
      <label className="block">
        <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-mute">Email</span>
        <input
          className="field-input mt-2"
          type="email"
          maxLength={320}
          autoComplete="email"
          required
          value={email}
          onChange={(event) => setEmail(event.target.value)}
        />
      </label>
      {error ? <p role="alert" className="text-sm text-heat">{error}</p> : null}
      <button
        type="submit"
        disabled={pending}
        className="inline-flex min-h-11 items-center bg-gold px-4 font-display text-xl uppercase tracking-wide text-ink-950 disabled:opacity-60"
      >
        {pending ? "Working" : "Send reset link"}
      </button>
      <p className="text-sm text-mute">
        <Link href="/login" className="text-gold">
          Back to log in
        </Link>
      </p>
    </form>
  );
}
