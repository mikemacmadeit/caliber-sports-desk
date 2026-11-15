"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import type { PropRow, TdCustomerExplanation, TdDecisionCandidate } from "@/lib/api";
import { PropExplorer } from "@/components/PropExplorer";
import { TeamMark } from "@/components/TeamMark";
import { dayParts } from "@/lib/days";
import { browserGet, peekBrowserCache, rememberBrowserCache } from "@/lib/browserApi";
import { deskPickReason } from "@/lib/atdDeskTakes";
import { american, kickoffDate, kickoffParts, pct } from "@/lib/format";
import { presentTeam } from "@/lib/teams";

type BoardPayload = { rows: PropRow[]; disclaimer?: string; refresh_status?: string; unavailable_games?: { game_id: string; reason: string }[] };

function calledPath(league: string, date: string): string {
  const params = new URLSearchParams({ league, date });
  return `/v1/anytime-td/replay?${params}`;
}

function boardPath(league: string, date: string): string {
  const params = new URLSearchParams({ market: "anytime_td", league, date });
  return `/v1/props?${params}`;
}

export function AnytimeTdBoard({
  league,
  date,
  today,
  candidateDates,
  emptyTitle,
}: {
  league: string;
  date: string;
  today: string;
  candidateDates: string[];
  emptyTitle: string;
}) {
  const path = boardPath(league, date);
  const cached = peekBrowserCache<BoardPayload>(path);
  const cachedPicks = peekBrowserCache<BoardPayload>(calledPath(league, date));
  const [data, setData] = useState<BoardPayload | null>(cached);
  const [called, setCalled] = useState<PropRow[]>(cachedPicks?.rows || []);
  const [unavailable, setUnavailable] = useState<NonNullable<BoardPayload["unavailable_games"]>>(cachedPicks?.unavailable_games || []);
  const [error, setError] = useState<string | null>(null);
  const [oddsError, setOddsError] = useState<string | null>(null);
  const [resultFilter, setResultFilter] = useState("all");
  const [loading, setLoading] = useState(!((cachedPicks?.rows?.length ?? 0) > 0));
  const [refresh, setRefresh] = useState(0);
  const [scorerStatus, setScorerStatus] = useState<string | undefined>();
  useEffect(() => {
    if (loading && !called.length) return;
    const timer = setTimeout(() => setRefresh(n => n + 1), called.length ? 20000 : 60000);
    return () => clearTimeout(timer);
  }, [scorerStatus, refresh, loading, called.length]);

  useEffect(() => {
    const ctrl = new AbortController();
    const warmPicks = peekBrowserCache<BoardPayload>(calledPath(league, date));
    const warm = peekBrowserCache<BoardPayload>(boardPath(league, date));
    if (warmPicks?.rows?.length) {
      setCalled(warmPicks.rows);
      setUnavailable(warmPicks.unavailable_games || []);
      setLoading(false);
      setError(null);
    } else if (!called.length) {
      setLoading(true);
      setError(null);
    }
    if (warm && (warm.rows?.length ?? 0) > 0) setData(warm);

    // Price research must not block the day's saved scorer cards.
    void browserGet<BoardPayload>(calledPath(league, date), ctrl.signal).then(locked => {
      if (ctrl.signal.aborted) return;
      rememberBrowserCache(calledPath(league, date), locked);
      setCalled(locked.rows || []);
      setUnavailable(locked.unavailable_games || []);
      setScorerStatus(locked.refresh_status);
      setError(null);
    }).catch((err: unknown) => {
      if (!ctrl.signal.aborted && !called.length) setError(err instanceof Error ? err.message : "Picks unavailable");
    }).finally(() => { if (!ctrl.signal.aborted) setLoading(false); });
    void browserGet<BoardPayload>(boardPath(league, date), ctrl.signal).then(payload => {
      if (ctrl.signal.aborted) return;
      const rows = (payload.rows || []).filter(row => !row.kickoff_utc || kickoffDate(row.kickoff_utc) === date);
      setData({ ...payload, rows });
      setOddsError(null);
    }).catch((err: unknown) => {
      if (!ctrl.signal.aborted) setOddsError(err instanceof Error ? err.message : "Odds unavailable");
    });

    return () => ctrl.abort();
  }, [league, date, refresh]);

  const deskPicks = useMemo(() => {
    return called.filter(row => !row.kickoff_utc || kickoffDate(row.kickoff_utc) === date)
      .sort((a, b) => String(a.kickoff_utc || "").localeCompare(String(b.kickoff_utc || "")));
  }, [called, date]);

  if (loading && !(data?.rows?.length) && !deskPicks.length) {
    return <p className="text-sm text-mute">Loading saved touchdown picks…</p>;
  }
  if (error && !(data?.rows?.length) && !deskPicks.length) {
    return (
      <div className="plate px-6 py-8">
        <p className="font-display text-4xl uppercase leading-none">Picks did not load</p>
        <p className="mt-3 max-w-xl text-sm text-mute">Try again in a minute. This is not an empty slate.</p>
      </div>
    );
  }
  if (!(data?.rows?.length) && !deskPicks.length && !unavailable.length) {
    const next = candidateDates.find((d) => d > date) ?? null;
    const nextParams = new URLSearchParams();
    if (league && league !== "all") nextParams.set("league", league);
    if (next && next !== today) nextParams.set("date", next);
    const nextHref = nextParams.size ? `/anytime-td?${nextParams}` : "/anytime-td";
    return (
      <div className="plate px-6 py-8">
        <p className="font-display text-4xl uppercase leading-none">{emptyTitle}</p>
        <p className="mt-3 max-w-xl text-sm text-mute">{data?.refresh_status === "running" ? "Updating player research. This board will refresh automatically." : data?.refresh_status === "failed" ? "Research could not update. Please retry after the next refresh." : "No touchdown research is currently published for this date."}</p>
        {next ? (
          <Link href={nextHref} className="mt-4 inline-block font-display text-2xl uppercase tracking-wide text-gold">
            Open {dayParts(next).weekday} {dayParts(next).monthDay} →
          </Link>
        ) : null}
      </div>
    );
  }

  return (
    <div className="space-y-10">
      <section className="space-y-4">
        <div className="border-b border-white/10 pb-2">
          <h2 className="font-display text-4xl uppercase leading-none tracking-wide">Anytime touchdown picks</h2>
          <p className="mt-2 max-w-xl text-sm text-mute">
              A main pick and an alternate for each game. Both use our football analysis; the odds are checked separately. Green means a touchdown; red means no touchdown.
          </p>
        </div>
        {error ? <p role="alert" className="text-heat">The replay could not refresh. Any cards shown are from the last successful load.</p> : null}
        {scorerStatus === "running" ? <p role="status" className="text-sm text-mute">Saved picks are shown while the latest pregame checks update.</p> : scorerStatus === "failed" ? <p role="status" className="text-sm text-mute">The update failed. Showing the last saved picks with current availability checks.</p> : null}
        <DailyTdResults rows={deskPicks} date={date} filter={resultFilter} onFilter={setResultFilter} />
        <DailyDecisionResults rows={deskPicks} />
        {deskPicks.length === 0 ? (
          <div className="plate px-5 py-5">
            <p className="font-display text-3xl uppercase leading-none">{scorerStatus === "running" ? "Updating touchdown picks" : "No combined pick available"}</p>
            <p className="mt-2 max-w-xl text-sm text-mute">
              {scorerStatus === "running" ? "The model is preparing this slate. Picks will appear automatically when the update finishes." : "No saved eligible player is available for this slate. See the game status below."}
            </p>
          </div>
        ) : (
          <div className="grid gap-3 md:grid-cols-2">
            {deskPicks.filter(row => resultFilter === "all" || (resultFilter === "pending" ? row.result !== "scored" && row.result !== "blank" : row.result === resultFilter)).map((row) => (
              <DeskPickCard key={row.outcome_id} row={row} />
            ))}
          </div>
        )}
        {unavailable.length ? <details className="text-sm text-mute"><summary>{unavailable.length} games without a current pick</summary><ul className="mt-2 space-y-1">{unavailable.map(g => <li key={g.game_id}>{g.game_id}: {g.reason}</li>)}</ul></details> : null}
      </section>

      <section className="space-y-4">
        <div className="border-b border-white/10 pb-2">
          <h2 className="font-display text-4xl uppercase leading-none tracking-wide">The board</h2>
          <p className="mt-2 max-w-xl text-sm text-mute">Every priced touchdown chance for the day.</p>
        </div>
        {oddsError ? <p role="status" className="text-sm text-mute">The odds table could not refresh. Scorer picks and their results are loaded separately.</p> : null}
        <PropExplorer rows={data?.rows || []} mode="flat" />
      </section>
    </div>
  );
}

