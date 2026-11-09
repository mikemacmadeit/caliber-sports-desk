import Link from "next/link";
import { AnytimeTdResearchBoard, type ResearchFinderPayload } from "@/components/AnytimeTdResearchBoard";
import { AnytimeTdScorecard, type AtdScoreRow, type AtdTake } from "@/components/AnytimeTdScorecard";
import { DayStrip, EmptyDay } from "@/components/DayStrip";
import { DeskStatusBanner, type DeskStatus } from "@/components/DeskStatusBanner";
import { LeagueToggle } from "@/components/LeagueToggle";
import { PageHeader } from "@/components/PageHeader";
import { PropExplorer } from "@/components/PropExplorer";
import { apiGet, type PropRow } from "@/lib/api";
import { dayParts, type SlateDays, withQuery } from "@/lib/days";

export default async function AnytimeTdPage({
  searchParams,
}: {
  searchParams: Promise<{ league?: string; date?: string; view?: string }>;
}) {
  const params = await searchParams;
  const league = params.league || "all";
  const dateParam = params.date;
  // FBS research board defaults to C_recent finder (experimental). Priced board still available.
  const view = params.view || (league === "fbs" ? "research" : "board");
  const researchView = view === "research";

  let days: SlateDays | null = null;
  let daysError: string | null = null;
  try {
    days = await apiGet<SlateDays>(`/v1/days?league=${league}`, 15000);
  } catch (e) {
    daysError = e instanceof Error ? e.message : "days unavailable";
  }

  const selected = dateParam || days?.today || new Date().toISOString().slice(0, 10);

  // Research view does not render the priced board — skip the heavy props scan so
  // ingest overlap cannot 500 the finder behind a 15s AbortSignal on /v1/props.
  let data: { rows: PropRow[]; disclaimer: string } | null = null;
  let propsError: string | null = null;
  if (!researchView) {
    try {
      data = await apiGet<{ rows: PropRow[]; disclaimer: string }>(
        `/v1/props?market=anytime_td&league=${league}&date=${selected}`,
        45000,
      );
    } catch (e) {
      propsError = e instanceof Error ? e.message : "props unavailable";
    }
  }

  let research: ResearchFinderPayload | null = null;
  let researchError: string | null = null;
  try {
    research = await apiGet<ResearchFinderPayload>(`/v1/anytime-td/research?league=${league}`, 20000);
  } catch (e) {
    researchError = e instanceof Error ? e.message : "research unavailable";
    research = null;
  }

  const [scorecard, deskStatus] = await Promise.all([
    apiGet<{
      atd_scorecard?: { rows: AtdScoreRow[]; note?: string; open_and_settled_takes?: AtdTake[] };
    }>("/v1/shadow/track-record", 45000).catch(() => null),
    apiGet<DeskStatus>("/v1/atd/desk-status", 30000).catch(() => null),
  ]);
  const scoreRows = scorecard?.atd_scorecard?.rows || [];
  const gradedTakes = scorecard?.atd_scorecard?.open_and_settled_takes || [];

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
    view: "board",
  });

  const loadFailure = researchView
    ? Boolean(researchError && !research)
    : Boolean(daysError || propsError);

  return (
    <div className="space-y-10">
      <PageHeader
        kicker={days && selected === days.today ? `Today · ${dayParts(selected).monthDay}` : dayParts(selected).monthDay}
        title="Anytime TD"
        lede={
          league === "fbs"
            ? "FBS desk picks the player most likely to score in each game. Research primary is a blend (experimental). Official Play stays off."
            : "One scorer per game: the player most likely to score tonight, from the depth chart, recent carries and targets, recent scores, the defense, the weather, and injuries."
        }
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
      <div className="flex flex-wrap gap-2">
        <Link
          href={boardHref}
          className={`px-3 py-1.5 font-display text-lg uppercase tracking-wide ${
            !researchView ? "bg-gold text-ink-950" : "border border-white/15 text-mute"
          }`}
        >
          Priced board
        </Link>
        <Link
          href={researchHref}
          className={`px-3 py-1.5 font-display text-lg uppercase tracking-wide ${
            researchView ? "bg-gold text-ink-950" : "border border-white/15 text-mute"
          }`}
        >
          Research finder
        </Link>
      </div>

      <DeskStatusBanner status={deskStatus} />

      <AnytimeTdScorecard
        rows={scoreRows}
        note={scorecard?.atd_scorecard?.note}
        league={league}
        takes={gradedTakes}
      />

      {loadFailure ? (
        <div className="plate px-6 py-8">
          <p className="font-display text-4xl uppercase leading-none">Desk temporarily busy</p>
          <p className="mt-3 max-w-xl text-sm text-mute">
            The Anytime TD API did not answer in time — usually because an ingest or capture job is
            holding the database. This is not an empty slate and not a successful refresh. Retry in a
            minute; freshness labels stay honest when data returns.
          </p>
          <p className="mt-3 font-mono text-xs text-mute">
            {[daysError, propsError, researchError].filter(Boolean).join(" · ")}
          </p>
        </div>
      ) : researchView ? (
        research && (research.games || []).length > 0 ? (
          <AnytimeTdResearchBoard payload={research} />
        ) : (
          <div className="plate px-6 py-8">
            <p className="font-display text-4xl uppercase leading-none">Research payload not ready</p>
            <p className="mt-3 max-w-xl text-sm text-mute">
              Run <span className="font-mono text-paper">python -m worker.main atd-reliability-guardrails</span> to
              build dual-model warnings for the finder. Official board forecasts stay untouched.
            </p>
          </div>
        )
      ) : (
        <>
          {days ? (
            <DayStrip today={days.today} dates={days.dates} selected={selected} basePath="/anytime-td" league={league} />
          ) : null}
          {(data?.rows.length ?? 0) === 0 ? (
            days && days.dates.includes(selected) ? (
              <div className="plate px-6 py-8">
                <p className="font-display text-4xl uppercase leading-none">No prices {when}</p>
                <p className="mt-3 max-w-xl text-sm text-mute">
                  The games are on the board. This market has no locked number for them yet.
                </p>
                <Link
                  href={withQuery("/", { league, date: selected === days.today ? undefined : selected })}
                  className="mt-4 inline-block font-display text-2xl uppercase tracking-wide text-gold"
                >
                  Open the slate →
                </Link>
              </div>
            ) : (
              <EmptyDay
                selected={selected}
                today={days?.today ?? selected}
                next={next}
                href={next ? withQuery("/anytime-td", { league, date: days && next === days.today ? undefined : next }) : null}
              />
            )
          ) : (
            <PropExplorer rows={data!.rows} mode="calls" />
          )}
        </>
      )}
    </div>
  );
}
