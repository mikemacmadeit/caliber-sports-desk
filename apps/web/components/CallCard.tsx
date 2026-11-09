import Link from "next/link";
import { MarginBar } from "@/components/MarginBar";
import { TeamMark } from "@/components/TeamMark";
import type { AtsRow } from "@/components/AtsBoard";
import { callCopy, ledgerLines } from "@/lib/calls";
import { kickoffParts } from "@/lib/format";
import { clubFromName } from "@/lib/teams";

export function CallCard({ row }: { row: AtsRow }) {
  const settled = row.game_status === "final";
  const live = row.game_status === "in_progress";
  const copy = callCopy(row);
  const graded = ledgerLines(row);
  const away = clubFromName(row.away_team);
  const home = clubFromName(row.home_team);
  const when = kickoffParts(row.kickoff_utc);
  const book = bookName(row.sportsbook);

  return (
    <Link href={`/games/${row.game_id}`} className="plate group block overflow-hidden px-4 py-4 sm:px-5">
      <span aria-hidden className="absolute bottom-0 left-0 top-0 w-1.5" style={{ background: away.primary }} />
      <span aria-hidden className="absolute bottom-0 right-0 top-0 w-1.5" style={{ background: home.primary }} />
      <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-mute">
        <span className={live ? "text-signal" : settled ? "text-gold" : "text-gold"}>{row.league_id}</span>
        {settled ? " · Final" : live ? " · Live" : ` · ${when.time}`}
        {settled ? ` · ${when.day}` : ""}
        {book ? ` · ${book}` : ""}
      </p>
      <div className="mt-3 grid grid-cols-[auto_1fr] items-center gap-x-3 gap-y-2">
        <TeamMark team={away} size="sm" />
        <p className="min-w-0 truncate text-sm text-paper">
          {row.away_team}
          {row.away_record ? <span className="text-mute"> {row.away_record}</span> : null}
          {settled && row.away_score != null ? (
            <span className="ml-2 font-display text-2xl leading-none">{row.away_score}</span>
          ) : null}
        </p>
        <TeamMark team={home} size="sm" />
        <p className="min-w-0 truncate text-sm text-paper">
          {row.home_team}
          {row.home_record ? <span className="text-mute"> {row.home_record}</span> : null}
          {settled && row.home_score != null ? (
            <span className="ml-2 font-display text-2xl leading-none">{row.home_score}</span>
          ) : null}
        </p>
      </div>

      {settled ? (
        <div className="mt-4 space-y-1">
          <p className={`text-sm ${row.pick_result === "correct" ? "text-gold" : row.pick_result === "miss" ? "text-heat" : "text-paper/80"}`}>
            {graded.win}
          </p>
          {graded.cover ? (
            <p
              className={`text-sm ${
                row.ats_pick_result === "correct" ? "text-gold" : row.ats_pick_result === "miss" ? "text-heat" : "text-mute"
              }`}
            >
              {graded.cover}
            </p>
          ) : null}
        </div>
      ) : (
        <div className="mt-4 space-y-2">
          <p className="text-sm leading-relaxed text-paper">{copy.winSentence}</p>
          {copy.score ? <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-mute">{copy.score}</p> : null}
          {copy.coverSentence ? (
            <p className={`text-sm leading-relaxed ${copy.loud ? "text-gold" : "text-mute"}`}>{copy.coverSentence}</p>
          ) : null}
          <MarginBar
            margin={row.predicted_margin}
            handicap={row.home_handicap}
            awayColor={away.primary}
            homeColor={home.primary}
            awayName={row.away_team}
            homeName={row.home_team}
          />
        </div>
      )}
    </Link>
  );
}

function bookName(book: string | null | undefined) {
  if (!book) return null;
  const text = book.replaceAll("_", " ");
  if (text.toLowerCase().includes("consensus") || text.toLowerCase().includes("nflverse")) return "Consensus number";
  return text;
}
