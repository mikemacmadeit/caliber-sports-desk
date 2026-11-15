import Link from "next/link";
import { TeamMark } from "@/components/TeamMark";
import type { ParlayLeg, ParlayTicket } from "@/lib/api";
import { ticketLine } from "@/lib/calls";
import { kickoffParts, marketLabel } from "@/lib/format";
import { club } from "@/lib/teams";

export function ParlayBoard({ tickets }: { tickets: ParlayTicket[] }) {
  return (
    <div className="space-y-4">
      <p className="text-sm text-mute">Today · {tally(tickets)}. Experimental parlay research. Combined odds are estimates and may span sportsbooks; they are not a verified ticket quote or part of the published singles record.</p>
      {tickets.map((ticket, index) => {
        const tone = ticketTone(ticket.kind);
        const ticketMark = markOf(ticket.result);
        return (
          <article key={ticket.id} className={`plate px-5 py-5 ${ticketShell(ticketMark, tone.shell)}`}>
            <div className="flex flex-wrap items-end justify-between gap-3">
              <div className="min-w-0">
                <p className="font-mono text-[10px] uppercase tracking-[0.2em] text-mute">
                  {String(index + 1).padStart(2, "0")} · {ticket.legs.length} legs
                </p>
                <h2 className={`mt-2 font-display uppercase leading-[0.92] tracking-wide ${tone.title}`}>{ticket.title}</h2>
                <p className={resultClass(ticketMark)}>{resultLabel(ticketMark)}</p>
              </div>
              <div className="flex items-center gap-3">
                <GradeBadge mark={ticketMark} />
                <p className={`font-display uppercase leading-none tracking-wide ${tone.chance}`}>
                  {ticketLine(ticket.joint_probability, ticket.price_american)}
                </p>
              </div>
            </div>

            <ul className="mt-5 divide-y divide-white/10 border-y border-white/10">
              {ticket.legs.map((leg, legIndex) => {
                const mark = markOf(leg.result);
                return (
                  <li key={`${ticket.id}-${leg.outcome_id}-${legIndex}`}>
                    <Link href={`/games/${leg.game_id}`} className={`grid grid-cols-[1fr_auto] items-center gap-3 py-3 ${rowTint(mark)}`}>
                      <span className="flex min-w-0 items-center gap-3">
                        <TeamMark team={club(leg.team || leg.player_name)} size="sm" />
                        <span className="min-w-0">
                        <span className="block truncate font-display text-2xl uppercase leading-none tracking-wide text-paper">
                          {leg.player_name}
                        </span>
                        <span className="mt-1 block truncate font-mono text-[10px] uppercase tracking-[0.16em] text-mute">
                          {leg.team} · {legPick(leg)} · {leg.away_abbr} @ {leg.home_abbr} · {kickoffParts(leg.kickoff_utc).time}
                          {finalScore(leg)}
                        </span>
                        <span className={resultClass(mark)}>
                          {resultLabel(mark, leg.game_status)}
                          {actualText(leg)}
                        </span>
                        </span>
                      </span>
                      <span className="flex items-center gap-3">
                        <GradeBadge mark={mark} />
                        <span className="text-right font-mono text-[11px] uppercase tracking-wider text-mute">
                          {leg.book_price == null ? "—" : leg.book_price > 0 ? `+${leg.book_price}` : String(leg.book_price)}
                        </span>
                      </span>
                    </Link>
                  </li>
                );
              })}
            </ul>

            {ticket.summary ? <p className="mt-4 max-w-2xl text-sm leading-relaxed text-paper/60">{ticket.summary}</p> : null}
          </article>
        );
      })}
    </div>
  );
}