function extraResult(row: PropRow, result: string) {
  const alternative = row.decision?.value_candidate;
  return Boolean(
    alternative &&
    alternative.player_id !== row.player_id &&
    row.decision_results?.value_candidate?.result === result,
  );
}

function DailyTdResults({ rows, date, filter, onFilter }: { rows: PropRow[]; date: string; filter: string; onFilter: (value: string) => void }) {
  const wins = rows.filter(r => r.result === "scored").length + rows.filter(r => extraResult(r, "scored")).length;
  const losses = rows.filter(r => r.result === "blank").length + rows.filter(r => extraResult(r, "blank")).length;
  const pending = rows.length - wins - losses;
  const awaiting = rows.filter(r => r.result === "awaiting_box").length;
  return <div className="plate space-y-3 p-4" aria-label="Daily touchdown results">
    <p className="font-display text-2xl uppercase">{date} · Daily replay results</p>
    <p className="text-sm text-mute">{wins + losses ? `${Math.round(100 * wins / (wins + losses))}% hit rate across ${wins + losses} graded games.` : "No games graded yet."} {awaiting ? `${awaiting} final games await participation or box-score confirmation; excluded from wins and losses.` : ""} Replays are not the original pregame record.</p>
    <div className="flex flex-wrap gap-3">
      {[["all", `All ${rows.length}`], ["scored", `${wins} winners`], ["blank", `${losses} losers`], ["pending", `${pending} pending / ungraded`]].map(([value, label]) =>
        <button key={value} type="button" aria-pressed={filter === value} onClick={() => onFilter(value)} className={`rounded border px-3 py-2 ${value === "scored" ? "border-green-500 text-green-300" : value === "blank" ? "border-red-500 text-red-300" : "border-white/20"} ${filter === value ? "bg-white/10" : ""}`}>{label}</button>)}
    </div>
  </div>;
}

