import Link from "next/link";
import { TeamMark } from "@/components/TeamMark";
import { american, marketLabel, pct } from "@/lib/format";
import { presentTeam, type TeamMarkInfo } from "@/lib/teams";

export type PropDeskCall = {
  outcome_id?: string | null;
  game_id: string;
  player_name?: string | null;
  position?: string | null;
  team_abbr?: string | null;
  team_mark?: TeamMarkInfo | null;
  away_abbr?: string | null;
  home_abbr?: string | null;
  kickoff_utc?: string | null;
  game_status?: string | null;
  final_score?: string | null;
  market_id?: string | null;
  side?: string | null;
  line?: number | null;
  book_price?: number | null;
  bookmaker?: string | null;
  p?: number | null;
  pick?: string | null;
  result?: string | null;
  actual?: number | null;
  matchup_note?: string | null;
  team_spread?: number | null;
};

export function deskRecord(calls: PropDeskCall[]) {
  const wins = calls.filter((call) => call.result === "win").length;
  const losses = calls.filter((call) => call.result === "loss").length;
  const pushes = calls.filter((call) => call.result === "push").length;
  const voids = calls.filter((call) => call.result === "void").length;
  const pending = calls.length - wins - losses - pushes - voids;
  const graded = wins + losses;
  const rate = graded ? `${Math.round((100 * wins) / graded)}%` : null;
  return { wins, losses, pushes, voids, pending, graded, rate };
}

export function DeskRecord({ calls }: { calls: PropDeskCall[] }) {
  const { wins, losses, pushes, voids, pending, rate } = deskRecord(calls);
  const held = [pushes ? `${pushes} pushes` : "", voids ? `${voids} didn't play` : ""].filter(Boolean).join(" · ");
  return (
    <div className="grid gap-3 sm:grid-cols-3" aria-label="Desk pick record">
      <RecordStat label="Wins" value={String(wins)} tone="text-green-300" hint={rate ? `${rate} of graded` : "None graded yet"} />
      <RecordStat label="Losses" value={String(losses)} tone="text-red-300" hint={held || "Wrong once the stat is in"} />
      <RecordStat label="Record" value={`${wins}–${losses}`} hint={pending ? `${pending} still pending` : "Every call graded"} />
    </div>
  );
}

function RecordStat({ label, value, hint, tone = "text-paper" }: { label: string; value: string; hint?: string; tone?: string }) {
  return (
    <div className="plate px-4 py-4">
      <div className="font-mono text-[10px] uppercase tracking-[0.22em] text-mute">{label}</div>
      <div className={`mt-2 font-display text-5xl leading-none tracking-wide ${tone}`}>{value}</div>
      {hint ? <div className="mt-2 font-mono text-[10px] uppercase tracking-[0.16em] text-mute">{hint}</div> : null}
    </div>
  );
}

export function PropDesk({ calls }: { calls: PropDeskCall[] }) {
  const { wins, losses, pushes, voids, pending } = deskRecord(calls);
  const parts = [`${wins} wins`, `${losses} losses`, `${pending} pending`];
  if (pushes) parts.push(`${pushes} pushes`);
  if (voids) parts.push(`${voids} didn't play`);

  return (
    <div className="space-y-4">
      <p className="max-w-xl text-sm text-mute">
        One prop for every game. The strongest chance under 90%. Touchdowns stay on the Anytime TD page.
      </p>
      <p className="text-sm text-mute">Today · {parts.join(" · ")}</p>
      <div className="grid gap-3 md:grid-cols-2">
        {calls.map((call) => (
          <PropDeskCard key={call.outcome_id || `${call.game_id}-${call.market_id}-${call.player_name}`} call={call} />
        ))}
      </div>
    </div>
  );
}

function PropDeskCard({ call }: { call: PropDeskCall }) {
  const mark = call.result === "win" ? "win" : call.result === "loss" ? "loss" : call.result === "push" ? "push" : null;
  const team = call.team_abbr ? presentTeam({ abbreviation: call.team_abbr, mark: call.team_mark }) : null;
  const matchup = call.away_abbr && call.home_abbr ? `${call.away_abbr} at ${call.home_abbr}` : "";
  const line = call.line == null ? "" : Number.isInteger(call.line) ? String(call.line) : call.line.toFixed(1);
  const side = call.side === "under" ? "Under" : "Over";
  const label = call.pick || `${side} ${line} ${marketLabel(call.market_id || "").toLowerCase()}`;
  const actual = call.result === "void" || call.actual == null ? "" : ` · Actual ${Number.isInteger(call.actual) ? call.actual : call.actual.toFixed(1)}`;
  const price = american(call.book_price);
  return (
    <Link href={`/games/${call.game_id}`} className={boxClass(mark)}>
      <div className="flex gap-4">
        {team ? <TeamMark team={team} abbr={team.abbr} size="md" /> : null}
        <div className="min-w-0 flex-1">
          <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-mute">
            {marketLabel(call.market_id || "")}
            {call.position ? ` · ${call.position}` : ""}
            {call.team_abbr ? ` · ${call.team_abbr}` : ""}
          </p>
          <p className="mt-1 truncate font-display text-3xl uppercase leading-none tracking-wide">{call.player_name}</p>
          <p className="mt-2 text-sm text-paper/80">{label}</p>
          {call.matchup_note ? <p className="mt-1 text-sm text-mute">{call.matchup_note}</p> : null}
          {favoriteLine(call.team_spread) ? <p className="mt-1 text-sm text-mute">{favoriteLine(call.team_spread)}</p> : null}
          <p className="mt-2 text-sm text-mute">
            {matchup}
            {call.final_score ? ` · ${call.final_score}` : ""}
          </p>
          <p className="mt-3 text-sm text-gold">
            {call.bookmaker || "Posted price"}
            {call.p == null ? "" : ` · ${pct(call.p, 0)}`}
          </p>
          <p className={resultClass(mark)}>
            {call.result === "void" ? "Didn't play" : mark === "win" ? "Correct" : mark === "loss" ? "Wrong" : mark === "push" ? "Push" : statusLabel(call.game_status)}
            {actual}
          </p>
        </div>
        <div className="flex shrink-0 flex-col items-end text-right">
          <GradeBadge mark={mark} />
          <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-mute">{side}</p>
          {line ? <p className="mt-1 font-display text-4xl leading-none tracking-wide">{line}</p> : null}
          <p className={`mt-1 font-display text-2xl leading-none ${call.book_price != null && call.book_price > 0 ? "text-green-300" : "text-gold"}`}>{price}</p>
        </div>
      </div>
    </Link>
  );
}

function favoriteLine(spread?: number | null) {
  if (spread == null || spread > -7) return "";
  const points = Math.abs(spread);
  const text = Number.isInteger(points) ? String(points) : points.toFixed(1);
  return `Favored by ${text}`;
}

function statusLabel(status?: string | null) {
  if (status === "final" || status === "post") return "Final — waiting on the stat";
  if (status === "in_progress") return "In progress";
  return "On the card";
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
    <span aria-label={won ? "Win" : "Loss"} className={`mb-2 flex h-7 w-7 items-center justify-center rounded-full text-xl font-bold ${won ? "bg-green-500 text-black" : "bg-red-500 text-white"}`}>
      {won ? "✓" : "✕"}
    </span>
  );
}
