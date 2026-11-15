import Link from "next/link";
import { TeamMark } from "@/components/TeamMark";
import { dayParts } from "@/lib/days";
import { presentTeam, type TeamMarkInfo } from "@/lib/teams";

export type DeskCall = {
  shadow_id: string;
  league_id: string;
  game_id: string;
  player_name?: string | null;
  position?: string | null;
  p?: number | null;
  settlement?: string | null;
  kickoff_utc?: string | null;
  week?: number | null;
  away_abbr?: string | null;
  home_abbr?: string | null;
  final_score?: string | null;
  team_abbr?: string | null;
  team_mark?: TeamMarkInfo | null;
  opponent_abbr?: string | null;
  family?: string | null;
};

const RESULT: Record<string, { label: string; tone: string }> = {
  win: { label: "Scored", tone: "text-gold" },
  loss: { label: "No touchdown", tone: "text-heat" },
  blocked: { label: "Waiting on the box", tone: "text-mute" },
  pending: { label: "On the card", tone: "text-paper" },
};

export function DeskPicks({
  calls,
  date,
  today,
  league,
  next,
}: {
  calls: DeskCall[];
  date: string;
  today: string;
  league: string;
  next: string | null;
}) {
  if (calls.length === 0) {
    const nextParams = new URLSearchParams();
    if (league && league !== "all") nextParams.set("league", league);
    if (next && next !== today) nextParams.set("date", next);
    const nextHref = nextParams.size ? `/anytime-td?${nextParams}` : "/anytime-td";
    const when = date === today ? "today" : dayParts(date).monthDay;
    return (
      <div className="plate px-6 py-8">
        <p className="font-display text-4xl uppercase leading-none">No scorer locked {when}</p>
        <p className="mt-3 max-w-xl text-sm text-mute">
          The desk has not named the touchdown it will grade for this day. A blank board is not a pick.
        </p>
        {next ? (
          <Link href={nextHref} className="mt-4 inline-block font-display text-2xl uppercase tracking-wide text-gold">
            Open {dayParts(next).weekday} {dayParts(next).monthDay} →
          </Link>
        ) : null}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <p className="max-w-xl text-sm text-mute">
        One name per game. That name is the win-loss column: a touchdown is a win, and no touchdown is a loss once the box score is in.
      </p>
      <div className="grid gap-3 md:grid-cols-2">
        {calls.map((call) => (
          <DeskCard key={call.shadow_id} call={call} />
        ))}
      </div>
    </div>
  );
}

function DeskCard({ call }: { call: DeskCall }) {
  const result = RESULT[call.settlement || "pending"] || RESULT.pending;
  const chance = call.p == null ? "—" : `${Math.round(call.p * 100)}%`;
  const team = call.team_abbr
    ? presentTeam({ abbreviation: call.team_abbr, mark: call.team_mark })
    : null;
  const matchup =
    call.away_abbr && call.home_abbr ? `${call.away_abbr} at ${call.home_abbr}` : call.game_id;
  return (
    <article className="plate flex gap-4 px-4 py-4">
      {team ? <TeamMark team={team} abbr={team.abbr} size="md" /> : null}
      <div className="min-w-0 flex-1">
        <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-mute">
          {result.label}
          {call.position ? ` · ${call.position}` : ""}
          {call.team_abbr ? ` · ${call.team_abbr}` : ""}
        </p>
        <p className="mt-1 truncate font-display text-3xl uppercase leading-none tracking-wide">{call.player_name}</p>
        <p className="mt-2 text-sm text-mute">
          {matchup}
          {call.final_score ? ` · ${call.final_score}` : ""}
        </p>
      </div>
      <p className={`font-display text-4xl leading-none ${result.tone}`}>{chance}</p>
    </article>
  );
}
