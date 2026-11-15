"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import type { PropPick, PropPickCard } from "@/lib/api";
import { marketLabel } from "@/lib/format";

export function TodayPropPicks({ league, date, gameId, market, initialCard = null, hideWhenEmpty = false }: {
  league: string; date: string; gameId?: string; market?: string; pending?: boolean; initialCard?: PropPickCard | null; hideWhenEmpty?: boolean;
}) {
  const [card, setCard] = useState(initialCard);
  const [error, setError] = useState(false);
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    let disposed = false;
    let controller: AbortController | null = null;
    setCard(initialCard);
    async function refresh() {
      controller?.abort();
      controller = new AbortController();
      const timer = setTimeout(() => controller?.abort(), 15000);
      try {
        const params = new URLSearchParams({ league, date });
        const response = await fetch(`/api/backend/v1/props/picks?${params}`, { signal: controller.signal, cache: "no-store" });
        if (!response.ok) throw new Error("unavailable");
        const next: PropPickCard = await response.json();
        if (next.degraded) throw new Error("degraded");
        if (!disposed) { setCard(next); setError(false); setNow(Date.now()); }
      } catch { if (!disposed) setError(true); }
      finally { clearTimeout(timer); }
    }
    setError(Boolean(initialCard?.degraded));
    if (!initialCard || initialCard.degraded) void refresh();
    const poll = setInterval(() => void refresh(), 60000);
    const clock = setInterval(() => setNow(Date.now()), 1000);
    return () => { disposed = true; controller?.abort(); clearInterval(poll); clearInterval(clock); };
  }, [league, date, initialCard]);
  if (error) return <p role="status" className="text-sm text-mute">Updates are temporarily unavailable. Current opportunities are paused while prices are verified.</p>;
  if (!card) return <p className="text-sm text-mute">Checking published opportunities…</p>;
  const posted = Array.isArray(card.day);
  const picks = (posted ? card.day! : card.picks).filter(p => (!gameId || p.game_id === gameId) && (!market || p.market_id === market) &&
    !p.locked && (posted || (p.qualification?.eligible && p.qualification.valid_until && Date.parse(p.qualification.valid_until) > now)));
  if (!picks.length) {
    if (hideWhenEmpty) return null;
    return <p className="plate px-5 py-6 text-sm text-mute">No qualifying opportunity right now. A pick needs a verified price, sufficient data, and enough estimated value.</p>;
  }
  return <div className="space-y-3">
    <p className="text-sm text-mute">Today · {tally(picks)}</p>
    <div className="grid gap-3 md:grid-cols-2">{picks.map(p => <PropResultCard key={p.pick_id || `${p.game_id}-${p.market_id}-${p.player_name}`} pick={p} />)}</div>
  </div>;
}

function tally(picks: PropPick[]) {
  const wins = picks.filter(p => p.result === "win").length;
  const losses = picks.filter(p => p.result === "loss").length;
  const pushes = picks.filter(p => p.result === "push").length;
  const voids = picks.filter(p => p.result === "void").length;
  const pending = picks.length - wins - losses - pushes - voids;
  const parts = [`${wins} wins`, `${losses} losses`, `${pending} pending`];
  if (pushes) parts.push(`${pushes} pushes`);
  if (voids) parts.push(`${voids} voids`);
  return parts.join(" · ");
}

function PropResultCard({ pick }: { pick: PropPick }) {
  const won = pick.result === "win";
  const lost = pick.result === "loss";
  const label = won ? "Correct" : lost ? "Wrong" : pick.result === "push" ? "Push" : pick.result === "void" ? "Void" : "Result pending";
  const actual = pick.actual == null ? "" : ` · Actual ${formatActual(pick.actual)}`;
  return (
    <Link href={`/games/${pick.game_id}`} className={`relative block rounded-xl border px-4 py-4 ${won ? "border-green-500 bg-green-950/50" : lost ? "border-red-500 bg-red-950/50" : "plate"}`}>
      {won || lost ? <span aria-label={won ? "Win" : "Loss"} className={`absolute right-3 top-3 flex h-7 w-7 items-center justify-center rounded-full text-xl font-bold ${won ? "bg-green-500 text-black" : "bg-red-500 text-white"}`}>{won ? "✓" : "✕"}</span> : null}
      <p className={`font-mono text-[10px] uppercase tracking-[0.16em] text-mute ${won || lost ? "pr-9" : ""}`}>
        {marketLabel(pick.market_id)}
        {pick.away_abbr && pick.home_abbr ? ` · ${pick.away_abbr} at ${pick.home_abbr}` : ""}
      </p>
      <p className="mt-2 text-paper">{pick.sentence}</p>
      <p className="mt-3 text-sm text-gold">{pick.bookmaker} · {odds(pick.book_price)}</p>
      <p className={`mt-2 text-sm font-semibold ${won ? "text-green-300" : lost ? "text-red-300" : "text-mute"}`}>{label}{actual}</p>
    </Link>
  );
}

function odds(n: number) {
  return n > 0 ? `+${n}` : String(n);
}

function formatActual(value: number) {
  return Number.isInteger(value) ? String(value) : String(value);
}
