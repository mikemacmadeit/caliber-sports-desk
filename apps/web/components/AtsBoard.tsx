import Link from "next/link";
import { CallCard } from "@/components/CallCard";
import { withQuery } from "@/lib/days";
import type { TeamMarkInfo } from "@/lib/teams";

export type AtsRow = {
  locked?: boolean;
  game_id: string;
  league_id: string;
  kickoff_utc: string;
  home_team: string;
  away_team: string;
  home_abbr?: string | null;
  away_abbr?: string | null;
  home_mark?: TeamMarkInfo | null;
  away_mark?: TeamMarkInfo | null;
  home_record?: string | null;
  away_record?: string | null;
  sportsbook?: string | null;
  home_handicap?: number | null;
  predicted_margin: number;
  predicted_home_score: number;
  predicted_away_score: number;
  p_cover?: number | null;
  pick_label?: string | null;
  pick_team?: string | null;
  pick_line?: number | null;
  pick_market?: string | null;
  pick_probability?: number | null;
  pick_chance_label?: string | null;
  pick_edge_points?: number | null;
  favored_side: string;
  model_vs_market?: number | null;
  game_status?: string | null;
  home_score?: number | null;
  away_score?: number | null;
  pick_result?: "correct" | "miss" | "push" | null;
  ats_pick_label?: string | null;
  ats_side?: "home" | "away" | null;
  ats_team?: string | null;
  ats_line?: number | null;
  ats_cover_margin?: number | null;
  ats_pick_result?: "correct" | "miss" | "push" | null;
  projection_withheld?: boolean;
};

type View = "all" | "open" | "final";
type ResultFilter = "correct" | "miss" | "push";
type Book = "ml" | "spread";

export function AtsBoard({
  rows,
  disclaimer,
  league,
  sort,
  side,
  date,
  view,
  result,
  book,
}: {
  rows: AtsRow[];
  disclaimer: string;
  league: string;
  sort: string;
  side: string;
  date?: string;
  view: View;
  result?: ResultFilter;
  book?: Book;
}) {
  const openRows = rows.filter((row) => row.game_status !== "final");
  const finalRows = rows.filter((row) => row.game_status === "final");
  const settled = sort === "kickoff" ? [...finalRows].sort((a, b) => b.kickoff_utc.localeCompare(a.kickoff_utc)) : finalRows;
  const correct = settled.filter((row) => row.ats_pick_result === "correct").length;
  const misses = settled.filter((row) => row.ats_pick_result === "miss").length;
  const pushes = settled.filter((row) => row.ats_pick_result === "push").length;
  const wins = settled.filter((row) => row.pick_result === "correct").length;
  const losses = settled.filter((row) => row.pick_result === "miss").length;
  const mlPushes = settled.filter((row) => row.pick_result === "push").length;
  const shownSettled = result
    ? settled.filter((row) => (book === "ml" ? row.pick_result : row.ats_pick_result) === result)
    : settled;
  const listed = view === "open" ? openRows : view === "final" ? shownSettled : rows;
  const query = { league, sort, side, date };

  return (
    <div className="space-y-8">
      <div className="grid gap-3 lg:grid-cols-3">
        <Ledger
          kicker="Moneyline"
          wins={wins}
          losses={losses}
          pushes={mlPushes}
          winWord="Won"
          lossWord="Lost"
          bookName="ml"
          activeBook={book}
          activeResult={view === "final" ? result : undefined}
          query={query}
          view={view}
        />
        <Ledger
          kicker="Spread"
          wins={correct}
          losses={misses}
          pushes={pushes}
          winWord="Covered"
          lossWord="Missed"
          bookName="spread"
          activeBook={book}
          activeResult={view === "final" ? result : undefined}
          query={query}
          view={view}
        />
        <RecordLink
          href={toggleHref(query, view, result, book, "open")}
          label="Still to come"
          value={String(openRows.length)}
          tone="text-paper"
          active={view === "open"}
        />
      </div>

      <div className="flex flex-wrap items-center gap-2">
        {SORTS.map((item) => (
          <Link
            key={item.id}
            href={withQuery("/ats", { ...query, sort: item.id, view: view === "all" ? undefined : view, result, book })}
            className={chip(sort === item.id)}
          >
            {item.label}
          </Link>
        ))}
        <span className="mx-1 hidden h-5 w-px bg-white/15 sm:block" />
        {SIDES.map((item) => (
          <Link
            key={item.id}
            href={withQuery("/ats", { ...query, side: item.id, view: view === "all" ? undefined : view, result, book })}
            className={chip(side === item.id)}
          >
            {item.label}
          </Link>
        ))}
        {view !== "all" || result || book ? (
          <Link href={withQuery("/ats", query)} className="px-3 font-display text-lg uppercase tracking-wide text-gold">
            Clear filter
          </Link>
        ) : null}
      </div>

      {listed.length === 0 ? (
        <div className="plate px-6 py-8">
          <p className="font-display text-4xl uppercase leading-none">No games on the card</p>
          <p className="mt-3 max-w-xl text-sm text-mute">
            Nothing on this day matched the filter. Finished games stay on the day they were played.
          </p>
        </div>
      ) : (
        <section className="space-y-3">
          <SectionHead
            kicker="This day"
            title="Every game"
            meta={`${rows.length} games · ${finalRows.length} finished · Moneyline ${wins}–${losses} · Spread ${correct}–${misses}`}
          />
          <div className="grid gap-3 md:grid-cols-2">
            {listed.map((row) => (
              <CallCard key={row.game_id} row={row} />
            ))}
          </div>
        </section>
      )}

      <p className="max-w-3xl text-xs leading-relaxed text-mute">
        {disclaimer || "One pick per game. Not a guarantee and not a sportsbook."}
      </p>
    </div>
  );
}

