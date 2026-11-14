"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import type { PropRow } from "@/lib/api";
import { PropExplorer } from "@/components/PropExplorer";

type BoardPayload = { rows: PropRow[]; disclaimer?: string };

export function AnytimeTdBoard({
  league,
  date,
  initial,
  emptyTitle,
  emptyHref,
  emptyLabel,
}: {
  league: string;
  date: string;
  initial: BoardPayload | null;
  emptyTitle: string;
  emptyHref: string | null;
  emptyLabel: string;
}) {
  const [data, setData] = useState<BoardPayload | null>(initial);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(!(initial && (initial.rows?.length ?? 0) > 0));

  useEffect(() => {
    if (initial && (initial.rows?.length ?? 0) > 0) {
      setData(initial);
      setLoading(false);
      setError(null);
      return;
    }
    const ctrl = new AbortController();
    const timer = window.setTimeout(() => ctrl.abort(), 25000);
    setLoading(true);
    setError(null);
    const params = new URLSearchParams({ market: "anytime_td", league, date });
    fetch(`/api/backend/v1/props?${params}`, { signal: ctrl.signal })
      .then(async (res) => {
        if (!res.ok) throw new Error(`props ${res.status}`);
        return (await res.json()) as BoardPayload;
      })
      .then((payload) => {
        if (!ctrl.signal.aborted) setData(payload);
      })
      .catch((err: unknown) => {
        if (ctrl.signal.aborted) return;
        setError(err instanceof Error ? err.message : "unavailable");
      })
      .finally(() => {
        window.clearTimeout(timer);
        if (!ctrl.signal.aborted) setLoading(false);
      });
    return () => {
      ctrl.abort();
      window.clearTimeout(timer);
    };
  }, [league, date, initial]);

  if (loading) {
    return <p className="text-sm text-mute">Loading touchdown prices…</p>;
  }
  if ((data?.rows.length ?? 0) > 0) {
    return <PropExplorer rows={data!.rows} mode="calls" />;
  }
  if (error) {
    return (
      <div className="plate px-6 py-8">
        <p className="font-display text-4xl uppercase leading-none">Picks did not load</p>
        <p className="mt-3 max-w-xl text-sm text-mute">Try again in a minute. This is not an empty slate.</p>
      </div>
    );
  }
  return (
    <div className="plate px-6 py-8">
      <p className="font-display text-4xl uppercase leading-none">{emptyTitle}</p>
      <p className="mt-3 max-w-xl text-sm text-mute">
        The games are on the slate. Sportsbooks have not posted a touchdown price yet.
      </p>
      {emptyHref ? (
        <Link href={emptyHref} className="mt-4 inline-block font-display text-2xl uppercase tracking-wide text-gold">
          {emptyLabel}
        </Link>
      ) : null}
    </div>
  );
}
