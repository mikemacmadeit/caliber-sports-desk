"use client";

import { useMemo, useState } from "react";
import { GameCard } from "@/components/GameCard";
import type { SlateGame } from "@/lib/api";
import { presentTeam } from "@/lib/teams";

export function SlateBoard({
  games,
  versions,
}: {
  games: SlateGame[];
  versions: Record<string, string>;
}) {
  const [query, setQuery] = useState("");
  const [conference, setConference] = useState("all");
  const conferences = useMemo(() => {
    const names = new Set(games.map((game) => game.conference).filter((name): name is string => Boolean(name)));
    return [...names].sort();
  }, [games]);
  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return [...games]
      .filter((game) => conference === "all" || game.conference === conference)
      .filter((game) => !needle || gameText(game).includes(needle))
      .sort((a, b) => +new Date(a.kickoff_utc) - +new Date(b.kickoff_utc));
  }, [games, query, conference]);
  const live = visible.filter((game) => game.status === "in_progress");
  const upcoming = visible.filter((game) => game.status !== "in_progress" && game.status !== "final");
  const featured = live[0] ?? upcoming[0];
  const restLive = live.filter((game) => game.game_id !== featured?.game_id);
  const rest = upcoming.filter((game) => game.game_id !== featured?.game_id);
  const college = rest.filter((game) => game.league_id === "fbs");
  const nfl = rest.filter((game) => game.league_id === "nfl");
  const sections =
    featured?.league_id === "nfl"
      ? [
          { key: "nfl", title: "NFL", games: nfl, note: weekMark(versions.nfl) },
          { key: "fbs", title: "College", games: college, note: weekMark(versions.fbs) },
        ]
      : [
          { key: "fbs", title: "College", games: college, note: weekMark(versions.fbs) },
          { key: "nfl", title: "NFL", games: nfl, note: weekMark(versions.nfl) },
        ];

  return (
    <div className="space-y-8">
      <div className="space-y-3">
        <div className="flex flex-wrap items-center gap-3">
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Find a team"
            className="field-input max-w-xs"
            aria-label="Find a team"
          />
          <span className="font-mono text-[10px] uppercase tracking-[0.16em] text-mute">
            {visible.length} of {games.length}
          </span>
        </div>
        {conferences.length > 1 ? (
          <div className="flex gap-2 overflow-x-auto pb-1">
            <FilterChip active={conference === "all"} onClick={() => setConference("all")}>
              All
            </FilterChip>
            {conferences.map((name) => (
              <FilterChip key={name} active={conference === name} onClick={() => setConference(name)}>
                {name}
              </FilterChip>
            ))}
          </div>
        ) : null}
      </div>

      {featured ? <GameCard game={featured} featured /> : <p className="text-sm text-mute">No games match that filter.</p>}

      {restLive.length > 0 ? (
        <section className="space-y-4">
          <div className="flex items-end justify-between gap-4 border-b border-white/10 pb-3">
            <div>
              <p className="kicker kicker-signal">In progress</p>
              <h2 className="mt-2 font-display text-4xl uppercase leading-none tracking-wide">Live now</h2>
            </div>
            <p className="font-mono text-[11px] uppercase tracking-[0.16em] text-signal">{restLive.length} on the field</p>
          </div>
          <div className="grid gap-4 xl:grid-cols-2">
            {restLive.map((game) => (
              <GameCard key={game.game_id} game={game} />
            ))}
          </div>
        </section>
      ) : null}

      {sections.map(
        (section) =>
          section.games.length > 0 && (
            <section key={section.key} className="space-y-4">
              <div className="flex items-end justify-between gap-4 border-b border-white/10 pb-3">
                <div>
                  <p className="kicker">Still to come</p>
                  <h2 className="mt-2 font-display text-4xl uppercase leading-none tracking-wide">{section.title}</h2>
                </div>
                <p className="font-mono text-[11px] uppercase tracking-[0.16em] text-mute">
                  {section.games.length} games · {section.note}
                </p>
              </div>
              <div className="grid gap-4 xl:grid-cols-2">
                {section.games.map((game) => (
                  <GameCard key={game.game_id} game={game} />
                ))}
              </div>
            </section>
          ),
      )}
    </div>
  );
}

function gameText(game: SlateGame) {
  const away = presentTeam(game.away);
  const home = presentTeam(game.home);
  return [away.abbr, away.city, away.nick, home.abbr, home.city, home.nick, game.venue.name, game.conference]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
}

function weekMark(value?: string) {
  const week = value?.match(/w(\d+)/i)?.[1];
  return week ? `W${week}` : value || "—";
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
      className={`shrink-0 px-3 py-1.5 font-display text-lg uppercase tracking-wide ${
        active ? "bg-gold text-ink-950" : "border border-white/15 text-mute hover:text-paper"
      }`}
    >
      {children}
    </button>
  );
}
