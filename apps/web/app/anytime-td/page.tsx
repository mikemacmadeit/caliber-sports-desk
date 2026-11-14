import Link from "next/link";
import { redirect } from "next/navigation";
import { AnytimeTdRecord } from "@/components/AnytimeTdRecord";
import { AnytimeTdResearchBoard, type ResearchFinderPayload } from "@/components/AnytimeTdResearchBoard";
import type { AtdScoreRow, AtdTake } from "@/components/AnytimeTdScorecard";
import { DayStrip, EmptyDay } from "@/components/DayStrip";
import { LeagueToggle } from "@/components/LeagueToggle";
import { AnytimeTdBoard } from "@/components/AnytimeTdBoard";
import { PageHeader } from "@/components/PageHeader";
import { TeamMark } from "@/components/TeamMark";
import { apiGet, type PropRow } from "@/lib/api";
import { dayParts, type SlateDays, withQuery } from "@/lib/days";
import { kickoffParts, pct } from "@/lib/format";
import { presentTeam } from "@/lib/teams";

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
  let daysError: string | null = null;
  try {
    days = await apiGet<SlateDays>(`/v1/days?league=${league}`, 10000);
  } catch (e) {
    daysError = e instanceof Error ? e.message : "days unavailable";
  }

  const today = days?.today || new Date().toISOString().slice(0, 10);

  // No date in the URL + today has no TD prices → send people to the next day
  // that already has a board (usually Sunday). Avoids a dead "empty today" page.
  if (!researchView && !dateParam && days?.dates?.length) {
    const todayBoard = await apiGet<{ rows: PropRow[] }>(
      `/v1/props?market=anytime_td&league=${league}&date=${today}`,
      8000,
    ).catch(() => ({ rows: [] as PropRow[] }));
    if ((todayBoard.rows?.length ?? 0) === 0) {
      for (const day of days.dates.filter((d) => d > today).slice(0, 3)) {
        const probe = await apiGet<{ rows: PropRow[] }>(
          `/v1/props?market=anytime_td&league=${league}&date=${day}`,
          8000,
        ).catch(() => null);
        if (probe && (probe.rows?.length ?? 0) > 0) {
          redirect(
            withQuery("/anytime-td", {
              league: league === "all" ? undefined : league,
              date: day,
            }),
          );
        }
      }
    }
  }

  const selected = dateParam || today;

  // Parallel + short budgets: Netlify SSR dies around ~26–60s if these run in series.
  let data: { rows: PropRow[]; disclaimer: string } | null = null;
  let propsError: string | null = null;
  let called: { rows: CalledRow[] } | null = null;
  let research: ResearchFinderPayload | null = null;
  let researchError: string | null = null;
  let scorecard: {
    atd_scorecard?: { rows: AtdScoreRow[]; note?: string; open_and_settled_takes?: AtdTake[] };
  } | null = null;

  if (researchView) {
    try {
      research = await apiGet<ResearchFinderPayload>(`/v1/anytime-td/research?league=${league}`, 10000);
    } catch (e) {
      researchError = e instanceof Error ? e.message : "research unavailable";
      research = null;
    }
  } else {
    const [propsResult, calledResult, scoreResult] = await Promise.all([
      apiGet<{ rows: PropRow[]; disclaimer: string }>(
        `/v1/props?market=anytime_td&league=${league}&date=${selected}`,
        10000,
      ).then(
        (ok) => ({ ok: true as const, value: ok }),
        (e) => ({ ok: false as const, error: e instanceof Error ? e.message : "props unavailable" }),
      ),
      apiGet<{ rows: CalledRow[] }>(`/v1/anytime-td/called?league=${league}&date=${selected}`, 10000).catch(
        () => null,
      ),
      apiGet<{
        atd_scorecard?: { rows: AtdScoreRow[]; note?: string; open_and_settled_takes?: AtdTake[] };
      }>("/v1/shadow/track-record", 10000).catch(() => null),
    ]);
    if (propsResult.ok) data = propsResult.value;
    else propsError = propsResult.error;
    called = calledResult;
    scorecard = scoreResult;
  }
  const calledRows = called?.rows ?? [];
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
  });

  // Props can soft-fail and hydrate in the browser. Only hard-fail the research view.
  const loadFailure = researchView ? Boolean(researchError && !research) : Boolean(daysError && !days);
  void propsError;

  return (
    <div className="space-y-10">
      <PageHeader
        kicker={days && selected === days.today ? `Today · ${dayParts(selected).monthDay}` : dayParts(selected).monthDay}
        title="Anytime TD"
        lede="One player in each game — the one most likely to score a touchdown. The percentage is a chance, not a promise."
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
          <p className="mt-3 max-w-xl text-sm text-mute">
            Try again in a minute. This is not an empty slate.
          </p>
        </div>
      ) : researchView ? (
        <div className="space-y-4">
          <Link href={boardHref} className="font-display text-xl uppercase tracking-wide text-gold">
            ← Back to picks
          </Link>
          <p className="max-w-xl text-sm text-mute">
            This view compares the models. It is not the pick list.
          </p>
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
              initial={data}
              emptyTitle={`No odds posted ${when}`}
              emptyHref={
                next
                  ? withQuery("/anytime-td", {
                      league: league === "all" ? undefined : league,
                      date: days && next === days.today ? undefined : next,
                    })
                  : withQuery("/", { league, date: days && selected === days.today ? undefined : selected })
              }
              emptyLabel={
                next ? `Open ${dayParts(next).weekday} ${dayParts(next).monthDay} →` : "Open the slate →"
              }
            />
          )}
          {calledRows.length > 0 ? <CalledScorers rows={calledRows} /> : null}
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

