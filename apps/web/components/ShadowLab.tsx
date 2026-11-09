import { AnytimeTdScorecard, type AtdScoreRow } from "@/components/AnytimeTdScorecard";
import { PageHeader } from "@/components/PageHeader";
import { LeagueBadge, RecChip } from "@/components/StatusChips";
import { apiGet } from "@/lib/api";
import { evPct, marketLabel, pct } from "@/lib/format";

type DemoRow = {
  recommendation_id: string;
  created_at: string;
  status: string;
  ev: number | null;
  ev_after_haircut: number | null;
  reason_codes: string[];
  player: string | null;
  market: string | null;
  league_id: string | null;
  settlement: string;
  p: number | null;
  immutable: boolean;
  data_tier?: string;
};

type PicksBucket = {
  league_id: string;
  cohort_version: string;
  eligibility: string;
  n: number;
  cover: number;
  loss: number;
  push: number;
  void: number;
  cover_rate: number | null;
  flat_stake_roi: number | null;
  brier: number | null;
  included_in_clean_prospective_eval?: boolean;
};

type ShadowPayload = {
  play_enabled: boolean;
  atd_scorecard?: {
    rows: AtdScoreRow[];
    primary_rows?: AtdScoreRow[];
    note?: string;
  };
  picks_scorecard?: {
    buckets: Record<string, PicksBucket>;
    note?: string;
  };
  totals: {
    games_monitored: number;
    predictions: number;
    m3: number;
    m4: number;
    settled: number;
    pending: number;
    blocked: number;
    blend_v1_c_recent?: number;
    m5_c_recent?: number;
    player_td_v1?: number;
  };
  independence?: {
    raw_shadow_rows: number;
    unique_outcomes: number;
    unique_outcome_price_ticks: number;
    unique_player_game_markets: number;
    m3_m4_paired_ticks: number;
    independent_eval_units_approx: number;
    settled_unique_outcomes: number;
    note: string;
  };
  abstain_reason_counts: Record<string, number>;
  by_market: Array<{
    league_id: string;
    market_id: string;
    family: string;
    n: number;
    brier: number;
    log_loss: number;
    ece: number;
    flat_stake_roi: number | null;
  }>;
  m3_vs_m4: Array<Record<string, unknown>>;
  injury_coverage?: {
    coverage_counts: Record<string, number>;
    by_league: Record<string, Record<string, number>>;
    shadow_injury_abstentions: Record<string, number>;
    nflverse_latest_reported_at: string | null;
    notes: string[];
  };
  odds_freshness?: {
    price_rows: number;
    stale_rows: number;
    stale_share: number;
    age_minutes_p50: number | null;
    age_minutes_p90: number | null;
    age_minutes_max: number | null;
    stale_by_bookmaker: Record<string, number>;
    near_kickoff_stale_rows: number;
    max_price_age_minutes: number;
  };
  job_execution?: {
    recent_runs: number;
    failures: number;
    minutes_since_last_cycle: number | null;
    expected_jobs_missing_history: string[];
    by_job: Record<string, { runs: number; failed: number; last_status: string | null }>;
  };
  jobs: Array<{ job_name: string; status: string; started_at: string; error: string | null }>;
  rows: Array<{
    shadow_id: string;
    player: string | null;
    league_id: string;
    market_id: string;
    family: string;
    p: number;
    expected_value: number | null;
    market_implied_raw: number;
    market_implied_novig: number | null;
    settlement: string;
    abstain_reasons: string[];
    data_quality_status: string;
    forecast_as_of: string;
    flat_stake_return: number | null;
    closing_line_value: number | null;
  }>;
  disclaimer: string;
};

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="plate px-4 py-4">
      <div className="font-mono text-[10px] uppercase tracking-[0.22em] text-mute">{label}</div>
      <div className="mt-2 truncate font-display text-4xl uppercase leading-none tracking-wide text-paper">{value}</div>
    </div>
  );
}

