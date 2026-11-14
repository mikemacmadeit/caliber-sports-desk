"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import type { PropRow } from "@/lib/api";
import { PropExplorer } from "@/components/PropExplorer";
import { PriceFreshnessBadge } from "@/components/PropTable";
import { TeamMark } from "@/components/TeamMark";
import { dayParts } from "@/lib/days";
import { browserGet, peekBrowserCache } from "@/lib/browserApi";
import { deskPickReason, selectDeskTakes } from "@/lib/atdDeskTakes";
import { kickoffParts, pct } from "@/lib/format";
import { presentTeam } from "@/lib/teams";

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
  const [clock, setClock] = useState(0);
  useEffect(() => {
    const timer = setInterval(() => setClock(n => n + 1), 1000);
    return () => clearInterval(timer);
  }, []);

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

  const deskPicks = useMemo(() => selectDeskTakes(data?.rows || []), [data, clock]);

  if (loading && !(data?.rows?.length)) {
    return <p className="text-sm text-mute">Loading touchdown prices…</p>;
  }
  if (error && !(data?.rows?.length)) {
    return (
      <div className="plate px-6 py-8">
        <p className="font-display text-4xl uppercase leading-none">Picks did not load</p>
        <p className="mt-3 max-w-xl text-sm text-mute">Try again in a minute. This is not an empty slate.</p>
      </div>
    );
  }
  if (!(data?.rows?.length)) {
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

  const showingOther = boardDate !== date;
  const other = dayParts(boardDate);

  return (
    <div className="space-y-10">
      {showingOther ? (
        <p className="text-sm text-mute">
          No touchdown prices for {date === today ? "today" : dayParts(date).monthDay} yet. Showing{" "}
          <span className="text-gold">
            {other.weekday} {other.monthDay}
          </span>
          .
        </p>
      ) : null}

      <section className="space-y-4">
        <div className="border-b border-white/10 pb-2">
          <h2 className="font-display text-4xl uppercase leading-none tracking-wide">Desk picks</h2>
          <p className="mt-2 max-w-xl text-sm text-mute">
            At most one name per game, using scoring opportunities, matchup, and price. These are the ones we grade on the record — a touchdown is a win, no touchdown is a loss.
          </p>
        </div>
        {deskPicks.length === 0 ? (
          <div className="plate px-5 py-5">
            <p className="font-display text-3xl uppercase leading-none">No desk pick locked yet</p>
            <p className="mt-2 max-w-xl text-sm text-mute">
              A desk pick needs scoring work, a current price, and an estimated chance at least four points above the price. The full board is still below.
            </p>
          </div>
        ) : (
          <div className="grid gap-3 md:grid-cols-2">
            {deskPicks.map((row) => (
              <DeskPickCard key={row.outcome_id} row={row} />
            ))}
          </div>
        )}
      </section>

      <section className="space-y-4">
        <div className="border-b border-white/10 pb-2">
          <h2 className="font-display text-4xl uppercase leading-none tracking-wide">The board</h2>
          <p className="mt-2 max-w-xl text-sm text-mute">Every priced touchdown chance for the day.</p>
        </div>
        <PropExplorer rows={data.rows} mode="flat" />
      </section>
    </div>
  );
}

function DeskPickCard({ row }: { row: PropRow }) {
  const when = kickoffParts(row.kickoff_utc);
  const club = presentTeam({ abbreviation: row.team, mark: row.team_mark });
  const chance = row.forecast.p_estimate;
  const reason = deskPickReason(row);
  return (
    <Link href={`/games/${row.game_id}`} className="plate block px-4 py-4">
      <div className="flex items-start justify-between gap-3">
        <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-mute">
          {row.league_id} · {when.day} · {when.time}
          {row.position ? ` · ${row.position}` : ""}
        </p>
        <p className="font-display text-3xl leading-none text-gold">
          {chance == null ? "—" : pct(chance, 0)}
        </p>
      </div>
      <div className="mt-3 flex items-center gap-3">
        <TeamMark team={club} size="sm" />
        <p className="truncate font-display text-3xl uppercase leading-none tracking-wide">{row.player_name}</p>
      </div>
      <p className="mt-2 text-sm text-paper/70">
        {row.away_abbr} at {row.home_abbr}
        {row.book_price != null ? ` · ${row.book_price > 0 ? `+${row.book_price}` : row.book_price}` : ""}
      </p>
      {reason ? <p className="mt-2 text-sm text-mute">{reason}</p> : null}
      {row.desk_analysis ? <div className="mt-3 border-t border-white/10 pt-3 text-xs leading-relaxed text-mute">
        <p className="font-mono uppercase tracking-wide">What could change this pick</p>
        {row.desk_analysis.risks.map(risk => <p key={risk} className="mt-1">{risk}</p>)}
        <p className="mt-2">New research model · live accuracy is still being measured.</p>
      </div> : null}
      <PriceFreshnessBadge freshness={row.price_freshness || row.forecast.price_freshness} />
    </Link>
  );
}
