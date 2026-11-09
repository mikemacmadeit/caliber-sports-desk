import { ShadowLab } from "@/components/ShadowLab";
import { PageHeader } from "@/components/PageHeader";
import { LeagueBadge } from "@/components/StatusChips";
import { apiGet } from "@/lib/api";
import { pct } from "@/lib/format";

type Snap = {
  league_id: string;
  market_id: string;
  as_of: string;
  brier: number;
  log_loss: number;
  ece: number;
  skill_vs_novig: number | null;
  n: number;
  coverage: number;
  freshness_minutes: number;
  clv: number | null;
  notes: string;
};

export default async function DataQualityPage() {
  const data = await apiGet<{
    snapshots: Snap[];
    adapters: Record<string, Record<string, unknown>>;
    licenses: Record<string, { license_name: string; commercial_redisplay: string; notes: string }>;
    gate: string;
    disclaimer: string;
    intelligence?: {
      configured: boolean;
      runs: number;
      accepted: number;
      rejected: number;
      token_input: number;
      token_output: number;
      cost_usd: number;
      models: string[];
      official_probabilities: string;
      improvement?: {
        reviews: number;
        applied: number;
        origins: Record<string, number>;
        play_enabled: boolean;
        retrain: boolean;
        note?: string;
      };
    };
  }>("/v1/model-health");
  const coverage = await apiGet<{
    catalog: Array<{ asset: string; availability: string; provider: string; notes: string; leagues: string[] }>;
    counts: Record<string, number>;
    credentials: Record<string, boolean>;
    coverage: Array<{
      provider: string;
      dataset: string;
      league_id: string;
      season_year: number | null;
      row_count: number;
      status: string;
      notes: string;
    }>;
    unavailable_without_license: string[];
    note: string;
    quality?: {
      usage_pregame_player_rows: number;
      usage_pregame_team_rows: number;
      integrations: Array<{
        dataset: string;
        rows?: number;
        roster_rows?: number;
        depth_rows?: number;
        player_game_rows?: number;
        license?: string;
        analytics?: string[];
        missing?: string[];
      }>;
    };
  }>("/v1/coverage");

  return (
    <div className="space-y-10">
      <PageHeader kicker="Gates stay closed until the proof" title="Model health" lede={data.gate} />
      <ShadowLab />
      {data.intelligence ? (
        <div className="plate p-5">
          <h2 className="kicker mb-3">Research layer</h2>
          <p className="font-mono text-[11px] uppercase tracking-wider text-mute">
            Key {data.intelligence.configured ? "on server" : "missing"} · {data.intelligence.runs} runs ·{" "}
            {data.intelligence.accepted} accepted · {data.intelligence.rejected} rejected ·{" "}
            {data.intelligence.token_input + data.intelligence.token_output} tokens · $
            {data.intelligence.cost_usd.toFixed(4)} · probabilities {data.intelligence.official_probabilities}
          </p>
          {data.intelligence.improvement ? (
            <p className="mt-3 font-mono text-[11px] uppercase tracking-wider text-mute">
              Settlement reviews {data.intelligence.improvement.reviews} · applied{" "}
              {data.intelligence.improvement.applied} · retrain {data.intelligence.improvement.retrain ? "yes" : "no"} ·
              play {data.intelligence.improvement.play_enabled ? "on" : "off"}
              {Object.keys(data.intelligence.improvement.origins || {}).length
                ? ` · ${Object.entries(data.intelligence.improvement.origins)
                    .map(([k, v]) => `${k}:${v}`)
                    .join(" · ")}`
                : ""}
            </p>
          ) : null}
        </div>
      ) : null}
      <p className="font-mono text-[11px] uppercase tracking-[0.16em] text-gold">{coverage.note}</p>
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {Object.entries(coverage.counts).map(([k, v]) => (
          <div key={k} className="plate px-4 py-4">
            <div className="font-mono text-[10px] uppercase tracking-[0.2em] text-mute">{k.replaceAll("_", " ")}</div>
            <div className="mt-2 font-display text-5xl uppercase leading-none">{v}</div>
          </div>
        ))}
      </div>
      {coverage.quality?.integrations?.length ? (
        <div className="plate overflow-x-auto">
          <table className="w-full min-w-[960px] text-left text-sm">
            <thead className="font-mono text-[10px] uppercase tracking-[0.18em] text-mute">
              <tr className="border-b border-white/10 bg-black/30">
                <th className="px-4 py-3 font-medium">Dataset</th>
                <th className="px-4 py-3 font-medium">Rows</th>
                <th className="px-4 py-3 font-medium">Can calculate</th>
                <th className="px-4 py-3 font-medium">Missing</th>
              </tr>
            </thead>
            <tbody>
              {coverage.quality.integrations.map((item) => (
                <tr key={item.dataset} className="border-b border-white/5 align-top last:border-0">
                  <td className="px-4 py-3 font-mono text-xs">{item.dataset}</td>
                  <td className="px-4 py-3 font-mono">
                    {item.rows ?? item.roster_rows ?? 0}
                    {item.player_game_rows ? ` / ${item.player_game_rows} player-games` : ""}
                    {item.depth_rows ? ` / ${item.depth_rows} depth` : ""}
                  </td>
                  <td className="px-4 py-3 text-xs text-paper/80">{(item.analytics ?? []).join("; ")}</td>
                  <td className="px-4 py-3 text-xs text-mute">{(item.missing ?? []).join("; ")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      <div className="plate overflow-x-auto">
        <table className="w-full min-w-[960px] text-left text-sm">
          <thead className="font-mono text-[10px] uppercase tracking-[0.18em] text-mute">
            <tr className="border-b border-white/10 bg-black/30">
              <th className="px-4 py-3 font-medium">Asset</th>
              <th className="px-4 py-3 font-medium">Provider</th>
              <th className="px-4 py-3 font-medium">Availability</th>
              <th className="px-4 py-3 font-medium">Notes</th>
            </tr>
          </thead>
          <tbody>
            {coverage.catalog.map((a) => (
              <tr key={a.asset} className="border-b border-white/5 last:border-0">
                <td className="px-4 py-3 font-mono text-xs">{a.asset}</td>
                <td className="px-4 py-3">{a.provider}</td>
                <td className="px-4 py-3 font-mono text-xs uppercase tracking-wider text-gold">{a.availability}</td>
                <td className="px-4 py-3 text-xs text-mute">{a.notes}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="plate overflow-x-auto">
        <table className="w-full min-w-[960px] text-left text-sm">
          <thead className="font-mono text-[10px] uppercase tracking-[0.18em] text-mute">
            <tr className="border-b border-white/10 bg-black/30">
              <th className="px-4 py-3 font-medium">League</th>
              <th className="px-4 py-3 font-medium">Market</th>
              <th className="px-4 py-3 font-medium">n</th>
              <th className="px-4 py-3 font-medium">Brier</th>
              <th className="px-4 py-3 font-medium">Log loss</th>
              <th className="px-4 py-3 font-medium">ECE</th>
              <th className="px-4 py-3 font-medium">Skill vs no-vig</th>
              <th className="px-4 py-3 font-medium">Coverage</th>
              <th className="px-4 py-3 font-medium">CLV</th>
            </tr>
          </thead>
          <tbody>
            {data.snapshots.map((r) => (
              <tr key={`${r.league_id}-${r.market_id}`} className="border-b border-white/5 last:border-0">
                <td className="px-4 py-3">
                  <LeagueBadge league={r.league_id} />
                </td>
                <td className="px-4 py-3 font-mono text-xs uppercase tracking-wider">{r.market_id}</td>
                <td className="px-4 py-3 font-mono">{r.n}</td>
                <td className="px-4 py-3 font-mono">{r.brier.toFixed(3)}</td>
                <td className="px-4 py-3 font-mono">{r.log_loss.toFixed(3)}</td>
                <td className="px-4 py-3 font-mono">{r.ece.toFixed(3)}</td>
                <td className="px-4 py-3 font-mono">
                  {r.skill_vs_novig == null ? "unidentified" : r.skill_vs_novig.toFixed(3)}
                </td>
                <td className="px-4 py-3 font-mono">{pct(r.coverage)}</td>
                <td className="px-4 py-3 font-mono">{r.clv == null ? "—" : r.clv.toFixed(3)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="grid gap-4 md:grid-cols-2">
        {Object.entries(data.licenses).map(([k, v]) => (
          <div key={k} className="plate p-5">
            <div className="kicker">{k}</div>
            <div className="mt-3 font-display text-3xl uppercase leading-none">{v.license_name}</div>
            <div className="mt-3 font-mono text-[11px] uppercase tracking-wider text-mute">
              redisplay · {v.commercial_redisplay}
            </div>
            <p className="mt-3 text-sm leading-relaxed text-paper/70">{v.notes}</p>
          </div>
        ))}
      </div>
    </div>
  );
}
