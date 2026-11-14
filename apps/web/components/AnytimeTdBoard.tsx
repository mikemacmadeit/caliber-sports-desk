"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import type { PropRow } from "@/lib/api";
import { PropExplorer } from "@/components/PropExplorer";
import { dayParts } from "@/lib/days";
import { browserGet, peekBrowserCache } from "@/lib/browserApi";

type BoardPayload = { rows: PropRow[]; disclaimer?: string };

function boardPath(league: string, date: string): string {
  const params = new URLSearchParams({ market: "anytime_td", league, date });
  return `/v1/props?${params}`;
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
  const path = boardPath(league, date);
  const cached = peekBrowserCache<BoardPayload>(path);
  const [data, setData] = useState<BoardPayload | null>(cached);
  const [boardDate, setBoardDate] = useState(date);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(!(cached && (cached.rows?.length ?? 0) > 0));

  useEffect(() => {
    const ctrl = new AbortController();
    const warm = peekBrowserCache<BoardPayload>(boardPath(league, date));
    if (warm && (warm.rows?.length ?? 0) > 0) {
      setData(warm);
      setBoardDate(date);
      setLoading(false);
      setError(null);
      return () => ctrl.abort();
    }

    setLoading(true);
    setError(null);

    (async () => {
      try {
        // One request for the selected day. If empty, one pass over later days
        // from memory/network — no multi-second retry loops.
        const ordered = [date, ...candidateDates.filter((d) => d !== date)];
        for (const day of ordered) {
          if (ctrl.signal.aborted) return;
          const payload = await browserGet<BoardPayload>(boardPath(league, day), ctrl.signal);
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
        if (!ctrl.signal.aborted) setLoading(false);
      }
    })();

    return () => ctrl.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- dates keyed by joined string
  }, [league, date, candidateDates.join("|")]);

  if (loading && !(data?.rows?.length)) {
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
  const nextParams = new URLSearchParams();
  if (league && league !== "all") nextParams.set("league", league);
  if (next && next !== today) nextParams.set("date", next);
  const nextHref = nextParams.size ? `/anytime-td?${nextParams}` : "/anytime-td";
  return (
    <div className="plate px-6 py-8">
      <p className="font-display text-4xl uppercase leading-none">{emptyTitle}</p>
      <p className="mt-3 max-w-xl text-sm text-mute">This day&apos;s card is not on the desk yet.</p>
      {next ? (
        <Link href={nextHref} className="mt-4 inline-block font-display text-2xl uppercase tracking-wide text-gold">
          Open {dayParts(next).weekday} {dayParts(next).monthDay} →
        </Link>
      ) : null}
    </div>
  );
}
