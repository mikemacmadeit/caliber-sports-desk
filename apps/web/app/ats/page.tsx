import { AtsBoard, type AtsRow } from "@/components/AtsBoard";
import { DayStrip } from "@/components/DayStrip";
import { LeagueToggle } from "@/components/LeagueToggle";
import { PageHeader } from "@/components/PageHeader";
import { apiGet } from "@/lib/api";
import { slateToday, validDay, validLeague, withQuery, type SlateDays } from "@/lib/days";
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
  const params = await searchParams;
  const league = validLeague(params.league);
  const dateParam = validDay(params.date);
  const sort = params.sort === "cover_prob" || params.sort === "model_vs_market" ? params.sort : "kickoff";
  const side = params.side === "favorites" || params.side === "underdogs" ? params.side : "all";
  const { view, result, book } = params;
  const selectedGuess = dateParam || slateToday();
  const [days, board] = await Promise.all([
    apiGet<SlateDays>(`/v1/days?league=${league}`).catch(() => null),
    apiGet<AtsPayload>(`/v1/ats?league=${league}&sort=${sort}&side=${side}&date=${selectedGuess}`).catch(() => null),
  ]);
  const today = days?.today ?? slateToday();
  const selected = dateParam || today;
  const data = selected === selectedGuess ? board : await apiGet<AtsPayload>(
    `/v1/ats?league=${league}&sort=${sort}&side=${side}&date=${selected}`).catch(() => null);
  const boardView = view === "open" || view === "final" ? view : "all";
  const boardResult = result === "correct" || result === "miss" || result === "push" ? result : undefined;
  const boardBook = book === "ml" || book === "spread" ? book : undefined;
  const dayRows = await callsForDate(data?.rows ?? [], selected, league);
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
      {data?.archive_note ? <p className="text-sm text-mute">{data.archive_note}</p> : null}
      {!data ? <div className="plate px-6 py-8" role="status">
        <p className="font-display text-3xl uppercase">Picks did not load</p>
        <p className="mt-3 text-sm text-mute">The board is temporarily unavailable. This is not an empty slate.</p>
        <a className="mt-4 inline-block text-gold" href={withQuery("/ats", { league, date: selected, ...filters })}>Try again</a>
      </div> : <AtsBoard
        rows={dayRows}
        disclaimer={data.disclaimer}
        league={league}
        sort={sort}
        side={side}
        date={selected === today ? undefined : selected}
        view={boardView}
        result={boardResult}
        book={boardBook}
      />}
    </div>
  );
}