function DeskPickCard({ row }: { row: PropRow }) {
  const when = kickoffParts(row.kickoff_utc);
  const club = presentTeam({ abbreviation: row.team, mark: row.team_mark });
  const chance = row.forecast.p_estimate;
  const won = row.result === "scored", lost = row.result === "blank";
  const report = row.desk_analysis?.scouting_report;
  const resultLabel = won ? "Touchdown - win" : lost ? "No touchdown - loss" : row.result === "awaiting_box" ? "Game finished - waiting for the player result" : row.game_status === "in_progress" ? "In progress" : null;
  return <article className={`relative rounded-xl border p-5 ${won ? "border-green-500 bg-green-950/50" : lost ? "border-red-500 bg-red-950/50" : "plate"}`}>
    {won || lost ? <span aria-label={won ? "Win" : "Loss"} className={`absolute right-3 top-3 flex h-7 w-7 items-center justify-center rounded-full text-xl font-bold ${won ? "bg-green-500 text-black" : "bg-red-500 text-white"}`}>{won ? "\u2713" : "\u2715"}</span> : null}
    <p className="pr-8 text-xs text-mute">{row.league_id === "fbs" ? "College football" : "NFL"} · {row.away_abbr} at {row.home_abbr} · {when.day}, {when.time}</p>
    <div className="mt-4 flex items-center gap-3">
      <TeamMark team={club} size="sm" />
      <div className="min-w-0"><p className="text-xs font-semibold uppercase tracking-wide text-gold">{row.locked ? "Subscriber pick" : report?.customer?.label || "Our touchdown pick"}</p>
        <Link href={`/games/${row.game_id}`} className="font-display text-3xl leading-tight hover:text-gold">{row.locked ? "Pregame pick locked" : row.player_name}</Link>
        {!row.locked ? <p className="mt-1 text-xs text-mute">{({QB:"Quarterback",RB:"Running back",FB:"Fullback",WR:"Wide receiver",TE:"Tight end"} as Record<string,string>)[row.position || ""] || row.position} · {row.team}</p> : null}
      </div>
    </div>
    <div className="mt-4 grid grid-cols-2 gap-3 rounded-lg bg-black/15 p-3">
      <div><p className="text-xs text-mute">Estimated chance to score</p><p className="mt-1 font-display text-3xl text-gold">{chance == null ? "—" : pct(chance, 0)}</p></div>
      <div><p className="text-xs text-mute">Saved odds</p><p className="mt-1 font-mono text-2xl">{american(row.book_price)}</p></div>
    </div>
    {resultLabel || row.final_score ? <p className={`mt-3 text-sm font-semibold ${won ? "text-green-300" : lost ? "text-red-300" : "text-mute"}`}>{resultLabel}{row.final_score ? `${resultLabel ? " · " : ""}Final ${row.final_score}` : ""}</p> : null}
    {row.retrospective !== false && !row.locked ? <p className="mt-1 text-xs text-mute">Past-game rerun · not an original pregame pick</p> : null}
    {row.locked ? <p className="mt-4 text-sm text-mute">Sign in with a subscription to see the pick and explanation.</p> : <>
      {row.selection_readiness?.status === "provisional" ? <p className="mt-3 text-xs text-amber-200">{row.retrospective === false ? "Check playing status and role before kickoff." : "Playing status or role was not fully confirmed when this pick was made."}</p> : null}
      {report?.customer ? <PickHighlights copy={report.customer} candidate={row.decision?.most_likely} opponent={row.opponent} /> : <p className="mt-3 text-sm text-paper/80">{deskPickReason(row)}</p>}
      <PriceNote candidate={row.decision?.most_likely} />
      {row.decision ? <PriceDecision row={row} /> : null}
      {report || row.decision?.value_candidate?.customer ? <details className="mt-4 border-t border-paper/15 pt-3">
        <summary className="cursor-pointer text-sm font-semibold text-gold">More analysis</summary>
        <div className="mt-4 space-y-5">
          {report?.customer ? <CustomerDetails copy={report.customer} name={row.player_name} /> : null}
          {row.decision?.value_candidate?.customer ? <CustomerDetails copy={row.decision.value_candidate.customer} name={row.decision.value_candidate.name} /> : null}
          {row.decision ? <PlayerComparison row={row} /> : null}
          {report ? <ScoringCase row={row} /> : null}
        </div>
      </details> : row.desk_analysis ? <details className="mt-3 text-xs text-mute"><summary className="cursor-pointer">Sources and notes</summary>
        {[...(row.desk_analysis.reasons || []), ...(row.desk_analysis.risks || [])].map((text, i) => <p key={i} className="mt-2">{text}</p>)}
        {row.desk_analysis.current_context?.weather ? <p className="mt-2">Weather is shown for context and has not changed this estimate.</p> : null}
      </details> : null}
    </>}
  </article>;
}

