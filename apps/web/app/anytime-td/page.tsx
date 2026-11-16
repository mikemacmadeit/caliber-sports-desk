import { Suspense } from "react";
import Link from "next/link";
import { AnytimeTdRecordPanel } from "@/components/AnytimeTdRecord";
import { AnytimeTdResearchBoard, type ResearchFinderPayload } from "@/components/AnytimeTdResearchBoard";
import { DayStrip, EmptyDay } from "@/components/DayStrip";
import { LeagueToggle } from "@/components/LeagueToggle";
import { AnytimeTdBoard, type BoardPayload } from "@/components/AnytimeTdBoard";
import { PageHeader } from "@/components/PageHeader";
import { apiGet } from "@/lib/api";
import { dayParts, slateToday, type SlateDays, withQuery } from "@/lib/days";

export default async function AnytimeTdPage({
  searchParams,
}: {
  searchParams: Promise<{ league?: string; date?: string; view?: string }>;
}) {
  const params = await searchParams;
  const league = params.league || "all";
  const dateParam = params.date;
  const researchView = params.view === "research";

  let days: SlateDays | null = null;
  try {
    days = await apiGet<SlateDays>(`/v1/days?league=${league}`, 6000);
  } catch {
    days = null;
  }

  const today = days?.today || slateToday();
  const nextGameDay = days?.dates.filter((d) => d >= today).sort()[0];
  const selected = dateParam || nextGameDay || today;
  const showingNextDay = !dateParam && selected > today;
  const next = days?.dates.find((d) => d > selected) ?? null;
  const when = days && selected === days.today ? "today" : dayParts(selected).monthDay;
  const researchHref = withQuery("/anytime-td", {
    league: league === "all" ? undefined : league,
    date: dateParam || (selected === today ? undefined : selected),
    view: "research",
  });
  const boardHref = withQuery("/anytime-td", {
    league: league === "all" ? undefined : league,
    date: dateParam || (selected === today ? undefined : selected),
  });

  let research: ResearchFinderPayload | null = null;
  let researchError: string | null = null;
  if (researchView) {
    try {
      research = await apiGet<ResearchFinderPayload>(`/v1/anytime-td/research?league=${league}`, 8000);
    } catch (e) {
      researchError = e instanceof Error ? e.message : "research unavailable";
      research = null;
    }
  }

  const loadFailure = researchView ? Boolean(researchError && !research) : false;
  const upcoming = (days?.dates || []).filter((d) => d >= selected).slice(0, 5);

  return (
    <div className="space-y-10">
      <PageHeader
        kicker={days && selected === days.today ? `Today · ${dayParts(selected).monthDay}` : dayParts(selected).monthDay}
        title="Anytime TD"
        lede="Combined-model picks and results for the selected day. Replays are separate from the original pregame record."
        accent="flare"
        action={
          <LeagueToggle
            value={league}
            basePath="/anytime-td"
            date={dateParam}
            extra={{ view: researchView ? "research" : undefined }}
          />
        }
      />
      {loadFailure ? (
        <div className="plate px-6 py-8">
          <p className="font-display text-4xl uppercase leading-none">Picks did not load</p>
          <p className="mt-3 max-w-xl text-sm text-mute">Try again in a minute. This is not an empty slate.</p>
        </div>
      ) : researchView ? (
        <div className="space-y-4">
          <Link href={boardHref} className="font-display text-xl uppercase tracking-wide text-gold">
            ← Back to picks
          </Link>
          <p className="max-w-xl text-sm text-mute">This view compares the models. It is not the pick list.</p>
          {research && (research.games || []).length > 0 ? (
            <AnytimeTdResearchBoard payload={research} />
          ) : (
            <div className="plate px-6 py-8">
              <p className="font-display text-4xl uppercase leading-none">Model notes are not ready</p>
              <p className="mt-3 max-w-xl text-sm text-mute">The pick list is still the page to use.</p>
            </div>
          )}
        </div>
      ) : (
        <>
          {showingNextDay ? (
            <p role="status" className="text-sm text-mute">
              No {league === "nfl" ? "NFL" : league === "fbs" ? "college football" : "football"} games today. Showing {dayParts(selected).weekday}, {dayParts(selected).monthDay} picks.
            </p>
          ) : null}
          {days ? (
            <DayStrip today={days.today} dates={days.dates} selected={selected} basePath="/anytime-td" league={league} explicitDates />
          ) : null}
          {days && !days.dates.includes(selected) ? (
            <EmptyDay
              selected={selected}
              today={days.today}
              next={next}
              href={next ? withQuery("/anytime-td", { league, date: next === days.today ? undefined : next }) : null}
            />
          ) : (
            <Suspense fallback={<p role="status">Loading saved touchdown picks…</p>}><SavedScorerBoard
              key={`${league}:${selected}`}
              league={league}
              date={selected}
              today={today}
              candidateDates={upcoming}
              emptyTitle={`No odds posted ${when}`}
            /></Suspense>
          )}
          <AnytimeTdRecordPanel league={league} />
          <p className="text-sm text-mute">
            <Link href={researchHref} className="text-gold">
              Model notes
            </Link>
            <span> · Chances only. We do not place bets.</span>
          </p>
        </>
      )}
    </div>
  );
}

async function SavedScorerBoard(props: Parameters<typeof AnytimeTdBoard>[0]) {
  let payload: BoardPayload | undefined;
  try { payload = await apiGet<BoardPayload>(`/v1/anytime-td/replay?${new URLSearchParams({league: props.league, date: props.date})}`, 6000); }
  catch { /* The client provides a retry and an explicit failure state. */ }
  return <AnytimeTdBoard {...props} initialCalled={payload?.rows} initialUnavailable={payload?.unavailable_games} initialRefresh={payload?.refresh_status} />;
}
