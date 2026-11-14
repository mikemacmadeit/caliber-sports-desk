import Link from "next/link";
import { AnytimeTdRecord } from "@/components/AnytimeTdRecord";
import { AnytimeTdResearchBoard, type ResearchFinderPayload } from "@/components/AnytimeTdResearchBoard";
import type { AtdScoreRow, AtdTake } from "@/components/AnytimeTdScorecard";
import { DayStrip, EmptyDay } from "@/components/DayStrip";
import { LeagueToggle } from "@/components/LeagueToggle";
import { AnytimeTdBoard } from "@/components/AnytimeTdBoard";
import { PageHeader } from "@/components/PageHeader";
import { apiGet } from "@/lib/api";
import { dayParts, type SlateDays, withQuery } from "@/lib/days";

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

  const today = days?.today || new Date().toISOString().slice(0, 10);
  const selected = dateParam || today;
  const next = days?.dates.find((d) => d > selected) ?? null;
  const when = days && selected === days.today ? "today" : dayParts(selected).monthDay;
  const researchHref = withQuery("/anytime-td", {
    league: league === "all" ? undefined : league,
    date: days && selected === days.today ? undefined : selected,
    view: "research",
  });
  const boardHref = withQuery("/anytime-td", {
    league: league === "all" ? undefined : league,
    date: days && selected === days.today ? undefined : selected,
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

  const scorecard = researchView
    ? null
    : await apiGet<{
        atd_scorecard?: { rows: AtdScoreRow[]; note?: string; open_and_settled_takes?: AtdTake[] };
      }>("/v1/shadow/track-record", 5000).catch(() => null);
  const scoreRows = scorecard?.atd_scorecard?.rows || [];
  const gradedTakes = scorecard?.atd_scorecard?.open_and_settled_takes || [];

  const loadFailure = researchView ? Boolean(researchError && !research) : false;
  const upcoming = (days?.dates || []).filter((d) => d >= selected).slice(0, 5);

  return (
    <div className="space-y-10">
      <PageHeader
        kicker={days && selected === days.today ? `Today · ${dayParts(selected).monthDay}` : dayParts(selected).monthDay}
        title="Anytime TD"
        lede="Desk picks are the names we grade. The board underneath is every priced chance for the day."
        accent="flare"
        action={
          <LeagueToggle
            value={league}
            basePath="/anytime-td"
            date={selected}
            today={days?.today ?? selected}
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
          {days ? (
            <DayStrip today={days.today} dates={days.dates} selected={selected} basePath="/anytime-td" league={league} />
          ) : null}
          {days && !days.dates.includes(selected) ? (
            <EmptyDay
              selected={selected}
              today={days.today}
              next={next}
              href={next ? withQuery("/anytime-td", { league, date: next === days.today ? undefined : next }) : null}
            />
          ) : (
            <AnytimeTdBoard
              league={league}
              date={selected}
              today={today}
              candidateDates={upcoming}
              emptyTitle={`No odds posted ${when}`}
            />
          )}
          <AnytimeTdRecord rows={scoreRows} takes={gradedTakes} league={league} />
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