function firstSentence(text: string): string {
  return text.trim().split(/(?<=[.!?])\s+(?=[A-Z])/)[0] || "";
}

function PickHighlights({ copy, candidate, opponent }: { copy: TdCustomerExplanation; candidate?: TdDecisionCandidate; opponent: string }) {
  const research = candidate?.research;
  const qb = research?.quarterback;
  const receiving = candidate?.position === "WR" || candidate?.position === "TE";
  const role = receiving && qb?.usable && qb.player_redzone_targets > 0
    ? `${qb.player_redzone_targets} red-zone targets and ${qb.receiving_tds} receiving TDs with ${qb.name || "the expected quarterback"} across ${qb.games} games.`
    : firstSentence(copy.why);
  const defense = research?.opponent;
  const games = research?.opponent_games || 0;
  const group = candidate?.position === "TE" ? "tight ends" : candidate?.position === "WR" ? "wide receivers" : candidate?.position === "QB" ? "quarterbacks" : "running backs";
  const matchup = defense && games > 0 && (receiving ? defense.targets > 0 : defense.carries > 0)
    ? receiving
      ? `${opponent} allowed ${defense.receiving_tds} receiving TDs and ${(defense.receiving_yards / games).toFixed(0)} yards per game to ${group} across ${games} games.`
      : `${opponent} allowed ${(defense.rushing_yards / defense.carries).toFixed(1)} yards per carry and ${defense.rushing_tds} rushing TDs to ${group} across ${games} games.`
    : firstSentence(copy.matchup);
  return <ul aria-label="Key reasons" className="mt-3 list-disc space-y-1.5 pl-4 text-sm leading-relaxed text-paper/85">
    {[role, matchup].filter(Boolean).map((fact, i) => <li key={i}>{fact}</li>)}
  </ul>;
}

