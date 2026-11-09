"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { PriceFreshnessBadge, PropTable } from "@/components/PropTable";
import { TeamMark } from "@/components/TeamMark";
import type { PropRow } from "@/lib/api";
import { deskPickReason, selectDeskTakes, valueAtPrice } from "@/lib/atdDeskTakes";
import { kickoffMs, kickoffParts } from "@/lib/format";
import { presentTeam, type TeamMarkInfo } from "@/lib/teams";

export function PropExplorer({ rows, mode }: { rows: PropRow[]; mode: "calls" | "flat" }) {
  const [query, setQuery] = useState("");
  const [gameId, setGameId] = useState("all");
  const [team, setTeam] = useState("all");
  const [position, setPosition] = useState("all");
  const [sort, setSort] = useState<SortId>("prob");
  const [dir, setDir] = useState<Dir>("desc");
  const [showNoBet, setShowNoBet] = useState(false);

  const games = useMemo(() => gameChips(rows), [rows]);
  const teams = useMemo(() => teamChips(rows, gameId), [rows, gameId]);
  const positions = useMemo(() => {
    const names = new Set(rows.map((row) => row.position).filter((name): name is string => Boolean(name)));
    return [...names].sort();
  }, [rows]);

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return rows.filter((row) => {
      if (gameId !== "all" && row.game_id !== gameId) return false;
      if (team !== "all" && row.team !== team) return false;
      if (position !== "all" && row.position !== position) return false;
      if (!needle) return true;
      return `${row.player_name} ${row.team} ${row.opponent} ${row.position || ""}`.toLowerCase().includes(needle);
    }).sort((a, b) => compareRows(a, b, sort, dir));
  }, [rows, query, gameId, team, position, sort, dir]);

  const plays = selectDeskTakes(visible);
  const priced = valueAtPrice(visible);
  const picked = new Set(plays.map((row) => row.outcome_id));
  const abstain = visible.filter((row) => !picked.has(row.outcome_id));
  const pickedGames = new Set(plays.map((row) => row.game_id));
  const noPickGames = games.filter((game) => !pickedGames.has(game.id));

  const toolbar = (
    <div className="space-y-3">
      <div className="flex items-center gap-2 overflow-x-auto pb-1">
        <div className="w-56 shrink-0">
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Find a player"
            className="field-input"
            aria-label="Find a player"
          />
        </div>
        <span className="shrink-0 font-mono text-[10px] uppercase tracking-[0.16em] text-mute">
          {visible.length} of {rows.length}
        </span>
        {SORTS.map((item) => (
          <TextChip
            key={item.id}
            active={sort === item.id}
            onClick={() => {
              if (sort === item.id) {
                setDir((current) => (current === "asc" ? "desc" : "asc"));
                return;
              }
              setSort(item.id);
              setDir(item.id === "name" || item.id === "team" ? "asc" : "desc");
            }}
          >
            {item.label}
            {sort === item.id ? (dir === "asc" ? " ↑" : " ↓") : ""}
          </TextChip>
        ))}
      </div>
      {visible.length > 0 && visible.every((row) => row.book_price == null) ? (
        <p className="max-w-xl text-sm text-mute">
          Price, EV, and the haircut need a posted book number. These rows have a research probability and no locked price, so those columns stay empty and the call is no bet.
        </p>
      ) : null}
    </div>
  );

  return (
    <div className="space-y-6">
      <div className="space-y-3">
        {games.length > 1 ? (
          <ChipRow>
            <TextChip active={gameId === "all"} onClick={() => { setGameId("all"); setTeam("all"); }}>
              All games
            </TextChip>
            {games.map((game) => (
              <button
                key={game.id}
                type="button"
                onClick={() => {
                  setGameId(game.id === gameId ? "all" : game.id);
                  setTeam("all");
                }}
                className={`flex shrink-0 items-center gap-2 border px-2 py-1.5 ${
                  gameId === game.id ? "border-gold bg-gold text-ink-950" : "border-white/10 bg-ink-950 text-paper"
                }`}
              >
                <TeamMark team={markClub(game.away, game.awayMark)} size="sm" />
                <span className="font-display text-lg uppercase leading-none">
                  {game.away}
                  <span className={`mx-1 font-mono text-[10px] ${gameId === game.id ? "text-ink-950" : "text-gold"}`}>at</span>
                  {game.home}
                </span>
                <TeamMark team={markClub(game.home, game.homeMark)} size="sm" />
              </button>
            ))}
          </ChipRow>
        ) : null}
        {teams.length > 1 ? (
          <ChipRow>
            <TextChip active={team === "all"} onClick={() => setTeam("all")}>
              All teams
            </TextChip>
            {teams.map((club) => (
              <button
                key={club.abbr}
                type="button"
                onClick={() => setTeam(club.abbr === team ? "all" : club.abbr)}
                className={`flex shrink-0 items-center gap-2 border px-2 py-1 ${
                  team === club.abbr ? "border-gold bg-gold text-ink-950" : "border-white/10 text-paper"
                }`}
              >
                <TeamMark team={club} size="sm" />
                <span className="font-display text-lg uppercase leading-none">{club.abbr}</span>
              </button>
            ))}
          </ChipRow>
        ) : null}
        {positions.length > 1 ? (
          <ChipRow>
            <TextChip active={position === "all"} onClick={() => setPosition("all")}>
              All spots
            </TextChip>
            {positions.map((name) => (
              <TextChip key={name} active={position === name} onClick={() => setPosition(name === position ? "all" : name)}>
                {name}
              </TextChip>
            ))}
          </ChipRow>
        ) : null}
      </div>

      {visible.length === 0 ? (
        <div className="space-y-3">
          {toolbar}
          <p className="text-sm text-mute">Nothing matches that filter.</p>
        </div>
      ) : null}

      {mode === "flat" && visible.length > 0 ? (
        <div className="space-y-3">
          {toolbar}
          <PropTable rows={visible} />
        </div>
      ) : null}

      {mode === "calls" && visible.length > 0 ? (
        <>
          {toolbar}
          <div className="grid gap-3 sm:grid-cols-3">
            <CallStat label="Desk picks" value={String(plays.length).padStart(2, "0")} hint="One player per game" />
            <CallStat label="No pick" value={String(noPickGames.length).padStart(2, "0")} hint="Games with no scorer call" />
            <CallStat label="Research rows" value={String(abstain.length).padStart(2, "0")} hint="Not a take — stay off" />
          </div>
          <Section title="Desk picks" count={plays.length} accent="flare" />
          <p className="max-w-xl text-sm text-mute">
            One player per game: whoever is most likely to score tonight. The price is a separate question. Research only — Official Play stays off.
          </p>
          {plays.length > 0 ? (
            <div className="grid gap-3 md:grid-cols-2">
              {plays.map((row) => (
                <ScorerCard key={row.outcome_id} row={row} />
              ))}
            </div>
          ) : (
            <p className="plate px-5 py-6 text-sm text-mute">Nobody on this board has a real chance to score tonight.</p>
          )}
          {noPickGames.length > 0 ? (
            <div className="space-y-2">
              <Section title="No pick this game" count={noPickGames.length} />
              <p className="max-w-xl text-sm text-mute">
                These games have players on the board, but nobody has a real chance to score tonight. That is a no-bet, not a miss.
              </p>
              <div className="grid gap-2 md:grid-cols-2">
                {noPickGames.map((game) => (
                  <div key={`nopick-${game.id}`} className="plate px-4 py-3">
                    <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-mute">No pick</p>
                    <p className="mt-1 font-display text-2xl uppercase leading-none tracking-wide">
                      {game.away} at {game.home}
                    </p>
                  </div>
                ))}
              </div>
            </div>
          ) : null}
          <Section title="At this price" count={priced.length} accent="gold" />
          <p className="max-w-xl text-sm text-mute">
            Fresh book and edge after haircut. Useful when it differs from the desk pick.
          </p>
          {priced.length > 0 ? (
            <div className="grid gap-3 md:grid-cols-2">
              {priced.map((row) => (
                <ScorerCard key={`price-${row.outcome_id}`} row={row} />
              ))}
            </div>
          ) : (
            <p className="plate px-5 py-6 text-sm text-mute">No fresh price beats the chance.</p>
          )}
          <div className="flex flex-wrap items-end justify-between gap-3 border-b border-white/10 pb-2">
            <h2 className="font-display text-4xl uppercase leading-none tracking-wide">No bet</h2>
            <button
              type="button"
              onClick={() => setShowNoBet((v) => !v)}
              className="font-display text-lg uppercase tracking-wide text-gold"
            >
              {showNoBet ? "Hide rows" : `Show ${abstain.length} rows`}
            </button>
          </div>
          {showNoBet ? <PropTable rows={abstain} /> : (
            <p className="text-sm text-mute">Hidden by default so the page stays on the calls.</p>
          )}
        </>
      ) : null}
    </div>
  );
}

