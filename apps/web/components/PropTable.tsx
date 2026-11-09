import Link from "next/link";
import { RecChip, EvChip, Interval, LeagueBadge, TierChip } from "@/components/StatusChips";
import { TeamMark } from "@/components/TeamMark";
import type { PriceFreshness, PropRow } from "@/lib/api";
import { american, kickoff, marketLabel } from "@/lib/format";
import { presentTeam } from "@/lib/teams";

function NeedsPrice() {
  return <span className="font-mono text-[10px] uppercase tracking-wider text-mute">Needs price</span>;
}

function situationLine(codes: string[] | undefined) {
  if (!codes?.length) return null;
  const bits: string[] = [];
  if (codes.includes("RULED_OUT")) bits.push("Out");
  if (codes.includes("CURRENT_ROLE")) bits.push("Backup, this week's touches");
  if (codes.includes("MATCHUP")) bits.push("This defense");
  if (codes.includes("WEATHER")) bits.push("Wind or rain");
  return bits.length ? bits.join(" · ") : null;
}

export function PriceFreshnessBadge({ freshness }: { freshness?: PriceFreshness }) {
  if (!freshness || freshness.status === "missing") {
    return (
      <div className="mt-1 font-mono text-[10px] uppercase tracking-wider text-mute">No timestamp</div>
    );
  }
  const age =
    freshness.age_minutes == null
      ? "—"
      : freshness.age_minutes < 60
        ? `${Math.round(freshness.age_minutes)}m ago`
        : `${(freshness.age_minutes / 60).toFixed(1)}h ago`;
  const stale = freshness.status === "stale" || !freshness.is_current_opportunity;
  return (
    <div
      className={`mt-1 font-mono text-[10px] uppercase tracking-wider ${
        stale ? "text-heat" : "text-mute"
      }`}
      title={freshness.source_timestamp || freshness.captured_at || undefined}
    >
      {stale ? "Last posted" : "Fresh"} · {age}
    </div>
  );
}

export function PropTable({ rows, call }: { rows: PropRow[]; call?: "scorer" }) {
  if (!rows.length) {
    return (
      <div className="plate px-5 py-8 text-sm text-mute">
        Nothing on this filter. Widen the league or switch the market.
      </div>
    );
  }
  return (
    <div className="plate overflow-x-auto">
      <table className="w-full min-w-[980px] text-left text-sm">
        <thead className="font-mono text-[10px] uppercase tracking-[0.18em] text-mute">
          <tr className="border-b border-white/10 bg-black/30">
            <th className="px-4 py-3 font-medium">Player</th>
            <th className="px-4 py-3 font-medium">Pick</th>
            <th className="px-4 py-3 font-medium">Chance this side hits</th>
            <th className="px-4 py-3 font-medium">Price</th>
            <th className="px-4 py-3 font-medium">EV</th>
            <th className="px-4 py-3 font-medium">Haircut</th>
            <th className="px-4 py-3 font-medium">Call</th>
            <th className="px-4 py-3 font-medium">Status</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const hot = (r.forecast.ev_after_haircut ?? -1) >= 0.04;
            const club = presentTeam({ abbreviation: r.team, mark: r.team_mark });
            return (
              <tr
                key={r.outcome_id}
                className={`border-b border-white/5 last:border-0 ${
                  hot ? "bg-gold/[0.05]" : "hover:bg-white/[0.03]"
                }`}
              >
                <td className="px-4 py-3">
                  <Link href={`/games/${r.game_id}`} className="group">
                    <div className="flex items-center gap-2">
                      <TeamMark team={club} size="sm" />
                      <LeagueBadge league={r.league_id} />
                      <TierChip tier={r.data_tier || r.forecast.data_tier} />
                      <span className="font-medium group-hover:text-gold">{r.player_name}</span>
                    </div>
                    <div className="mt-1 font-mono text-[10px] uppercase tracking-wider text-mute">
                      {r.team} vs {r.opponent} · {kickoff(r.kickoff_utc)}
                    </div>
                    {r.research_signal ? (
                      <div className="mt-1 max-w-xs text-[11px] leading-snug text-gold">
                        {r.research_signal.market_id
                          ? `${r.research_signal.market_id} opportunity`
                          : `${r.research_signal.metric ?? "signal"} ${r.research_signal.direction ?? ""}`.trim()}{" "}
                        · {r.research_signal.validation_status}. Not a model input.
                      </div>
                    ) : null}
                  </Link>
                </td>
                <td className="px-4 py-3 text-paper/90">
                  <div
                    className={`font-display text-2xl uppercase leading-none tracking-wide ${
                      r.model_lean ? "text-gold" : "text-paper"
                    }`}
                  >
                    {r.pick || `${marketLabel(r.market_id)} ${r.side}${r.line != null ? ` ${r.line}` : ""}`}
                  </div>
                  {r.projection != null ? (
                    <div className="mt-1 font-mono text-[10px] uppercase tracking-wider text-mute">
                      Projects {r.projection} {r.projection_unit || ""}
                      {r.projection_games ? ` · last ${r.projection_games} played` : ""}
                      {r.model_lean === true ? " · model side" : r.model_lean === false ? " · other side" : ""}
                    </div>
                  ) : (
                    <div className="mt-1 font-mono text-[10px] uppercase tracking-wider text-mute">
                      {marketLabel(r.market_id)}
                    </div>
                  )}
                </td>
                <td className="px-4 py-3">
                  <Interval
                    p={r.forecast.p_estimate}
                    lo={r.forecast.confidence_interval[0]}
                    hi={r.forecast.confidence_interval[1]}
                  />
                </td>
                <td className="px-4 py-3 font-mono text-base text-paper">
                  {r.book_price == null ? (
                    <span className="text-[11px] uppercase tracking-wider text-mute">No book price</span>
                  ) : (
                    <>
                      {american(r.book_price)}
                      <div className="text-[10px] uppercase tracking-wider text-mute">
                        {r.bookmaker || "book"}
                      </div>
                      <PriceFreshnessBadge
                        freshness={r.price_freshness || r.forecast.price_freshness}
                      />
                    </>
                  )}
                </td>
                <td className="px-4 py-3">
                  {r.book_price == null ? <NeedsPrice /> : <EvChip ev={r.forecast.ev} />}
                </td>
                <td className="px-4 py-3">
                  {r.book_price == null ? <NeedsPrice /> : <EvChip ev={r.forecast.ev_after_haircut} />}
                </td>
                <td className="px-4 py-3">
                  <RecChip status={call ?? r.forecast.recommendation_status} />
                  {(r.forecast.research_only || r.data_tier === "research") && (
                    <div className="mt-1 font-mono text-[10px] uppercase tracking-wider text-gold">
                      Research only
                    </div>
                  )}
                </td>
                <td className="px-4 py-3">
                  <div
                    className="whitespace-nowrap font-mono text-[11px] uppercase tracking-wider"
                    title={r.forecast.reason_codes.join(" · ")}
                  >
                    {r.forecast.availability_status.replaceAll("_", " ")}
                  </div>
                  <div className="mt-1 font-mono text-[10px] uppercase tracking-wider text-mute">
                    {r.forecast.model_version}
                  </div>
                  {situationLine(r.forecast.reason_codes) ? (
                    <div className="mt-1 max-w-[16rem] font-mono text-[10px] uppercase tracking-wider text-paper/70">
                      {situationLine(r.forecast.reason_codes)}
                    </div>
                  ) : null}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