function PriceNote({ candidate }: { candidate?: TdDecisionCandidate }) {
  if (!candidate || candidate.price_supported) return null;
  const stale = candidate.price_status === "no_fresh_price";
  return <p className="mt-2 text-xs text-amber-200">
    {stale ? "Check current odds. " : ""}
    {candidate.minimum_american != null ? `Target odds: ${american(candidate.minimum_american)} or better.` : stale ? "" : "Saved odds do not pass our value check."}
  </p>;
}

function CustomerDetails({ copy, name }: { copy: TdCustomerExplanation; name: string }) {
  return <section className="space-y-3 text-sm leading-relaxed">
    <h3 className="font-semibold text-paper">{name}</h3>
    <p className="text-paper/80">{copy.why}</p><p className="text-paper/80">{copy.matchup}</p>
    {copy.evidence?.length ? <ul className="list-disc space-y-2 pl-4 text-paper/80">{copy.evidence.map((fact, i) => <li key={i}>{fact}</li>)}</ul> : null}
    <p className="text-paper/75"><span className="font-semibold text-amber-200">The risk: </span>{copy.caution}</p>
    <p className="text-paper/80">{copy.price}</p>
    {copy.comparison ? <div><h4 className="font-semibold text-paper">{copy.comparison.heading}</h4><p className="mt-1 text-paper/80">{copy.comparison.text}</p></div> : null}
    <p className="text-xs text-mute">{copy.research_note}</p>
  </section>;
}

function DailyDecisionResults({ rows }: { rows: PropRow[] }) {
  const captured = rows.filter(r => !r.locked && r.retrospective === false && r.decision);
  if (!captured.length) return null;
  return <section aria-label="Separate pregame strategy records" className="grid gap-3 sm:grid-cols-2">
    {([['most_likely', 'Main-pick record'], ['value_candidate', 'Alternative-pick record']] as const).map(([lane, label]) => {
      const results = captured.map(r => r.decision_results?.[lane]).filter(Boolean);
      const wins = results.filter(r => r?.result === 'scored').length;
      const losses = results.filter(r => r?.result === 'blank').length;
      return <div key={lane} className="rounded border border-paper/15 p-3 text-sm">
        <p className="font-semibold">{label}</p>
        <p className="mt-1">{wins} wins · {losses} losses · {results.length - wins - losses} pending</p>
        <p className="mt-1 text-xs text-mute">Picks saved before kickoff. Past-game reruns are excluded.</p>
        {lane === 'value_candidate' ? <p className="mt-1 text-xs text-mute">{results.filter(r => r?.bet_qualified).length} had fresh odds that passed the value check. Scorer results and betting returns are tracked separately.</p> : null}
      </div>;
    })}
  </section>;
}