type SortId = "prob" | "name" | "ev" | "price" | "team" | "kick";
type Dir = "asc" | "desc";

const SORTS: { id: SortId; label: string }[] = [
  { id: "prob", label: "Probability" },
  { id: "name", label: "Name" },
  { id: "ev", label: "Profit" },
  { id: "price", label: "Price" },
  { id: "team", label: "Team" },
  { id: "kick", label: "Kickoff" },
];

function compareRows(a: PropRow, b: PropRow, sort: SortId, dir: Dir) {
  const sign = dir === "asc" ? 1 : -1;
  const byName = a.player_name.localeCompare(b.player_name);
  if (sort === "name") return byName * sign;
  if (sort === "team") return a.team.localeCompare(b.team) * sign || byName;
  if (sort === "kick") return a.kickoff_utc.localeCompare(b.kickoff_utc) * sign || byName;
  if (sort === "prob") return ((a.forecast.p_estimate ?? 0) - (b.forecast.p_estimate ?? 0)) * sign || byName;
  if (sort === "price") return missingLast(a.book_price, b.book_price, sign) || byName;
  const aEv = a.forecast.ev_after_haircut ?? a.forecast.ev;
  const bEv = b.forecast.ev_after_haircut ?? b.forecast.ev;
  return missingLast(aEv, bEv, sign) || byName;
}