function ticketTone(kind: string) {
  if (kind === "lottery" || kind === "anytime_long") {
    return {
      title: "text-3xl text-flare",
      chance: "text-2xl text-flare",
      shell: "border-flare/40 bg-flare/[0.05]",
    };
  }
  if (kind === "long_shot") {
    return {
      title: "text-3xl text-flare",
      chance: "text-2xl text-flare",
      shell: "border-flare/25",
    };
  }
  if (kind === "best_chance" || kind === "spread") {
    return {
      title: "text-3xl text-paper sm:text-5xl",
      chance: "text-2xl text-gold sm:text-3xl",
      shell: "border-gold/40",
    };
  }
  return {
    title: "text-3xl text-paper sm:text-4xl",
    chance: "text-2xl text-paper",
    shell: "",
  };
}

function legPick(leg: ParlayLeg) {
  if (leg.market_id === "anytime_td") return "Anytime TD";
  if (leg.market_id === "point_spread") {
    if (leg.line == null) return "Spread";
    const line = leg.line > 0 ? `+${leg.line}` : String(leg.line);
    return `Spread ${line}`;
  }
  const side = leg.side === "under" ? "Under" : "Over";
  const line = leg.line == null ? "" : ` ${leg.line}`;
  return `${marketLabel(leg.market_id)} ${side}${line}`;
}

type Mark = "win" | "loss" | "push" | null;

function tally(tickets: ParlayTicket[]) {
  const marks = tickets.map((ticket) => markOf(ticket.result));
  const wins = marks.filter((mark) => mark === "win").length;
  const losses = marks.filter((mark) => mark === "loss").length;
  const pushes = marks.filter((mark) => mark === "push").length;
  const pending = marks.length - wins - losses - pushes;
  const parts = [`${wins} wins`, `${losses} losses`, `${pending} pending`];
  if (pushes) parts.push(`${pushes} pushes`);
  return parts.join(" · ");
}

function markOf(result?: string | null): Mark {
  if (result === "win" || result === "correct" || result === "scored") return "win";
  if (result === "loss" || result === "miss" || result === "blank") return "loss";
  if (result === "push") return "push";
  return null;
}

function resultLabel(mark: Mark, status?: string | null) {
  if (mark === "win") return "Correct";
  if (mark === "loss") return "Wrong";
  if (mark === "push") return "Push";
  if (status === "final" || status === "post") return "Final — waiting on the stat";
  if (status === "in_progress") return "In progress";
  return "Result pending";
}

function resultClass(mark: Mark) {
  const color = mark === "win" ? "text-green-300" : mark === "loss" ? "text-red-300" : "text-mute";
  return `mt-1 text-sm font-semibold ${color}`;
}

function ticketShell(mark: Mark, fallback: string) {
  if (mark === "win") return "border-green-500 bg-green-950/50";
  if (mark === "loss") return "border-red-500 bg-red-950/50";
  return fallback;
}

function rowTint(mark: Mark) {
  if (mark === "win") return "bg-green-950/30";
  if (mark === "loss") return "bg-red-950/30";
  return "";
}

function finalScore(leg: ParlayLeg) {
  if (leg.game_status !== "final" && leg.game_status !== "post") return "";
  if (leg.away_score == null || leg.home_score == null) return "";
  return ` · Final ${leg.away_score}–${leg.home_score}`;
}

function actualText(leg: ParlayLeg) {
  if (leg.actual == null || leg.market_id === "point_spread" || leg.market_id === "anytime_td") return "";
  const value = Number.isInteger(leg.actual) ? String(leg.actual) : String(leg.actual);
  const unit =
    leg.market_id === "receptions" ? "receptions" : leg.market_id === "rushing_yards" ? "rushing yards" : "receiving yards";
  return ` · ${value} ${unit}`;
}

function GradeBadge({ mark }: { mark: Mark }) {
  if (mark !== "win" && mark !== "loss") return null;
  const won = mark === "win";
  return (
    <span aria-label={won ? "Win" : "Loss"} className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-xl font-bold ${won ? "bg-green-500 text-black" : "bg-red-500 text-white"}`}>
      {won ? "✓" : "✕"}
    </span>
  );
}