function PriceDecision({ row }: { row: PropRow }) {
  const d = row.decision;
  if (!d || row.locked) return null;
  const value = d.value_candidate;
  const settled = row.decision_results?.value_candidate?.result;
  return <section aria-label="Scorer and price decision" className="mt-4 border-t border-paper/15 pt-4 text-sm">
      {value && value.player_id !== row.player_id ? <div className={`relative rounded-lg p-3 ${settled === 'scored' ? 'border border-green-500 bg-green-950/40' : settled === 'blank' ? 'border border-red-500 bg-red-950/40' : 'bg-black/15'}`}>
        {settled === 'scored' || settled === 'blank' ? <span aria-label={settled === 'scored' ? 'Win' : 'Loss'} className={`absolute right-3 top-3 flex h-7 w-7 items-center justify-center rounded-full text-xl font-bold ${settled === 'scored' ? 'bg-green-500 text-black' : 'bg-red-500 text-white'}`}>{settled === 'scored' ? '\u2713' : '\u2715'}</span> : null}
        <p className="text-xs font-semibold uppercase tracking-wide text-gold">Alternate pick</p>
        <div className="mt-1 flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 pr-8"><p className="font-display text-2xl">{value.name}</p><p className="text-xs text-mute">{pct(value.probability, 0)} chance · <span className="font-mono text-paper">{american(value.american)}</span> saved odds</p></div>
        <DecisionResult result={settled} />
        {value.customer ? <PickHighlights copy={value.customer} candidate={value} opponent={value.team === row.team ? row.opponent : row.team} /> : <p className="mt-2 text-xs text-mute">Detailed reasoning was not saved with this pick.</p>}
        {value.customer?.caution?.includes("before kickoff") || value.cautions?.some(c => /injury designation|playing status/i.test(c)) ? <p className="mt-2 text-xs text-amber-200">Check playing status before kickoff.</p> : null}
        <PriceNote candidate={value} />
      </div> : <p className="text-xs text-mute">{d.alternative_status === "withheld_availability" || d.alternative_status === "availability_update_pending" ? "Alternate is being updated after a playing-status change." : "A second eligible scorer is not available yet."}</p>}
  </section>;
}

function PlayerComparison({ row }: { row: PropRow }) {
  const d = row.decision;
  if (!d || row.locked || !d.alternatives.length) return null;
  return <details className="text-sm"><summary className="cursor-pointer font-semibold text-gold">Compare other players</summary>
      <div className="overflow-x-auto"><table className="w-full text-left text-xs"><caption className="sr-only">Scoring chances and saved odds for other players</caption>
        <thead className="text-mute"><tr><th scope="col" className="py-2">Player</th><th scope="col">Chance to score</th><th scope="col">Saved odds</th><th scope="col">Football case</th></tr></thead>
        <tbody>{d.alternatives.map(p => <tr key={p.player_id} className="border-t border-paper/10"><th scope="row" className="py-2 pr-2 font-normal">{p.name}</th><td>{pct(p.probability, 0)}</td><td>{american(p.american)}</td><td>{(p.research_rank?.advantages ?? []).map(a => ({opponent_scoring_area_weakness:'Opponent weakness',quarterback_redzone_role:'QB red-zone connection',growing_role:'Growing role',concentrated_scoring_role:'Scoring-area role',game_script_role:'Game-script role'}[a] || a)).join('; ') || 'No distinct advantage established'}</td></tr>)}</tbody>
      </table></div>
      <p className="text-xs text-mute">Main and alternative scorers have separate hit records. Only a fresh qualifying price can count toward hypothetical betting returns.</p>
      </details>;
}

function DecisionResult({ result }: { result?: string }) {
  if (result !== "scored" && result !== "blank") return null;
  return <p className={`mt-1 text-xs font-semibold ${result === "scored" ? "text-green-300" : "text-red-300"}`}>{result === "scored" ? "✓ Touchdown" : "✕ No touchdown"}</p>;
}

