"use client";

import { useEffect, useState } from "react";
import type { PropPick, PropPickCard } from "@/lib/api";
import { kickoffParts } from "@/lib/format";

export function TodayPropPicks({
  league,
  date,
  gameId,
  market,
  pending = false,
}: {
  league: string;
  date: string;
  gameId?: string;
  market?: string;
  pending?: boolean;
}) {
  const [card, setCard] = useState<PropPickCard | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    const timer = window.setTimeout(() => ctrl.abort(), 120000);
    setCard(null);
    setError(null);
    const params = new URLSearchParams({ league, date });
    fetch(`/api/backend/v1/props/picks?${params}`, { signal: ctrl.signal })
      .then(async (res) => {
        if (!res.ok) throw new Error(`picks ${res.status}`);
        return (await res.json()) as PropPickCard;
      })
      .then((data) => {
        if (!ctrl.signal.aborted) setCard(data);
      })
      .catch((err: unknown) => {
        if (ctrl.signal.aborted) return;
        setError(err instanceof Error ? err.message : "unavailable");
      })
      .finally(() => window.clearTimeout(timer));
    return () => {
      ctrl.abort();
      window.clearTimeout(timer);
    };
  }, [league, date]);

  if (error) return <p className="text-sm text-mute">The prop calls didn’t load.</p>;
  if (!card) return <p className="text-sm text-mute">Choosing the prop calls.</p>;

  const picks = card.picks.filter((pick) => (!gameId || pick.game_id === gameId) && (!market || pick.market_id === market));
  if (picks.length === 0) {
    return <p className="text-sm text-mute">No prop call for this slate.</p>;
  }

  const groups = new Map<string, PropPick[]>();
  for (const pick of picks) {
    const list = groups.get(pick.game_id) ?? [];
    list.push(pick);
    groups.set(pick.game_id, list);
  }

  return (
    <div className="space-y-3">
      {[...groups.entries()].map(([id, rows]) => {
        const first = rows[0];
        const when = first.kickoff_utc ? kickoffParts(first.kickoff_utc) : null;
        return (
          <article key={id} className="plate px-4 py-4">
            {gameId ? null : (
              <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-mute">
                {first.away_abbr} at {first.home_abbr}
                {when ? ` · ${when.time}` : ""}
              </p>
            )}
            <ul className={gameId ? "space-y-2" : "mt-3 space-y-2"}>
              {rows.map((pick) => (
                <li key={`${pick.game_id}-${pick.market_id}`} className={`text-sm leading-relaxed ${pick.loud ? "text-gold" : "text-paper/75"}`}>
                  {pending ? `${pick.sentence.replace(/\.$/, "")}: pending.` : pick.sentence}
                </li>
              ))}
            </ul>
          </article>
        );
      })}
    </div>
  );
}
