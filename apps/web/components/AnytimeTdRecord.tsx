"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import type { AtdScoreRow, AtdTake } from "@/components/AnytimeTdScorecard";
import { kickoffDate, kickoffMs, kickoffParts, pct } from "@/lib/format";

const FAMILY_RANK: Record<string, number> = {
  blend_v1_c_recent: 0,
  m5_c_recent: 1,
  player_td_v1: 2,
};

type Filter = "all" | "win" | "loss";

export function AnytimeTdRecord({
  rows,
  takes,
  league = "all",
}: {
  rows: AtdScoreRow[];
  takes: AtdTake[];
  league?: string;
}) {
  const [filter, setFilter] = useState<Filter>("all");
  const graded = useMemo(() => dedupeTakes(takes, league), [takes, league]);
  const ordered = useMemo(
    () => [...graded].sort((a, b) => happenedMs(b) - happenedMs(a) || nameOf(a).localeCompare(nameOf(b))),
    [graded],
  );
  const oldestFirst = useMemo(
    () => [...graded].sort((a, b) => happenedMs(a) - happenedMs(b) || nameOf(a).localeCompare(nameOf(b))),
    [graded],
  );
  const slates = useMemo(() => groupSlates(ordered), [ordered]);
  const fromTakes = ordered.length > 0;
  const wins = fromTakes
    ? ordered.filter((row) => row.settlement === "win").length
    : recordByLeague(rows, league).reduce((sum, row) => sum + row.wins, 0);
  const losses = fromTakes
    ? ordered.filter((row) => row.settlement === "loss").length
    : recordByLeague(rows, league).reduce((sum, row) => sum + row.losses, 0);
  const gradedCount = wins + losses;
  const rate = gradedCount ? `${Math.round((wins / gradedCount) * 100)}%` : null;
  const latest = latestResult(ordered);
  const visibleSlates = slates
    .map((slate) => ({ ...slate, rows: slate.rows.filter((row) => filter === "all" || row.settlement === filter) }))
    .filter((slate) => slate.rows.length > 0);

  return (
    <section className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3 border-b border-white/10 pb-2">
        <div>
          <h2 className="font-display text-4xl uppercase leading-none tracking-wide">How the picks did</h2>
          <p className="mt-2 max-w-xl text-sm text-mute">
            One name per game. Most recent game first.
          </p>
        </div>
        <Link href="/track-record" className="font-display text-xl uppercase tracking-wide text-gold">
          Full record →
        </Link>
      </div>

      {gradedCount > 0 ? (
        <div className="grid gap-3 sm:grid-cols-3">
          <RecordStat label="Scored" value={String(wins)} hint={rate ? `${rate} of graded picks` : undefined} tone="gold" />
          <RecordStat label="No touchdown" value={String(losses)} hint="The pick did not score" />
          <RecordStat
            label="Last result"
            value={latest?.settlement === "win" ? "Scored" : "No score"}
            hint={latest ? nameOf(latest) : undefined}
            tone={latest?.settlement === "win" ? "gold" : undefined}
          />
        </div>
      ) : (
        <p className="plate px-5 py-6 text-sm text-mute">Results show up here after the games finish.</p>
      )}

      {ordered.length > 0 ? (
        <div className="space-y-2">
          <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-mute">Oldest to newest</p>
          <ol className="flex flex-wrap gap-1.5" aria-label="Graded picks, oldest to newest">
            {oldestFirst.map((row) => {
              const hit = row.settlement === "win";
              return (
                <li key={row.shadow_id}>
                  <span
                    title={`${nameOf(row)} ${hit ? "scored" : "did not score"}`}
                    className={`block h-3 w-3 ${hit ? "bg-gold" : "bg-heat/80"}`}
                  />
                </li>
              );
            })}
          </ol>
        </div>
      ) : null}

      {ordered.length > 0 ? (
        <div className="flex flex-wrap gap-2">
          <FilterChip active={filter === "all"} onClick={() => setFilter("all")}>
            All {ordered.length}
          </FilterChip>
          <FilterChip active={filter === "win"} onClick={() => setFilter("win")}>
            Scored {ordered.filter((row) => row.settlement === "win").length}
          </FilterChip>
          <FilterChip active={filter === "loss"} onClick={() => setFilter("loss")}>
            No score {ordered.filter((row) => row.settlement === "loss").length}
          </FilterChip>
        </div>
      ) : null}

      <div className="space-y-4">
        {visibleSlates.map((slate) => (
          <article key={slate.key} className="plate overflow-hidden">
            <header className="flex flex-wrap items-end justify-between gap-2 border-b border-white/10 px-4 py-3">
              <div>
                <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-mute">
                  {league === "all" && slate.league ? `${leagueName(slate.league)} · ` : ""}
                  {slate.week != null ? `Week ${slate.week} · ` : ""}
                  {slate.label}
                </p>
                <h3 className="mt-1 font-display text-3xl uppercase leading-none tracking-wide">
                  <span className="text-gold">{slate.wins} scored</span>
                  <span className="text-mute"> · {slate.losses} did not</span>
                </h3>
              </div>
            </header>
            <ul>
              {slate.rows.map((row) => (
                <TakeRow key={row.shadow_id} row={row} showLeague={league === "all"} />
              ))}
            </ul>
          </article>
        ))}
      </div>
    </section>
  );
}