function ScoringCase({ row }: { row: PropRow }) {
  const report = row.desk_analysis?.scouting_report;
  if (row.locked || !report) return null;
  return (
    <details className="mt-4 border-t border-paper/15 pt-3">
      <summary className="cursor-pointer text-sm font-semibold text-gold">See the detailed stats and sources</summary>
      <div className="mt-3 space-y-4 text-sm">
        {report.case ? <section className="space-y-3">
          <p className="font-semibold text-paper">{report.case.thesis}</p>
          <h3 className="font-semibold text-paper">The case for this player</h3>
          <ul className="list-disc space-y-2 pl-4 text-paper/80">{report.case.support.map((fact, i) => <li key={i}>{fact}</li>)}</ul>
          <h3 className="font-semibold text-amber-200">Model checks and limitations</h3>
          <ul className="list-disc space-y-2 pl-4 text-paper/75">{report.case.counter.map((fact, i) => <li key={i}>{fact}</li>)}</ul>
          <p className="rounded border border-paper/20 p-3">{report.case.verdict}</p>
          {report.case.paths.length ? <div className="overflow-x-auto"><table className="w-full text-left text-xs">
            <caption className="py-2 text-left font-semibold">Projected scoring opportunities — estimates, not guaranteed touches</caption>
            <thead><tr className="text-mute"><th scope="col">Path</th><th scope="col">Opportunities</th><th scope="col">Team share</th><th scope="col">Opponent conversion vs league</th></tr></thead>
            <tbody>{report.case.paths.map(path => <tr key={path.path} className="border-t border-paper/10">
              <th scope="row" className="py-2 pr-2 font-normal">{path.label}</th>
              <td>{path.projected_opportunities.toFixed(1)}</td><td>{pct(path.share, 0)}</td>
              <td>{path.opponent_rate_ratio.toFixed(2)}×<span className="block text-mute">{path.opponent_position_sample.toFixed(0)} prior opportunities</span></td>
            </tr>)}</tbody></table></div> : null}
        </section> : null}
        <p className="text-xs text-mute">{report.frozen_inputs_only ? "Explanation of saved inputs" : "Analysis captured with this prediction"}{report.information_cutoff ? ` · Information cutoff ${report.information_cutoff}` : " · Cutoff not recorded"}</p>
        {([
          ["Current role & scoring-area work", report.role],
          ["Offense against defense", report.matchup],
          ["Team scoring & game script", report.game_script],
          ["Availability & personnel checks", report.checks],
        ] as [string, string[]][]).map(([title, facts]) => facts.length ? <section key={title}>
          <h3 className="font-semibold text-paper">{title}</h3>
          <ul className="mt-2 list-disc space-y-2 pl-4 text-paper/75">{facts.map((fact, i) => <li key={i}>{fact}</li>)}</ul>
        </section> : null)}
        <section>
          <h3 className="font-semibold text-paper">Why this player over the alternatives?</h3>
          <p className="mt-1 text-xs text-mute">{report.comparison}</p>
          {report.alternatives.length ? <div className="mt-2 overflow-x-auto">
            <table className="w-full text-left text-xs">
              <caption className="sr-only">Other eligible scorers from the same prediction</caption>
              <thead className="text-mute"><tr><th scope="col" className="py-2">Player</th><th scope="col">Chance</th><th scope="col">Odds</th><th scope="col">Team points</th></tr></thead>
              <tbody>{report.alternatives.map(p => <tr key={p.player_id} className="border-t border-paper/10">
                <th scope="row" className="py-2 pr-2 font-normal">{p.name} · {p.team}<span className="mt-1 block text-[10px] text-mute">{p.role}</span>{p.policy_excluded ? <span className="mt-1 block text-[10px] text-amber-300">Excluded by team-total preference</span> : null}</th>
                <td>{pct(p.probability, 0)}</td><td>{american(p.price.american)}</td><td>{p.team_implied_points?.toFixed(1) ?? "Unknown"}</td>
              </tr>)}</tbody>
            </table>
          </div> : null}
        </section>
        <section>
          <h3 className="font-semibold text-paper">Scoring chance versus price</h3>
          <p className="mt-1 text-paper/75">Model fair odds: {american(report.price.fair_american)}. {report.price.break_even == null ? "No recorded player price for a value comparison." : `Recorded ${american(report.price.american)} needs ${pct(report.price.break_even, 0)} to break even; estimated model edge ${((report.price.estimated_edge ?? 0) * 100).toFixed(1)} percentage points.`}</p>
          <p className="mt-1 text-xs text-mute">{report.price.note}</p>
        </section>
        <section>
          <h3 className="font-semibold text-paper">What we still cannot verify</h3>
          <ul className="mt-2 list-disc space-y-1 pl-4 text-mute">{report.gaps.map((gap, i) => <li key={i}>{gap}</li>)}</ul>
        </section>
        <p className="text-xs text-mute">{report.model_note}</p>
        {report.sources.length ? <p className="text-xs text-mute">Sources: {report.sources.map((source, i) => <span key={i}>{i ? " · " : ""}{source.url ? <a className="underline" href={source.url} target="_blank" rel="noreferrer">{source.label}</a> : source.label}</span>)}</p> : null}
      </div>
    </details>
  );
}
