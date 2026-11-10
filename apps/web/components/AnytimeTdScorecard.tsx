import Link from "next/link";
import { LeagueBadge } from "@/components/StatusChips";
import { evPct, pct } from "@/lib/format";

export type AtdScoreRow = {
  league_id: string;
  family: string;
  primary?: boolean;
  n: number;
  wins: number;
  losses: number;
  games: number;
  hit_rate: number | null;
  brier: number | null;
  flat_stake_roi: number | null;
};

export type AtdTake = {
  shadow_id: string;
  league_id: string;
  game_id: string;
  player_name?: string | null;
  family?: string | null;
  settlement?: string | null;
  p?: number | null;
  kickoff_utc?: string | null;
  week?: number | null;
  away_abbr?: string | null;
  home_abbr?: string | null;
  final_score?: string | null;
};

export function AnytimeTdScorecard({
  rows,
  note,
  league = "all",
  title = "Pick score",
  showLedgerLink = true,
  takes = [],
}: {
  rows: AtdScoreRow[];
  note?: string | null;
  league?: string;
  title?: string;
  showLedgerLink?: boolean;
  takes?: AtdTake[];
}) {
  const filtered =
    league === "all" ? rows : rows.filter((row) => row.league_id === league);
  const primary = filtered.filter((row) => row.primary);
  const highlight = primary[0] ?? filtered[0] ?? null;

  return (
    <section className="space-y-3">
      <div className="flex flex-wrap items-end justify-between gap-3 border-b border-white/10 pb-2">
        <h2 className="font-display text-4xl uppercase leading-none tracking-wide">{title}</h2>
        {showLedgerLink ? (
          <Link href="/track-record" className="font-display text-xl uppercase tracking-wide text-gold">
            Full ledger →
          </Link>
        ) : note ? (
          <p className="max-w-xl text-sm text-mute">{note}</p>
        ) : null}
      </div>
      {highlight ? (
        <div className="grid gap-3 sm:grid-cols-4">
          <ScoreStat label="Model" value={highlight.family} hint={highlight.primary ? "Research primary" : "Settled family"} />
          <ScoreStat label="W-L" value={`${highlight.wins}-${highlight.losses}`} hint={`${highlight.games} games`} />
          <ScoreStat label="Hit" value={pct(highlight.hit_rate)} hint={`${highlight.n} player-games`} />
          <ScoreStat
            label="Brier"
            value={highlight.brier == null ? "—" : highlight.brier.toFixed(3)}
            hint={highlight.flat_stake_roi == null ? "Flat ROI —" : `Flat ROI ${evPct(highlight.flat_stake_roi)}`}
          />
        </div>
      ) : null}
      <div className="space-y-2 md:hidden">
        {filtered.length === 0 ? (
          <p className="plate px-4 py-6 text-sm text-mute">
            No settled anytime TD picks yet. Freeze before kickoff; grade after finals.
          </p>
        ) : (
          filtered.map((row) => (
            <div key={`atd-mobile-${row.league_id}-${row.family}`} className={`plate px-4 py-4 ${row.primary ? "border-gold/40" : ""}`}>
              <div className="flex items-center justify-between gap-3">
                <LeagueBadge league={row.league_id} />
                <span className="font-mono text-[10px] uppercase tracking-[0.16em] text-gold">
                  {row.family}
                  {row.primary ? " · primary" : ""}
                </span>
              </div>
              <p className="mt-3 font-display text-3xl uppercase leading-none tracking-wide">
                {row.wins}-{row.losses}
              </p>
              <p className="mt-2 font-mono text-[10px] uppercase tracking-[0.14em] text-mute">
                {row.games} games · Hit {pct(row.hit_rate)} · Brier {row.brier == null ? "—" : row.brier.toFixed(3)} · ROI{" "}
                {row.flat_stake_roi == null ? "—" : evPct(row.flat_stake_roi)}
              </p>
            </div>
          ))
        )}
      </div>
      <div className="plate hidden overflow-x-auto md:block">
        <table className="w-full min-w-[760px] text-left text-sm">
          <thead className="font-mono text-[10px] uppercase tracking-[0.18em] text-mute">
            <tr className="border-b border-white/10 bg-black/30">
              <th className="px-4 py-3 font-medium">League</th>
              <th className="px-4 py-3 font-medium">Model</th>
              <th className="px-4 py-3 font-medium">W-L</th>
              <th className="px-4 py-3 font-medium">Games</th>
              <th className="px-4 py-3 font-medium">Hit</th>
              <th className="px-4 py-3 font-medium">Brier</th>
              <th className="px-4 py-3 font-medium">Flat ROI</th>
            </tr>
          </thead>
          <tbody>
            {filtered.length === 0 ? (
              <tr>
                <td className="px-4 py-6 text-mute" colSpan={7}>
                  No settled anytime TD picks yet. Freeze before kickoff; grade after finals.
                </td>
              </tr>
            ) : (
              filtered.map((row) => (
                <tr
                  key={`atd-score-${row.league_id}-${row.family}`}
                  className={`border-b border-white/5 ${row.primary ? "bg-gold/5" : ""}`}
                >
                  <td className="px-4 py-3">
                    <LeagueBadge league={row.league_id} />
                  </td>
                  <td className="px-4 py-3 font-mono uppercase">
                    {row.family}
                    {row.primary ? (
                      <span className="ml-2 font-mono text-[10px] tracking-wider text-gold"> · primary</span>
                    ) : null}
                  </td>
                  <td className="px-4 py-3 font-mono">
                    {row.wins}-{row.losses}
                  </td>
                  <td className="px-4 py-3 font-mono">{row.games}</td>
                  <td className="px-4 py-3 font-mono">{pct(row.hit_rate)}</td>
                  <td className="px-4 py-3 font-mono">{row.brier == null ? "—" : row.brier.toFixed(4)}</td>
                  <td className="px-4 py-3 font-mono">
                    {row.flat_stake_roi == null ? "—" : evPct(row.flat_stake_roi)}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
      {note && showLedgerLink ? <p className="text-sm text-mute">{note}</p> : null}
      <GradedTakes takes={takes} league={league} />
    </section>
  );
}

function GradedTakes({ takes, league }: { takes: AtdTake[]; league: string }) {
  const graded = takes.filter((row) => {
    if (league !== "all" && row.league_id !== league) return false;
    return row.settlement === "win" || row.settlement === "loss";
  });
  if (graded.length === 0) return null;
  const wins = graded.filter((row) => row.settlement === "win").length;
  const losses = graded.length - wins;
  const rate = `${((wins / graded.length) * 100).toFixed(1).replace(/\.0$/, "")}%`;
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <h3 className="font-display text-3xl uppercase leading-none tracking-wide">Graded scorers</h3>
        <p className="font-display text-2xl leading-none tracking-wide text-gold">
          {wins}–{losses} {rate}
        </p>
      </div>
      <div className="grid gap-3 md:grid-cols-2">
        {graded.map((row) => {
          const right = row.settlement === "win";
          return (
            <div key={row.shadow_id} className="plate px-4 py-4">
              <div className="flex items-start justify-between gap-3">
                <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-mute">{matchup(row.game_id)}</p>
                <span className={`px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.16em] ${right ? "bg-gold text-ink-950" : "bg-heat text-ink-950"}`}>
                  {right ? "Right" : "Wrong"}
                </span>
              </div>
              <p className="mt-2 truncate font-display text-3xl uppercase leading-none tracking-wide">{row.player_name || "Scorer"}</p>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function matchup(gameId: string) {
  const parts = gameId.split("_");
  if (parts.length < 2) return gameId;
  const away = parts[parts.length - 2];
  const home = parts[parts.length - 1];
  return `${away} at ${home}`;
}

function ScoreStat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="plate px-4 py-4">
      <div className="font-mono text-[10px] uppercase tracking-[0.22em] text-mute">{label}</div>
      <div className="mt-2 truncate font-display text-3xl uppercase leading-none tracking-wide text-paper sm:text-4xl">
        {value}
      </div>
      {hint ? <div className="mt-2 truncate font-mono text-[10px] uppercase tracking-[0.16em] text-mute">{hint}</div> : null}
    </div>
  );
}
