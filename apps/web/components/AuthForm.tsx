"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

export function AuthForm({ mode }: { mode: "login" | "signup" }) {
  const router = useRouter();
  const signup = mode === "signup";
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setPending(true);
    setError(null);
    try {
      const res = await fetch(signup ? "/api/auth/signup" : "/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
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
      <label className="block">
        <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-mute">Email</span>
        <input
          className="field-input mt-2"
          type="email"
          autoComplete="email"
          required
          value={email}
          onChange={(event) => setEmail(event.target.value)}
        />
      </label>
      <label className="block">
        <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-mute">Password</span>
        <input
          className="field-input mt-2"
          type="password"
          autoComplete={signup ? "new-password" : "current-password"}
          required
          minLength={8}
          value={password}
          onChange={(event) => setPassword(event.target.value)}
        />
      </label>
      {error ? <p className="text-sm text-heat">{error}</p> : null}
      <button
        type="submit"
        disabled={pending}
        className="inline-flex min-h-11 items-center bg-gold px-4 font-display text-xl uppercase tracking-wide text-ink-950 disabled:opacity-60"
      >
        {pending ? "Working" : signup ? "Create account" : "Log in"}
      </button>
      <p className="text-sm text-mute">
        {signup ? (
          <>
            Already have an account?{" "}
            <Link href="/login" className="text-gold">
              Log in
            </Link>
          </>
        ) : (
          <>
            New here?{" "}
            <Link href="/signup" className="text-gold">
              Create an account
            </Link>
          </>
        )}
      </p>
    </form>
  );
}
