import { AtsBoard, type AtsRow } from "@/components/AtsBoard";
import { DayStrip } from "@/components/DayStrip";
import { LeagueToggle } from "@/components/LeagueToggle";
import { PageHeader } from "@/components/PageHeader";
import savedBoard from "@/lib/ats/board.json";
import { apiGet } from "@/lib/api";
import { slateToday, type SlateDays } from "@/lib/days";
import { finalsForDate, gradeFinishedCall } from "@/lib/gradeCalls";
import { kickoffDate } from "@/lib/format";

type AtsPayload = {
  as_of: string;
  disclaimer: string;
  rows: AtsRow[];
};

export default async function AtsPage({
  searchParams,
}: {
  searchParams: Promise<{ league?: string; sort?: string; side?: string; view?: string; result?: string; book?: string; date?: string }>;
}) {
  const { league = "all", sort = "kickoff", side = "all", view, result, book, date: dateParam } = await searchParams;
  const selectedGuess = dateParam || slateToday();
  const [days, board, finals] = await Promise.all([
    apiGet<SlateDays>(`/v1/days?league=${league}`, 20000).catch(() => null),
    apiGet<AtsPayload>(`/v1/ats?league=${league}&sort=${sort}&side=${side}`, 20000).catch(
      (): AtsPayload => ({
        as_of: new Date().toISOString(),
        disclaimer: "The card is unavailable right now.",
        rows: [],
      }),
    ),
    finalsForDate(selectedGuess),
  ]);
  const today = days?.today ?? slateToday();
  const selected = dateParam || today;
  const scores = selected === selectedGuess ? finals : await finalsForDate(selected);
  let data = board;
  const boardView = view === "open" || view === "final" ? view : "all";
  const boardResult = result === "correct" || result === "miss" || result === "push" ? result : undefined;
  const boardBook = book === "ml" || book === "spread" ? book : undefined;
  const source = data.rows.length ? data.rows : (savedBoard.rows as AtsRow[]);
  const dayRows = source
    .filter((row) => kickoffDate(row.kickoff_utc) === selected)
    .map((row) => gradeFinishedCall(row, scores));
  const filters = {
    sort,
    side,
    view: boardView === "all" ? undefined : boardView,
    result: boardResult,
    book: boardBook,
  };

  return (
    <div className="space-y-8">
      <PageHeader
        kicker="The card"
        title="Against the Spread"
        lede="Every game on the day stays up. A close NFL game gets our side when the score is a point to a touchdown off the number. A close college game gets our side inside a field goal of that. A game a touchdown apart takes the number's side. Finished games show what hit and what missed."
        action={<LeagueToggle value={league} basePath="/ats" date={selected} today={today} extra={filters} />}
      />
      {days ? (
        <DayStrip today={today} dates={days.dates} selected={selected} basePath="/ats" league={league} extra={filters} />
      ) : null}
      <AtsBoard
        rows={dayRows}
        disclaimer={data.disclaimer}
        league={league}
        sort={sort}
        side={side}
        date={selected === today ? undefined : selected}
        view={boardView}
        result={boardResult}
        book={boardBook}
      />
    </div>
  );
}
