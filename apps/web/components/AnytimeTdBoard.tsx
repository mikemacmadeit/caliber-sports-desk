"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import type { PropRow } from "@/lib/api";
import { PropExplorer } from "@/components/PropExplorer";
import { dayParts } from "@/lib/days";

type BoardPayload = { rows: PropRow[]; disclaimer?: string };

async function fetchBoard(league: string, date: string, signal: AbortSignal): Promise<BoardPayload> {
  const params = new URLSearchParams({ market: "anytime_td", league, date });
  const res = await fetch(`/api/backend/v1/props?${params}`, { signal });
  if (!res.ok) throw new Error(`props ${res.status}`);
  return (await res.json()) as BoardPayload;
}

export function AnytimeTdBoard({
  league,
  date,
  today,
  candidateDates,
  emptyTitle,
}: {
  league: string;
  date: string;
  today: string;
  candidateDates: string[];
  emptyTitle: string;
}) {
  const [data, setData] = useState<BoardPayload | null>(null);
  const [boardDate, setBoardDate] = useState(date);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const ctrl = new AbortController();
    const timer = window.setTimeout(() => ctrl.abort(), 20000);
    setLoading(true);
    setError(null);
    setData(null);
    setBoardDate(date);

    (async () => {
      try {
        // Try the selected day first, then the next slate days that already have prices.
        const ordered = [date, ...candidateDates.filter((d) => d !== date)];
        for (const day of ordered) {
          if (ctrl.signal.aborted) return;
          const payload = await fetchBoard(league, day, ctrl.signal);
          if ((payload.rows?.length ?? 0) > 0) {
            if (!ctrl.signal.aborted) {
              setData(payload);
              setBoardDate(day);
            }
            return;
          }
        }
        if (!ctrl.signal.aborted) {
          setData({ rows: [] });
          setBoardDate(date);
        }
      } catch (err: unknown) {
        if (ctrl.signal.aborted) return;
        setError(err instanceof Error ? err.message : "unavailable");
      } finally {
        window.clearTimeout(timer);
        if (!ctrl.signal.aborted) setLoading(false);
      }
    })();

    return () => {
      ctrl.abort();
      window.clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- dates keyed by joined string
  }, [league, date, candidateDates.join("|")]);

  if (loading) {
    return <p className="text-sm text-mute">Loading touchdown prices…</p>;
  }
  if ((data?.rows.length ?? 0) > 0) {
    const showingOther = boardDate !== date;
    const other = dayParts(boardDate);
    return (
      <div className="space-y-4">
        {showingOther ? (
          <p className="text-sm text-mute">
            No touchdown prices for {date === today ? "today" : dayParts(date).monthDay} yet. Showing{" "}
            <span className="text-gold">
              {other.weekday} {other.monthDay}
            </span>
            .
          </p>
        ) : null}
        <PropExplorer rows={data!.rows} mode="calls" />
      </div>
    );
  }
  if (error) {
    return (
      <div className="plate px-6 py-8">
        <p className="font-display text-4xl uppercase leading-none">Picks did not load</p>
        <p className="mt-3 max-w-xl text-sm text-mute">Try again in a minute. This is not an empty slate.</p>
      </div>
    );
  }
  const next = candidateDates.find((d) => d > date) ?? null;
  return (
    <div className="plate px-6 py-8">
      <p className="font-display text-4xl uppercase leading-none">{emptyTitle}</p>
      <p className="mt-3 max-w-xl text-sm text-mute">
        The games are on the slate. Sportsbooks have not posted a touchdown price yet.
      </p>
      {next ? (
        <Link
          href={next === today ? "/anytime-td" : `/anytime-td?date=${next}`}
          className="mt-4 inline-block font-display text-2xl uppercase tracking-wide text-gold"
        >
          Open {dayParts(next).weekday} {dayParts(next).monthDay} →
        </Link>
      ) : null}
    </div>
  );
}
