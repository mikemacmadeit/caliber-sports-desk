import Link from "next/link";
import { MarginBar } from "@/components/MarginBar";
import { TeamMark } from "@/components/TeamMark";
import type { AtsRow } from "@/components/AtsBoard";
import { pickTickets, type CallFacts, type PickTicket, type PickTickets } from "@/lib/calls";
import { kickoffParts } from "@/lib/format";
import { clubFromName } from "@/lib/teams";

export function CallCard({ row }: { row: AtsRow }) {
  const settled = row.game_status === "final";
  const live = row.game_status === "in_progress";
  const tickets = pickTickets(row);
  const away = clubFromName(row.away_team);
  const home = clubFromName(row.home_team);
  const when = kickoffParts(row.kickoff_utc);
  const book = bookName(row.sportsbook);

  return (
    <Link href={`/games/${row.game_id}`} className="plate group block overflow-hidden px-4 py-4 sm:px-5">
      <span aria-hidden className="absolute bottom-0 left-0 top-0 w-1.5" style={{ background: away.primary }} />
      <span aria-hidden className="absolute bottom-0 right-0 top-0 w-1.5" style={{ background: home.primary }} />
      <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-mute">
        <span className={live ? "text-signal" : "text-gold"}>{row.league_id}</span>
        {settled ? " · Final" : live ? " · Live" : ` · ${when.time}`}
        {settled ? ` · ${when.day}` : ""}
        {book ? ` · ${book}` : ""}
      </p>
      <div className="mt-3 grid grid-cols-[auto_1fr] items-center gap-x-3 gap-y-2">
        <TeamMark team={away} size="sm" />
        <TeamLine
          name={row.away_team}
          record={row.away_record}
          score={settled ? row.away_score : null}
          marks={sideMarks(tickets, "away")}
        />
        <TeamMark team={home} size="sm" />
        <TeamLine
          name={row.home_team}
          record={row.home_record}
          score={settled ? row.home_score : null}
          marks={sideMarks(tickets, "home")}
        />
      </div>
      <PickPairView tickets={tickets} />
      {settled ? null : (
        <MarginBar
          margin={row.predicted_margin}
          handicap={row.home_handicap}
          awayColor={away.primary}
          homeColor={home.primary}
          awayName={row.away_team}
          homeName={row.home_team}
        />
      )}
    </Link>
  );
}

export function PickPair({ row }: { row: CallFacts }) {
  return <PickPairView tickets={pickTickets(row)} />;
}

function PickPairView({ tickets }: { tickets: PickTickets }) {
  return (
    <div className="mt-4 space-y-2">
      <div className="grid grid-cols-2 gap-2">
        <PickBox ticket={tickets.moneyline} accent="bg-gold" />
        <PickBox ticket={tickets.spread} accent="bg-signal" />
      </div>
      {tickets.split ? (
        <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-gold">
          Moneyline and spread are different teams
        </p>
      ) : null}
      {tickets.score ? (
        <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-mute">{tickets.score}</p>
      ) : null}
    </div>
  );
}

function PickBox({ ticket, accent }: { ticket: PickTicket; accent: string }) {
  return (
    <div className="relative min-w-0 bg-black/30 px-3 py-2.5">
      <span aria-hidden className={`absolute bottom-0 left-0 top-0 w-1 ${accent}`} />
      <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-mute">{ticket.kicker}</p>
      <p
        className={`mt-1.5 font-display text-2xl uppercase leading-[0.9] tracking-wide sm:text-[1.7rem] ${
          ticket.empty ? "text-mute" : "text-paper"
        }`}
      >
        {ticket.title}
      </p>
      <p className={`mt-2 font-mono text-[10px] uppercase tracking-[0.16em] ${detailTone(ticket)}`}>{ticket.detail}</p>
    </div>
  );
}

function TeamLine({
  name,
  record,
  score,
  marks,
}: {
  name: string;
  record?: string | null;
  score?: number | null;
  marks: { key: string; label: string; tone: "gold" | "signal" }[];
}) {
  return (
    <p className="flex min-w-0 items-center gap-1.5 text-sm text-paper">
      <span className="min-w-0 truncate">
        {name}
        {record ? <span className="text-mute"> {record}</span> : null}
      </span>
      {marks.map((mark) => (
        <span
          key={mark.key}
          className={`shrink-0 px-1.5 py-0.5 font-mono text-[10px] uppercase leading-none tracking-[0.12em] ${
            mark.tone === "gold" ? "bg-gold text-ink-950" : "bg-signal text-ink-950"
          }`}
        >
          {mark.label}
        </span>
      ))}
      {score != null ? <span className="ml-auto shrink-0 font-display text-2xl leading-none">{score}</span> : null}
    </p>
  );
}

function sideMarks(tickets: PickTickets, side: "home" | "away") {
  const marks: { key: string; label: string; tone: "gold" | "signal" }[] = [];
  if (tickets.moneyline.side === side && tickets.moneyline.mark) {
    marks.push({ key: "ml", label: tickets.moneyline.mark, tone: "gold" });
  }
  if (tickets.spread.side === side && tickets.spread.mark) {
    marks.push({ key: "spread", label: tickets.spread.mark, tone: "signal" });
  }
  return marks;
}

function detailTone(ticket: PickTicket) {
  if (ticket.result === "correct") return "text-gold";
  if (ticket.result === "miss") return "text-heat";
  if (ticket.result === "push") return "text-paper";
  return "text-mute";
}

function bookName(book: string | null | undefined) {
  if (!book) return null;
  const text = book.replaceAll("_", " ");
  if (text.toLowerCase().includes("consensus") || text.toLowerCase().includes("nflverse")) return "Consensus number";
  return text;
}