function TakeRow({ row, showLeague }: { row: AtdTake; showLeague: boolean }) {
  const hit = row.settlement === "win";
  const clubs = matchup(row);
  return (
    <li className="border-b border-white/5 last:border-b-0">
      <Link
        href={`/games/${row.game_id}`}
        className={`grid grid-cols-[auto_1fr_auto] items-center gap-3 px-4 py-3 ${hit ? "bg-gold/[0.06]" : ""}`}
      >
        <span
          className={
            hit
              ? "bg-gold px-2 py-1 font-display text-lg uppercase leading-none tracking-wide text-ink-950"
              : "px-2 py-1 font-mono text-[10px] uppercase tracking-[0.14em] text-heat"
          }
        >
          {hit ? "TD" : "Miss"}
        </span>
        <span className="min-w-0">
          <span className={`block truncate font-display text-2xl uppercase leading-none tracking-wide ${hit ? "text-paper" : "text-paper/70"}`}>
            {nameOf(row)}
          </span>
          <span className="mt-1 block truncate font-mono text-[10px] uppercase tracking-[0.16em] text-mute">
            {showLeague ? `${leagueName(row.league_id)} · ` : ""}
            {row.kickoff_utc ? `${kickoffParts(row.kickoff_utc).time} · ` : ""}
            {clubs}
            {row.final_score ? ` · ${row.final_score}` : ""}
          </span>
        </span>
        <span className={`font-display text-2xl leading-none ${hit ? "text-gold" : "text-mute"}`}>
          {row.p == null ? "" : pct(row.p, 0)}
        </span>
      </Link>
    </li>
  );
}

function FilterChip({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`px-3 py-1.5 font-display text-lg uppercase tracking-wide ${
        active ? "bg-gold text-ink-950" : "border border-white/15 text-mute hover:text-paper"
      }`}
    >
      {children}
    </button>
  );
}

function RecordStat({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: "gold";
}) {
  return (
    <div className="plate px-4 py-4">
      <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-mute">{label}</p>
      <p className={`mt-2 font-display text-4xl uppercase leading-none tracking-wide sm:text-5xl ${tone === "gold" ? "text-gold" : "text-paper"}`}>
        {value}
      </p>
      {hint ? <p className="mt-2 truncate font-mono text-[10px] uppercase tracking-[0.16em] text-mute">{hint}</p> : null}
    </div>
  );
}

type Slate = {
  key: string;
  label: string;
  week: number | null;
  league: string;
  wins: number;
  losses: number;
  rows: AtdTake[];
};