const SORTS = [
  { id: "kickoff", label: "By kickoff" },
  { id: "cover_prob", label: "Best chance" },
  { id: "model_vs_market", label: "Biggest edge" },
];

const SIDES = [
  { id: "all", label: "All picks" },
  { id: "favorites", label: "Favorites" },
  { id: "underdogs", label: "Underdogs" },
];

function recordRate(wins: number, losses: number): string | null {
  const decided = wins + losses;
  if (decided === 0) return null;
  const text = ((wins / decided) * 100).toFixed(1);
  return `${text.endsWith(".0") ? text.slice(0, -2) : text}%`;
}

function rateSuffix(wins: number, losses: number): string {
  const rate = recordRate(wins, losses);
  return rate ? ` ${rate}` : "";
}

function toggleHref(
  query: { league: string; sort: string; side: string; date?: string },
  view: View,
  result: ResultFilter | undefined,
  book: Book | undefined,
  nextView: "open" | "final",
  nextResult?: ResultFilter,
  nextBook?: Book,
) {
  const same = view === nextView && result === nextResult && book === nextBook;
  return withQuery("/ats", {
    ...query,
    view: same ? undefined : nextView,
    result: same ? undefined : nextResult,
    book: same ? undefined : nextBook,
  });
}

function Ledger({
  kicker,
  wins,
  losses,
  pushes,
  winWord,
  lossWord,
  bookName,
  activeBook,
  activeResult,
  query,
  view,
}: {
  kicker: string;
  wins: number;
  losses: number;
  pushes: number;
  winWord: string;
  lossWord: string;
  bookName: Book;
  activeBook?: Book;
  activeResult?: ResultFilter;
  query: { league: string; sort: string; side: string; date?: string };
  view: View;
}) {
  const mark = (next: ResultFilter, word: string, count: number, tone: string) => {
    const on = activeBook === bookName && activeResult === next;
    return (
      <Link
        href={toggleHref(query, view, activeBook === bookName ? activeResult : undefined, activeBook, "final", next, bookName)}
        className={`font-mono text-[10px] uppercase tracking-[0.16em] ${on ? tone : "text-mute hover:text-paper"}`}
      >
        {word} {count}
      </Link>
    );
  };
  const rate = recordRate(wins, losses);
  return (
    <div className={`plate px-4 py-4 ${activeBook === bookName && view === "final" ? "border-gold/70" : ""}`}>
      <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-mute">{kicker}</p>
      <p className="mt-2 flex items-baseline gap-3">
        <span className="font-display text-5xl leading-none tracking-wide text-paper">
          {wins}–{losses}
        </span>
        {rate ? <span className="font-display text-2xl leading-none tracking-wide text-gold">{rate}</span> : null}
      </p>
      <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1">
        {mark("correct", winWord, wins, "text-gold")}
        {mark("miss", lossWord, losses, "text-heat")}
        {pushes > 0 ? mark("push", "Push", pushes, "text-paper") : null}
      </div>
    </div>
  );
}

function chip(active: boolean) {
  return `px-4 py-1.5 font-display text-lg uppercase tracking-wide ${
    active ? "bg-gold text-ink-950" : "border border-white/15 text-mute hover:text-paper"
  }`;
}

function RecordLink({
  href,
  label,
  value,
  tone,
  active,
}: {
  href: string;
  label: string;
  value: string;
  tone: string;
  active: boolean;
}) {
  return (
    <Link href={href} className={`plate block px-4 py-4 ${active ? "border-gold/70" : ""}`}>
      <div className="font-mono text-[10px] uppercase tracking-[0.22em] text-mute">{label}</div>
      <div className={`mt-2 font-display text-5xl uppercase leading-none tracking-wide ${tone}`}>{value}</div>
    </Link>
  );
}

function SectionHead({ kicker, title, meta }: { kicker: string; title: string; meta: string }) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-3 border-b border-white/10 pb-3">
      <div>
        <p className="kicker">{kicker}</p>
        <h2 className="mt-2 font-display text-4xl uppercase leading-none tracking-wide">{title}</h2>
      </div>
      <p className="font-mono text-[11px] uppercase tracking-[0.16em] text-mute">{meta}</p>
    </div>
  );
}

