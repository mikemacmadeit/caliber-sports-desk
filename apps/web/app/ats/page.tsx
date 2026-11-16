import { AtsBoard, type AtsRow } from "@/components/AtsBoard";
import { DayStrip } from "@/components/DayStrip";
import { LeagueToggle } from "@/components/LeagueToggle";
import { PageHeader } from "@/components/PageHeader";
import { apiGet } from "@/lib/api";
import { slateToday, type SlateDays } from "@/lib/days";
import { callsForDate } from "@/lib/atsDay";

type AtsPayload = {
  as_of: string;
  archive_note?: string;
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
  const [days, board] = await Promise.all([
    apiGet<SlateDays>(`/v1/days?league=${league}`, 20000).catch(() => null),
    apiGet<AtsPayload>(`/v1/ats?league=${league}&sort=${sort}&side=${side}&date=${selectedGuess}`, 20000).catch(
      (): AtsPayload => ({
        as_of: new Date().toISOString(),
        disclaimer: "The card is unavailable right now.",
        rows: [],
      }),
    ),
  ]);
  const today = days?.today ?? slateToday();
  const selected = dateParam || today;
  let data = board;
  const boardView = view === "open" || view === "final" ? view : "all";
  const boardResult = result === "correct" || result === "miss" || result === "push" ? result : undefined;
  const boardBook = book === "ml" || book === "spread" ? book : undefined;
  const dayRows = await callsForDate(data.rows, selected, league);
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
      {data.archive_note ? <p className="text-sm text-mute">{data.archive_note}</p> : null}
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