function groupSlates(ordered: AtdTake[]): Slate[] {
  const groups = new Map<string, AtdTake[]>();
  for (const row of ordered) {
    const key = slateKey(row);
    const list = groups.get(key) ?? [];
    list.push(row);
    groups.set(key, list);
  }
  return [...groups.entries()]
    .map(([key, slateRows]) => {
      const weeks = new Set(slateRows.map((row) => row.week).filter((week): week is number => week != null));
      return {
        key,
        label: dateSpan(slateRows),
        week: weeks.size === 1 ? [...weeks][0] : null,
        league: slateRows[0]?.league_id ?? "",
        wins: slateRows.filter((row) => row.settlement === "win").length,
        losses: slateRows.filter((row) => row.settlement === "loss").length,
        rows: slateRows,
      };
    })
    .sort((a, b) => happenedMs(b.rows[0]) - happenedMs(a.rows[0]) || a.key.localeCompare(b.key));
}

function slateKey(row: AtdTake) {
  if (row.week != null) return `${row.league_id}:${seasonOf(row)}:w${row.week}`;
  if (row.kickoff_utc) return `${row.league_id}:${kickoffDate(row.kickoff_utc)}`;
  return `${row.league_id}:undated`;
}

function seasonOf(row: AtdTake) {
  const fromId = row.game_id.match(/:(\d{4})_/);
  if (fromId) return fromId[1];
  if (row.kickoff_utc) return kickoffDate(row.kickoff_utc).slice(0, 4);
  return "na";
}

function dateSpan(rows: AtdTake[]) {
  const chronological = [...rows].sort((a, b) => happenedMs(a) - happenedMs(b));
  const days = chronological.flatMap((row) => (row.kickoff_utc ? [kickoffParts(row.kickoff_utc).day] : []));
  const unique = [...new Set(days)];
  if (unique.length === 0) return "Earlier";
  if (unique.length === 1) return unique[0];
  return `${unique[0]} – ${unique[unique.length - 1]}`;
}

function recordByLeague(rows: AtdScoreRow[], league: string) {
  const pool = league === "all" ? rows : rows.filter((row) => row.league_id === league);
  const chosen = new Map<string, AtdScoreRow>();
  for (const row of pool) {
    const prev = chosen.get(row.league_id);
    if (!prev || betterRecord(row, prev)) chosen.set(row.league_id, row);
  }
  return [...chosen.values()];
}

function betterRecord(next: AtdScoreRow, prev: AtdScoreRow) {
  if (next.primary !== prev.primary) return Boolean(next.primary);
  return next.n > prev.n;
}

function dedupeTakes(takes: AtdTake[], league: string) {
  const best = new Map<string, AtdTake>();
  for (const row of takes) {
    if (league !== "all" && row.league_id !== league) continue;
    if (row.settlement !== "win" && row.settlement !== "loss") continue;
    const key = `${row.game_id}:${(row.player_name || row.shadow_id).toLowerCase()}`;
    const prev = best.get(key);
    if (!prev || familyRank(row) < familyRank(prev)) best.set(key, row);
  }
  return [...best.values()];
}

function familyRank(row: AtdTake) {
  return FAMILY_RANK[row.family || ""] ?? 9;
}

function nameOf(row: AtdTake) {
  return row.player_name || "Scorer";
}

/** Kickoff is when that result happened. A missing time sorts first so it cannot become the latest. */
function happenedMs(row: AtdTake) {
  const kick = kickoffMs(row.kickoff_utc);
  return Number.isFinite(kick) ? kick : 0;
}

function latestResult(rows: AtdTake[]) {
  return rows.reduce<AtdTake | null>((best, row) => {
    if (!best || happenedMs(row) > happenedMs(best)) return row;
    return best;
  }, null);
}

function leagueName(id: string) {
  if (id === "nfl") return "NFL";
  if (id === "fbs") return "College";
  return id;
}

function matchup(row: AtdTake) {
  if (row.away_abbr && row.home_abbr) return `${row.away_abbr} at ${row.home_abbr}`;
  const parts = row.game_id.split("_");
  if (parts.length < 2) return row.game_id;
  return `${parts[parts.length - 2]} at ${parts[parts.length - 1]}`;
}