type CalledRow = PropRow & {
  result?: string | null;
  game_status?: string | null;
  final_score?: string | null;
};

function CalledScorers({ rows }: { rows: CalledRow[] }) {
  const waiting = rows.filter((row) => row.result !== "scored" && row.result !== "blank");
  if (waiting.length === 0) return null;
  const ordered = [...waiting].sort((a, b) => resultRank(a) - resultRank(b) || a.player_name.localeCompare(b.player_name));
  return (
    <section className="space-y-3">
      <div className="border-b border-white/10 pb-2">
        <h2 className="font-display text-4xl uppercase leading-none tracking-wide">Already kicked off</h2>
        <p className="mt-2 max-w-xl text-sm text-mute">The name was locked in before the game. It joins the record once the result is in.</p>
      </div>
      <div className="grid gap-3 md:grid-cols-2">
        {ordered.map((row) => {
          const when = kickoffParts(row.kickoff_utc);
          const scored = row.result === "scored";
          const missed = row.result === "blank";
          const club = presentTeam({ abbreviation: row.team, mark: row.team_mark });
          return (
            <Link
              key={row.outcome_id}
              href={`/games/${row.game_id}`}
              className={
                scored
                  ? "plate block border-gold/50 px-5 py-5 shadow-[0_0_36px_rgba(214,255,63,0.14)]"
                  : "plate block px-4 py-4"
              }
            >
              <div className="flex items-start justify-between gap-3">
                <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-mute">
                  {row.league_id} · {when.day}
                  {row.final_score ? ` · ${row.final_score}` : ` · ${when.time}`}
                </p>
                <span
                  className={
                    scored
                      ? "bg-gold px-2 py-0.5 font-display text-lg uppercase leading-none tracking-wide text-ink-950"
                      : missed
                        ? "font-mono text-[10px] uppercase tracking-[0.16em] text-heat"
                        : "font-mono text-[10px] uppercase tracking-[0.16em] text-mute"
                  }
                >
                  {resultLabel(row)}
                </span>
              </div>
              <div className="mt-4 flex items-center gap-3">
                <TeamMark team={club} size="sm" />
                <p className={`truncate font-display uppercase leading-none tracking-wide ${scored ? "text-4xl" : "text-3xl"}`}>
                  {row.player_name}
                </p>
              </div>
              <p className="mt-3 text-sm text-paper/70">
                {row.away_abbr} at {row.home_abbr}
                {row.position ? ` · ${row.position}` : ""}
                {row.forecast.p_estimate != null ? ` · called at ${pct(row.forecast.p_estimate, 0)}` : ""}
              </p>
              {row.result !== "scored" && row.result !== "blank" && row.game_status === "final" ? (
                <p className="mt-2 text-sm text-mute">Final is in. Waiting to see if this player scored.</p>
              ) : null}
            </Link>
          );
        })}
      </div>
    </section>
  );
}

function resultRank(row: CalledRow): number {
  if (row.result === "scored") return 0;
  if (row.result === "pending") return 1;
  return 2;
}

function resultLabel(row: CalledRow): string {
  if (row.result === "scored") return "Scored";
  if (row.result === "blank") return "No score";
  if (row.game_status === "final") return "Pending";
  return "Live";
}
