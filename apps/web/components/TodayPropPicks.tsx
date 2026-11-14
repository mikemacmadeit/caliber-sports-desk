"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import type { PropPickCard } from "@/lib/api";

export function TodayPropPicks({ league, date, gameId, market, initialCard = null }: {
  league: string; date: string; gameId?: string; market?: string; pending?: boolean; initialCard?: PropPickCard | null;
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
  const picks = card.picks.filter(p => (!gameId || p.game_id === gameId) && (!market || p.market_id === market) &&
    !p.locked && p.qualification?.eligible && p.qualification.valid_until && Date.parse(p.qualification.valid_until) > now);
  if (!picks.length) return <p className="plate px-5 py-6 text-sm text-mute">No qualifying opportunity right now. A pick needs a verified price, sufficient data, and enough estimated value.</p>;
  const odds = (n: number) => n > 0 ? `+${n}` : String(n);
  return <div className="grid gap-3 md:grid-cols-2">{picks.map(p => <article key={p.pick_id} className="plate px-5 py-5">
    <p className="font-mono text-xs text-mute">{p.away_abbr} at {p.home_abbr} · {p.league_id.toUpperCase()}</p>
    <p className="mt-3 text-paper">{p.sentence}</p>
    <p className="mt-3 text-sm text-gold">{p.bookmaker} · {odds(p.book_price)}</p>
    <p className="mt-2 text-xs text-mute">Verified {p.qualification?.verified_at ? new Date(p.qualification.verified_at).toLocaleTimeString() : "—"}.
      {p.qualification?.minimum_american_odds != null ? ` Pass below ${odds(p.qualification.minimum_american_odds)}.` : ""}</p>
    <p className="mt-2 text-xs text-mute">Research estimate · {Math.round(p.p * 100)}% probability</p>
    <Link href="/track-record" className="mt-3 inline-block text-sm text-gold">View the permanent record →</Link>
  </article>)}</div>;
}