export async function ShadowLab() {
  const shadow = await apiGet<ShadowPayload>("/v1/shadow/track-record", 60000).catch(() => null);
  const demo = await apiGet<{
    rows: DemoRow[];
    reviews?: Array<{ origin: string; suggested_experiment: string; applied: boolean }>;
    disclaimer: string;
  }>("/v1/recommendations/history").catch(() => ({ rows: [] as DemoRow[], disclaimer: "" }));
  const demoRows = (demo.rows || []).filter((row) => row.data_tier === "demo");
  const totals = shadow?.totals;
  const abstain = Object.entries(shadow?.abstain_reason_counts || {}).slice(0, 12);

  return (
    <div className="space-y-10">
      <PageHeader
        kicker="Model scores"
        title="How the versions graded"
        lede="Family scores, abstentions, and price freshness. The calls themselves live on the ledger, in the same words as the card."
      />

      <AnytimeTdScorecard
        title="Anytime TD score"
        rows={shadow?.atd_scorecard?.rows || []}
        note={shadow?.atd_scorecard?.note}
        showLedgerLink={false}
      />

      <section className="space-y-3">
        <div className="flex flex-wrap items-end justify-between gap-3 border-b border-white/10 pb-2">
          <h2 className="font-display text-4xl uppercase leading-none tracking-wide">Picks score</h2>
          <p className="max-w-xl text-sm text-mute">
            {shadow?.picks_scorecard?.note ||
              "ATS cover record by cohort. Clean prospective is the honest comparison bucket."}
          </p>
        </div>
        <div className="plate overflow-x-auto">
          <table className="w-full min-w-[820px] text-left text-sm">
            <thead className="font-mono text-[10px] uppercase tracking-[0.18em] text-mute">
              <tr className="border-b border-white/10 bg-black/30">
                <th className="px-4 py-3 font-medium">League</th>
                <th className="px-4 py-3 font-medium">Cohort</th>
                <th className="px-4 py-3 font-medium">Eligibility</th>
                <th className="px-4 py-3 font-medium">Cover-Loss</th>
                <th className="px-4 py-3 font-medium">Cover rate</th>
                <th className="px-4 py-3 font-medium">Brier</th>
                <th className="px-4 py-3 font-medium">Flat ROI</th>
              </tr>
            </thead>
            <tbody>
              {Object.keys(shadow?.picks_scorecard?.buckets || {}).length === 0 ? (
                <tr>
                  <td className="px-4 py-6 text-mute" colSpan={7}>
                    No graded ATS picks yet.
                  </td>
                </tr>
              ) : (
                Object.entries(shadow?.picks_scorecard?.buckets || {}).map(([key, row]) => (
                  <tr
                    key={key}
                    className={`border-b border-white/5 ${
                      row.included_in_clean_prospective_eval ? "bg-gold/5" : ""
                    }`}
                  >
                    <td className="px-4 py-3">
                      <LeagueBadge league={row.league_id} />
                    </td>
                    <td className="px-4 py-3 font-mono text-[11px] uppercase tracking-wider">
                      {row.cohort_version}
                    </td>
                    <td className="px-4 py-3 font-mono text-[11px] uppercase tracking-wider">
                      {row.eligibility}
                    </td>
                    <td className="px-4 py-3 font-mono">
                      {row.cover}-{row.loss}
                      {row.push ? ` · ${row.push} push` : ""}
                    </td>
                    <td className="px-4 py-3 font-mono">
                      {row.cover_rate == null ? "—" : pct(row.cover_rate)}
                    </td>
                    <td className="px-4 py-3 font-mono">
                      {row.brier == null ? "—" : row.brier.toFixed(4)}
                    </td>
                    <td className="px-4 py-3 font-mono">
                      {row.flat_stake_roi == null ? "—" : evPct(row.flat_stake_roi)}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </section>

      <section className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label="Games monitored" value={totals?.games_monitored ?? 0} />
        <Stat label="Shadow rows (raw)" value={totals?.predictions ?? 0} />
        <Stat label="Unique outcomes" value={shadow?.independence?.unique_outcomes ?? 0} />
        <Stat label="M3/M4 paired ticks" value={shadow?.independence?.m3_m4_paired_ticks ?? 0} />
        <Stat label="Settled" value={totals?.settled ?? 0} />
        <Stat label="Pending" value={totals?.pending ?? 0} />
        <Stat label="Blend frozen" value={totals?.blend_v1_c_recent ?? 0} />
        <Stat label="C_recent frozen" value={totals?.m5_c_recent ?? 0} />
        <Stat label="Blocked settlements" value={totals?.blocked ?? 0} />
        <Stat label="Play enabled" value={shadow?.play_enabled ? "yes" : "no"} />
      </section>
      {shadow?.independence?.note ? (
        <p className="text-sm text-mute">{shadow.independence.note}</p>
      ) : null}

      <section className="grid gap-3 lg:grid-cols-3">
        <div className="plate p-5">
          <h2 className="kicker mb-3">Injury coverage</h2>
          <ul className="space-y-2 text-sm">
            {Object.entries(shadow?.injury_coverage?.coverage_counts || {}).length === 0 ? (
              <li className="text-mute">No upcoming-game coverage snapshot yet.</li>
            ) : (
              Object.entries(shadow?.injury_coverage?.coverage_counts || {}).map(([code, count]) => (
                <li key={code} className="flex justify-between gap-3 border-b border-white/5 py-1">
                  <span className="font-mono text-[11px] uppercase tracking-wider text-mute">
                    {code.replaceAll("_", " ")}
                  </span>
                  <span className="font-mono text-paper">{count}</span>
                </li>
              ))
            )}
          </ul>
          <p className="mt-3 text-xs text-mute">
            {(shadow?.injury_coverage?.notes || [])[0] || "Missing reports are never healthy."}
          </p>
        </div>
        <div className="plate p-5">
          <h2 className="kicker mb-3">Odds freshness</h2>
          <div className="grid grid-cols-2 gap-3 text-sm">
            <div>
              <div className="kicker mb-1">Stale rows</div>
              <div className="font-mono text-xl text-paper">{shadow?.odds_freshness?.stale_rows ?? 0}</div>
            </div>
            <div>
              <div className="kicker mb-1">Near kickoff stale</div>
              <div className="font-mono text-xl text-paper">
                {shadow?.odds_freshness?.near_kickoff_stale_rows ?? 0}
              </div>
            </div>
            <div>
              <div className="kicker mb-1">Age p50 (min)</div>
              <div className="font-mono text-paper">
                {shadow?.odds_freshness?.age_minutes_p50 == null
                  ? "—"
                  : shadow.odds_freshness.age_minutes_p50.toFixed(0)}
              </div>
            </div>
            <div>
              <div className="kicker mb-1">Age max (min)</div>
              <div className="font-mono text-paper">
                {shadow?.odds_freshness?.age_minutes_max == null
                  ? "—"
                  : shadow.odds_freshness.age_minutes_max.toFixed(0)}
              </div>
            </div>
          </div>
          <ul className="mt-3 space-y-1 text-xs text-mute">
            {Object.entries(shadow?.odds_freshness?.stale_by_bookmaker || {})
              .slice(0, 4)
              .map(([book, count]) => (
                <li key={book} className="flex justify-between gap-2 font-mono">
                  <span>{book}</span>
                  <span>{count}</span>
                </li>
              ))}
          </ul>
        </div>
        <div className="plate p-5">
          <h2 className="kicker mb-3">Job execution</h2>
          <div className="mb-3 grid grid-cols-2 gap-3 text-sm">
            <div>
              <div className="kicker mb-1">Recent runs</div>
              <div className="font-mono text-xl text-paper">{shadow?.job_execution?.recent_runs ?? 0}</div>
            </div>
            <div>
              <div className="kicker mb-1">Failures</div>
              <div className="font-mono text-xl text-paper">{shadow?.job_execution?.failures ?? 0}</div>
            </div>
          </div>
          <p className="text-sm text-mute">
            Minutes since last cycle:{" "}
            <span className="font-mono text-paper">
              {shadow?.job_execution?.minutes_since_last_cycle == null
                ? "—"
                : shadow.job_execution.minutes_since_last_cycle.toFixed(0)}
            </span>
          </p>
          {(shadow?.job_execution?.expected_jobs_missing_history || []).length > 0 ? (
            <p className="mt-2 text-xs text-heat">
              Missing job history: {(shadow?.job_execution?.expected_jobs_missing_history || []).join(", ")}
            </p>
          ) : null}
        </div>
      </section>

      <section className="plate p-5">
        <h2 className="kicker mb-3">Why Caliber abstained</h2>
        {abstain.length === 0 ? (
          <p className="text-sm text-mute">No shadow abstention reasons yet. Run the shadow cycle before kickoff.</p>
        ) : (
          <ul className="grid gap-2 sm:grid-cols-2">
            {abstain.map(([reason, count]) => (
              <li key={reason} className="flex items-center justify-between gap-3 border-b border-white/5 py-2 text-sm">
                <span className="font-mono text-[11px] uppercase tracking-wider text-mute">{reason.replaceAll("_", " ")}</span>
                <span className="font-mono text-paper">{count}</span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="plate overflow-x-auto">
        <div className="border-b border-white/10 px-4 py-3">
          <h2 className="kicker">Market performance</h2>
          <p className="mt-1 text-sm text-mute">
            Settled shadow props only. Raw implied probability is shown separately from no-vig when both sides exist.
          </p>
        </div>
        <table className="w-full min-w-[900px] text-left text-sm">
          <thead className="font-mono text-[10px] uppercase tracking-[0.18em] text-mute">
            <tr className="border-b border-white/10 bg-black/30">
              <th className="px-4 py-3 font-medium">League</th>
              <th className="px-4 py-3 font-medium">Market</th>
              <th className="px-4 py-3 font-medium">Model</th>
              <th className="px-4 py-3 font-medium">n</th>
              <th className="px-4 py-3 font-medium">Brier</th>
              <th className="px-4 py-3 font-medium">Log loss</th>
              <th className="px-4 py-3 font-medium">ECE</th>
              <th className="px-4 py-3 font-medium">Flat ROI</th>
            </tr>
          </thead>
          <tbody>
            {(shadow?.by_market || []).length === 0 ? (
              <tr>
                <td className="px-4 py-6 text-mute" colSpan={8}>
                  No settled shadow props yet. That is expected until finals land.
                </td>
              </tr>
            ) : (
              (shadow?.by_market || []).map((row) => (
                <tr key={`${row.league_id}-${row.market_id}-${row.family}`} className="border-b border-white/5">
                  <td className="px-4 py-3">
                    <LeagueBadge league={row.league_id} />
                  </td>
                  <td className="px-4 py-3 font-mono text-[11px] uppercase tracking-wider">
                    {marketLabel(row.market_id)}
                  </td>
                  <td className="px-4 py-3 font-mono uppercase">{row.family}</td>
                  <td className="px-4 py-3 font-mono">{row.n}</td>
                  <td className="px-4 py-3 font-mono">{row.brier.toFixed(4)}</td>
                  <td className="px-4 py-3 font-mono">{row.log_loss.toFixed(4)}</td>
                  <td className="px-4 py-3 font-mono">{row.ece.toFixed(4)}</td>
                  <td className="px-4 py-3 font-mono">
                    {row.flat_stake_roi == null ? "—" : evPct(row.flat_stake_roi)}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </section>

      <section className="plate overflow-x-auto">
        <div className="border-b border-white/10 px-4 py-3">
          <h2 className="kicker">Shadow predictions</h2>
        </div>
        <table className="w-full min-w-[1100px] text-left text-sm">
          <thead className="font-mono text-[10px] uppercase tracking-[0.18em] text-mute">
            <tr className="border-b border-white/10 bg-black/30">
              <th className="px-4 py-3 font-medium">When</th>
              <th className="px-4 py-3 font-medium">Player</th>
              <th className="px-4 py-3 font-medium">Model</th>
              <th className="px-4 py-3 font-medium">p</th>
              <th className="px-4 py-3 font-medium">Raw implied</th>
              <th className="px-4 py-3 font-medium">No-vig</th>
              <th className="px-4 py-3 font-medium">EV</th>
              <th className="px-4 py-3 font-medium">Quality</th>
              <th className="px-4 py-3 font-medium">Settlement</th>
            </tr>
          </thead>
          <tbody>
            {(shadow?.rows || []).length === 0 ? (
              <tr>
                <td className="px-4 py-6 text-mute" colSpan={9}>
                  No prospective shadow rows yet.
                </td>
              </tr>
            ) : (
              (shadow?.rows || []).slice(0, 100).map((row) => (
                <tr key={row.shadow_id} className="border-b border-white/5 last:border-0 hover:bg-white/[0.03]">
                  <td className="px-4 py-3 font-mono text-[11px] uppercase tracking-wider text-mute">
                    {new Date(row.forecast_as_of).toLocaleString("en-US", {
                      month: "short",
                      day: "numeric",
                      hour: "numeric",
                      minute: "2-digit",
                    })}
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-2">
                      <LeagueBadge league={row.league_id} />
                      <span className="font-medium">{row.player || "—"}</span>
                    </div>
                    <div className="mt-1 font-mono text-[10px] uppercase tracking-wider text-mute">
                      {marketLabel(row.market_id)}
                    </div>
                    <div className="mt-1 max-w-[280px] truncate font-mono text-[10px] text-mute">
                      {(row.abstain_reasons || []).slice(0, 3).join(" · ")}
                    </div>
                  </td>
                  <td className="px-4 py-3 font-mono uppercase">{row.family}</td>
                  <td className="px-4 py-3 font-mono">{pct(row.p)}</td>
                  <td className="px-4 py-3 font-mono">{pct(row.market_implied_raw)}</td>
                  <td className="px-4 py-3 font-mono">
                    {row.market_implied_novig == null ? "unidentified" : pct(row.market_implied_novig)}
                  </td>
                  <td className={`px-4 py-3 font-mono ${(row.expected_value ?? 0) < 0 ? "text-heat" : "text-gold"}`}>
                    {evPct(row.expected_value)}
                  </td>
                  <td className="px-4 py-3 font-mono text-[11px] uppercase tracking-wider">{row.data_quality_status}</td>
                  <td className="px-4 py-3 font-mono text-[11px] uppercase tracking-wider">
                    {row.settlement}
                    {row.closing_line_value != null ? ` · CLV ${evPct(row.closing_line_value)}` : ""}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </section>

      <section className="plate p-5">
        <h2 className="kicker mb-3">Automation</h2>
        {(shadow?.jobs || []).length === 0 ? (
          <p className="text-sm text-mute">No shadow jobs recorded yet.</p>
        ) : (
          <ul className="space-y-2 text-sm">
            {(shadow?.jobs || []).slice(0, 12).map((job) => (
              <li key={`${job.job_name}-${job.started_at}`} className="flex flex-wrap items-center gap-3">
                <span className="font-mono text-[11px] uppercase tracking-wider text-mute">
                  {new Date(job.started_at).toLocaleString()}
                </span>
                <span className="font-medium">{job.job_name}</span>
                <span className="font-mono uppercase">{job.status}</span>
                {job.error ? <span className="text-heat">{job.error}</span> : null}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="space-y-4">
        <PageHeader
          kicker="Demonstration only"
          title="Legacy demo ledger"
          lede="These rows are not shadow market results and are not used for model promotion."
        />
        <div className="plate overflow-x-auto">
          <table className="w-full min-w-[900px] text-left text-sm">
            <thead className="font-mono text-[10px] uppercase tracking-[0.18em] text-mute">
              <tr className="border-b border-white/10 bg-black/30">
                <th className="px-4 py-3 font-medium">When</th>
                <th className="px-4 py-3 font-medium">Player</th>
                <th className="px-4 py-3 font-medium">p</th>
                <th className="px-4 py-3 font-medium">EV</th>
                <th className="px-4 py-3 font-medium">Call</th>
                <th className="px-4 py-3 font-medium">Settlement</th>
              </tr>
            </thead>
            <tbody>
              {demoRows.map((r) => (
                <tr key={r.recommendation_id} className="border-b border-white/5 last:border-0 hover:bg-white/[0.03]">
                  <td className="px-4 py-3 font-mono text-[11px] uppercase tracking-wider text-mute">
                    {new Date(r.created_at).toLocaleString("en-US", {
                      month: "short",
                      day: "numeric",
                      hour: "numeric",
                      minute: "2-digit",
                    })}
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-2">
                      {r.league_id && <LeagueBadge league={r.league_id} />}
                      <span className="font-medium">{r.player}</span>
                    </div>
                    <div className="mt-1 font-mono text-[10px] uppercase tracking-wider text-mute">
                      {marketLabel((r.market || "").toLowerCase())}
                    </div>
                  </td>
                  <td className="px-4 py-3 font-mono">{pct(r.p)}</td>
                  <td
                    className={`px-4 py-3 font-mono ${(r.ev_after_haircut ?? r.ev ?? 0) < 0 ? "text-heat" : "text-gold"}`}
                  >
                    {evPct(r.ev_after_haircut ?? r.ev)}
                  </td>
                  <td className="px-4 py-3">
                    <RecChip status={r.status} />
                  </td>
                  <td className="px-4 py-3 font-mono text-[11px] uppercase tracking-wider">{r.settlement}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
