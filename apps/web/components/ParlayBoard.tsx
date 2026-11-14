import Link from "next/link";
import type { ParlayLeg, ParlayTicket } from "@/lib/api";
import { ticketLine } from "@/lib/calls";
import { kickoffParts, marketLabel } from "@/lib/format";

export function ParlayBoard({ tickets }: { tickets: ParlayTicket[] }) {
  return (
    <div className="space-y-4">
      <p className="text-sm text-mute">Experimental parlay research. Combined odds are estimates and may span sportsbooks; they are not a verified ticket quote or part of the published singles record.</p>
      {tickets.map((ticket, index) => {
        const tone = ticketTone(ticket.kind);
        return (
          <article key={ticket.id} className={`plate px-5 py-5 ${tone.shell}`}>
            <div className="flex flex-wrap items-end justify-between gap-3">
              <div className="min-w-0">
                <p className="font-mono text-[10px] uppercase tracking-[0.2em] text-mute">
                  {String(index + 1).padStart(2, "0")} · {ticket.legs.length} legs
                </p>
                <h2 className={`mt-2 font-display uppercase leading-[0.92] tracking-wide ${tone.title}`}>{ticket.title}</h2>
              </div>
              <div>
                <p className={`font-display uppercase leading-none tracking-wide ${tone.chance}`}>
                  {ticketLine(ticket.joint_probability, ticket.price_american)}
                </p>
              </div>
            </div>

            <ul className="mt-5 divide-y divide-white/10 border-y border-white/10">
              {ticket.legs.map((leg, legIndex) => (
                <li key={`${ticket.id}-${leg.outcome_id}-${legIndex}`}>
                  <Link href={`/games/${leg.game_id}`} className="grid grid-cols-[1fr_auto] items-center gap-3 py-3">
                    <span className="min-w-0">
                      <span className="block truncate font-display text-2xl uppercase leading-none tracking-wide text-paper">
                        {leg.player_name}
                      </span>
                      <span className="mt-1 block truncate font-mono text-[10px] uppercase tracking-[0.16em] text-mute">
                        {leg.team} · {legPick(leg)} · {leg.away_abbr} @ {leg.home_abbr} · {kickoffParts(leg.kickoff_utc).time}
                      </span>
                    </span>
                    <span className="text-right font-mono text-[11px] uppercase tracking-wider text-mute">
                      {leg.book_price == null ? "—" : leg.book_price > 0 ? `+${leg.book_price}` : String(leg.book_price)}
                    </span>
                  </Link>
                </li>
              ))}
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