function missingLast(a: number | null | undefined, b: number | null | undefined, sign: number) {
  if (a == null && b == null) return 0;
  if (a == null) return 1;
  if (b == null) return -1;
  return (a - b) * sign;
}

function gameChips(rows: PropRow[]) {
  const map = new Map<
    string,
    { id: string; away: string; home: string; kickoff: string; awayMark: TeamMarkInfo | null; homeMark: TeamMarkInfo | null }
  >();
  for (const row of rows) {
    if (map.has(row.game_id) || !row.away_abbr || !row.home_abbr) continue;
    map.set(row.game_id, {
      id: row.game_id,
      away: row.away_abbr,
      home: row.home_abbr,
      kickoff: row.kickoff_utc,
      awayMark: row.away_mark ?? null,
      homeMark: row.home_mark ?? null,
    });
  }
  return [...map.values()].sort(
    (a, b) => kickoffMs(a.kickoff) - kickoffMs(b.kickoff) || a.away.localeCompare(b.away),
  );
}

function teamChips(rows: PropRow[], gameId: string) {
  const map = new Map<string, ReturnType<typeof presentTeam>>();
  for (const row of rows) {
    if (gameId !== "all" && row.game_id !== gameId) continue;
    if (!row.team || map.has(row.team)) continue;
    map.set(row.team, presentTeam({ abbreviation: row.team, mark: row.team_mark }));
  }
  return [...map.values()].sort((a, b) => a.abbr.localeCompare(b.abbr));
}

function markClub(abbr: string, mark?: TeamMarkInfo | null) {
  return presentTeam({ abbreviation: abbr, mark });
}

function ChipRow({ children }: { children: React.ReactNode }) {
  return <div className="flex gap-2 overflow-x-auto pb-1">{children}</div>;
}

function TextChip({
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
      className={`shrink-0 px-3 py-1.5 font-display text-lg uppercase tracking-wide ${
        active ? "bg-gold text-ink-950" : "border border-white/15 text-mute hover:text-paper"
      }`}
    >
      {children}
    </button>
  );
}

function ScorerCard({ row }: { row: PropRow }) {
  const chance = row.forecast.p_estimate;
  const when = kickoffParts(row.kickoff_utc);
  const price = row.book_price == null ? null : row.book_price > 0 ? `+${row.book_price}` : String(row.book_price);
  return (
    <Link href={`/games/${row.game_id}`} className="plate group block px-4 py-4">
      <div className="flex items-start justify-between gap-3">
        <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-flare">
          {row.league_id} · {when.day} · {when.time}
        </p>
        <p className="font-display text-3xl leading-none text-flare">
          {chance == null ? "—" : `${Math.round(chance * 100)}%`}
        </p>
      </div>
      <p className="mt-3 truncate font-display text-3xl uppercase leading-none tracking-wide text-paper">{row.player_name}</p>
      <p className="mt-2 truncate text-sm text-paper/70">
        {row.team} vs {row.opponent}
        {row.position ? ` · ${row.position}` : ""}
      </p>
      <p className="mt-3 text-sm leading-relaxed text-paper/80">{deskPickReason(row)}</p>
      <p className="mt-3 font-mono text-[10px] uppercase tracking-[0.16em] text-mute">
        {price ? `${price} · ` : "Needs price · "}Anytime TD
      </p>
      <PriceFreshnessBadge freshness={row.price_freshness || row.forecast.price_freshness} />
    </Link>
  );
}

function Section({ title, count, accent }: { title: string; count: number; accent?: "gold" | "flare" }) {
  const tone = accent === "flare" ? "text-flare" : accent === "gold" ? "text-gold" : "text-mute";
  return (
    <div className="flex items-end justify-between border-b border-white/10 pb-2">
      <h2 className={`font-display text-4xl uppercase leading-none tracking-wide ${accent === "flare" ? "text-flare" : ""}`}>
        {title}
      </h2>
      <span className={`font-mono text-sm ${tone}`}>{String(count).padStart(2, "0")}</span>
    </div>
  );
}

function CallStat({ label, value, hint }: { label: string; value: string; hint: string }) {
  return (
    <div className="plate px-4 py-4">
      <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-mute">{label}</p>
      <p className="mt-2 font-display text-4xl uppercase leading-none tracking-wide text-paper">{value}</p>
      <p className="mt-2 font-mono text-[10px] uppercase tracking-[0.16em] text-mute">{hint}</p>
    </div>
  );
}
