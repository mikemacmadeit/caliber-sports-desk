"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";

export function ResetForm() {
  const router = useRouter();
  const params = useSearchParams();
  const token = params.get("token") || "";
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setPending(true);
    setError(null);
    try {
      const res = await fetch("/api/auth/reset", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token, password }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setError(typeof data.detail === "string" ? data.detail : "That didn’t work.");
        setPending(false);
        return;
      }
      router.push("/account");
      router.refresh();
    } catch {
      setError("The desk didn’t answer. Try again.");
      setPending(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="plate max-w-md space-y-4 px-5 py-6">
      {token ? null : <p className="text-sm text-heat">This reset link is missing. Ask for a new one.</p>}
      <label className="block">
        <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-mute">New password</span>
        <input
          className="field-input mt-2"
          type="password"
          maxLength={200}
          autoComplete="new-password"
          required
          minLength={8}
          value={password}
          onChange={(event) => setPassword(event.target.value)}
        />
      </label>
      {error ? <p role="alert" className="text-sm text-heat">{error}</p> : null}
      <button
        type="submit"
        disabled={pending || !token}
        className="inline-flex min-h-11 items-center bg-gold px-4 font-display text-xl uppercase tracking-wide text-ink-950 disabled:opacity-60"
      >
        {pending ? "Working" : "Save password"}
      </button>
      <p className="text-sm text-mute">
        <Link href="/forgot" className="text-gold">
          Send a new link
        </Link>
      </p>
    </form>
  );
}
