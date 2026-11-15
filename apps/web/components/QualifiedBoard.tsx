import Link from "next/link";
import { american, kickoffParts, marketLabel, pct } from "@/lib/format";

export type QualifiedSpread = {
  game_id: string;
  league_id?: string;
  kickoff_utc?: string | null;
  game_status?: string | null;
  home_score?: number | null;
  away_score?: number | null;
  away_team?: string | null;
  home_team?: string | null;
  ats_pick_label?: string | null;
  ats_pick_result?: "correct" | "miss" | "push" | string | null;
  edge_points?: number | null;
};

export type QualifiedPrice = {
  game_id: string;
  player_name?: string | null;
  away_abbr?: string | null;
  home_abbr?: string | null;
  market_id?: string | null;
  book_price?: number | null;
  bookmaker?: string | null;
  kickoff_utc?: string | null;
  result?: string | null;
  actual?: number | null;
  game_status?: string | null;
  pick?: string | null;
  forecast?: { p_estimate?: number | null };
};

export function QualifiedBoard({
  spreads,
  prices,
}: {
  spreads: QualifiedSpread[];
  prices: QualifiedPrice[];
}) {
  const cards = [
    ...spreads.map((row) => tone(row.ats_pick_result)),
    ...prices.map((row) => tone(row.result)),
  ];
  const wins = cards.filter((item) => item === "win").length;
  const losses = cards.filter((item) => item === "loss").length;
  const pending = cards.length - wins - losses - cards.filter((item) => item === "push").length;
  const pushes = cards.filter((item) => item === "push").length;
  const parts = [`${wins} wins`, `${losses} losses`, `${pending} pending`];
  if (pushes) parts.push(`${pushes} pushes`);

  return (
    <div className="space-y-8">
      <p className="text-sm text-mute">Today · {parts.join(" · ")}</p>
      {spreads.length > 0 ? (
        <section className="space-y-3">
          <h2 className="font-display text-3xl uppercase leading-none tracking-wide">Spreads</h2>
          <div className="grid gap-3 md:grid-cols-2">
            {spreads.map((row) => (
              <SpreadCard key={row.game_id} row={row} />
            ))}
          </div>
        </section>
      ) : null}
      {prices.length > 0 ? (
        <section className="space-y-3">
          <h2 className="font-display text-3xl uppercase leading-none tracking-wide">Player props</h2>
          <div className="grid gap-3 md:grid-cols-2">
            {prices.map((row) => (
              <PriceCard key={`${row.game_id}-${row.market_id}-${row.player_name}-${row.pick}`} row={row} />
            ))}
          </div>
        </section>
      ) : null}
    </div>
  );
}

function SpreadCard({ row }: { row: QualifiedSpread }) {
  const mark = tone(row.ats_pick_result);
  const final = row.game_status === "final" && row.home_score != null && row.away_score != null;
  return (
    <Link href={`/games/${row.game_id}`} className={boxClass(mark)}>
      <GradeBadge mark={mark} />
      <p className={`font-mono text-[10px] uppercase tracking-[0.16em] text-mute ${mark ? "pr-9" : ""}`}>
        Spread{row.league_id ? ` · ${row.league_id}` : ""}
        {row.edge_points != null ? ` · ${row.edge_points} pt edge` : ""}
      </p>
      <p className="mt-2 font-display text-3xl uppercase leading-none tracking-wide">{row.ats_pick_label}</p>
      <p className="mt-2 text-sm text-paper/70">
        {row.away_team} at {row.home_team}
        {final ? ` · Final ${row.away_score}–${row.home_score}` : ""}
      </p>
      <p className={resultClass(mark)}>{resultLabel(mark, row.game_status)}</p>
    </Link>
  );
}

function PriceCard({ row }: { row: QualifiedPrice }) {
  const mark = tone(row.result);
  const when = row.kickoff_utc ? kickoffParts(row.kickoff_utc) : null;
  const chance = row.forecast?.p_estimate;
  return (
    <Link href={`/games/${row.game_id}`} className={boxClass(mark)}>
      <GradeBadge mark={mark} />
      <p className={`font-mono text-[10px] uppercase tracking-[0.16em] text-mute ${mark ? "pr-9" : ""}`}>
        {marketLabel(row.market_id || "")}
        {row.away_abbr && row.home_abbr ? ` · ${row.away_abbr} at ${row.home_abbr}` : ""}
        {when ? ` · ${when.time}` : ""}
      </p>
      <p className="mt-2 font-display text-3xl uppercase leading-none tracking-wide">{row.player_name}</p>
      <p className="mt-2 text-sm text-paper/80">{row.pick}</p>
      <p className="mt-3 text-sm text-gold">
        {row.bookmaker || "Posted price"} · {american(row.book_price)}
        {chance == null ? "" : ` · ${pct(chance, 0)}`}
      </p>
      <p className={resultClass(mark)}>
        {resultLabel(mark, row.game_status)}
        {actualText(row)}
      </p>
    </Link>
  );
}

function tone(result?: string | null): "win" | "loss" | "push" | null {
  if (result === "win" || result === "correct" || result === "scored") return "win";
  if (result === "loss" || result === "miss" || result === "blank") return "loss";
  if (result === "push") return "push";
  return null;
}

function actualText(row: QualifiedPrice) {
  if (row.actual == null) return "";
  const value = Number.isInteger(row.actual) ? String(row.actual) : String(row.actual);
  const unit =
    row.market_id === "receptions" ? "receptions" : row.market_id === "rushing_yards" ? "rushing yards" : "receiving yards";
  return ` · ${value} ${unit}`;
}

function resultLabel(mark: "win" | "loss" | "push" | null, status?: string | null) {
  if (mark === "win") return "Correct";
  if (mark === "loss") return "Wrong";
  if (mark === "push") return "Push";
  if (status === "final") return "Final — waiting on the stat";
  if (status === "in_progress") return "In progress";
  return "Result pending";
}

function boxClass(mark: "win" | "loss" | "push" | null) {
  const color = mark === "win" ? "border-green-500 bg-green-950/50" : mark === "loss" ? "border-red-500 bg-red-950/50" : "plate";
  return `relative block rounded-xl border px-4 py-4 ${color}`;
}

function resultClass(mark: "win" | "loss" | "push" | null) {
  const color = mark === "win" ? "text-green-300" : mark === "loss" ? "text-red-300" : "text-mute";
  return `mt-2 text-sm font-semibold ${color}`;
}

function GradeBadge({ mark }: { mark: "win" | "loss" | "push" | null }) {
  if (mark !== "win" && mark !== "loss") return null;
  const won = mark === "win";
  return (
    <span aria-label={won ? "Win" : "Loss"} className={`absolute right-3 top-3 flex h-7 w-7 items-center justify-center rounded-full text-xl font-bold ${won ? "bg-green-500 text-black" : "bg-red-500 text-white"}`}>
      {won ? "✓" : "✕"}
    </span>
  );
}
