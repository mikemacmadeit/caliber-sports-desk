"use client";

import { Fragment, useMemo, useState } from "react";
import { LeagueBadge } from "@/components/StatusChips";
import { kickoffMs, pct } from "@/lib/format";

export type ResearchWarning = { code: string; message: string };

export type ResearchPlayer = {
  player_id: string;
  player_name: string;
  team_name?: string | null;
  position_group?: string | null;
  availability_status?: string | null;
  sample_size?: number | null;
  probabilities: {
    player_td_v1?: number | null;
    m6_hist_gbm?: number | null;
    m5?: number | null;
    c_recent?: number | null;
    c_recent_raw_involved_conditional?: number | null;
    primary_research?: number | null;
    ensemble_reference_only?: number | null;
  };
  projection_scope?: string | null;
  game_level_supported?: boolean | null;
  projection_scope_evidence?: Record<string, unknown> | null;
  projection_scope_reasons?: string[] | null;
  model_source?: {
    primary?: string | null;
    primary_label?: string | null;
    legacy_m5_label?: string | null;
    locked_version?: string | null;
    pipeline_version?: string | null;
    prediction_timestamp?: string | null;
    prediction_source?: string | null;
    price_captured_at?: string | null;
    accuracy_unproven?: boolean | null;
  } | null;
  sportsbook?: {
    status?: string | null;
    book_price?: number | null;
    bookmaker?: string | null;
    captured_at?: string | null;
    price_freshness?: {
      status?: string | null;
      age_minutes?: number | null;
      is_current_opportunity?: boolean | null;
    } | null;
    note?: string | null;
  } | null;
  team_scoring?: {
    team_lambda_off_td?: number | null;
    rush_td_share?: number | null;
    rec_td_share?: number | null;
    lambda_player_td?: number | null;
  };
  key_features?: Record<string, number | null | undefined>;
  missing_information?: string[];
  research_warnings: ResearchWarning[];
  disagreement?: {
    abs_delta?: number | null;
    delta_gbm_minus_v1?: number | null;
    root_cause?: string | null;
    root_detail?: string | null;
    causes?: string[] | null;
  } | null;
  sparse_history?: {
    diagnosis?: string;
    hierarchical_fallback_tests?: Record<string, { p_hierarchical?: number; fallback_suggested?: boolean }>;
  } | null;
  recommendation_status?: string;
  disclaimer?: string;
};

export type ResearchGame = {
  game_id: string;
  league_id: string;
  kickoff_utc?: string;
  home?: string;
  away?: string;
  players: ResearchPlayer[];
};

export type ResearchFinderPayload = {
  as_of?: string;
  disclaimer?: string;
  play_enabled?: boolean;
  validated_play?: boolean;
  fbs_primary_model?: string;
  fbs_primary_label?: string;
  n_games?: number;
  n_disagreement_flags?: number;
  n_warning_instances?: number;
  games: ResearchGame[];
};

export function AnytimeTdResearchBoard({ payload }: { payload: ResearchFinderPayload }) {
  const [gameId, setGameId] = useState("all");
  const [onlyDisagree, setOnlyDisagree] = useState(false);
  const [onlyWarned, setOnlyWarned] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);

  const games = useMemo(
    () =>
      [...(payload.games || [])].sort(
        (a, b) => kickoffMs(a.kickoff_utc) - kickoffMs(b.kickoff_utc) || (a.away || "").localeCompare(b.away || ""),
      ),
    [payload.games],
  );
  const rows = useMemo(() => {
    const out: Array<{ game: ResearchGame; player: ResearchPlayer }> = [];
    for (const game of games) {
      if (gameId !== "all" && game.game_id !== gameId) continue;
      for (const player of game.players || []) {
        if (onlyDisagree && !player.disagreement) continue;
        if (onlyWarned && (player.research_warnings || []).every((w) => w.code === "research_only_not_a_bet")) {
          continue;
        }
        out.push({ game, player });
      }
    }
    return out;
  }, [games, gameId, onlyDisagree, onlyWarned]);

  return (
    <div className="space-y-6">
      <div className="plate space-y-3 px-5 py-4">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-gold">Research finder</p>
            <h2 className="mt-1 font-display text-3xl uppercase leading-none tracking-wide">
              {payload.fbs_primary_label || "Research estimates · warnings on"}
            </h2>
          </div>
          <div className="font-mono text-[11px] uppercase tracking-wider text-mute">
            {payload.n_disagreement_flags ?? 0} disagreements · {payload.n_warning_instances ?? 0} warnings · Play off
          </div>
        </div>
        <p className="max-w-3xl text-sm text-mute">
          {payload.disclaimer ||
            "Research forecasts only. FBS primary is C_recent (experimental) — accuracy unproven. Not a validated betting recommendation."}
        </p>
        <div className="flex flex-wrap gap-2">
          <FilterChip active={gameId === "all"} onClick={() => setGameId("all")}>
            All games
          </FilterChip>
          {games.map((game) => (
            <FilterChip
              key={game.game_id}
              active={gameId === game.game_id}
              onClick={() => setGameId(gameId === game.game_id ? "all" : game.game_id)}
            >
              {game.away} @ {game.home}
            </FilterChip>
          ))}
          <FilterChip active={onlyDisagree} onClick={() => setOnlyDisagree((v) => !v)}>
            ≥10pp gaps
          </FilterChip>
          <FilterChip active={onlyWarned} onClick={() => setOnlyWarned((v) => !v)}>
            Data warnings
          </FilterChip>
        </div>
      </div>

      <div className="plate overflow-x-auto">
        <table className="w-full min-w-[1100px] text-left text-sm">
          <thead className="font-mono text-[10px] uppercase tracking-[0.18em] text-mute">
            <tr className="border-b border-white/10 bg-black/30">
              <th className="px-4 py-3 font-medium">Player</th>
              <th className="px-4 py-3 font-medium">Team λ / Legacy</th>
              <th className="px-4 py-3 font-medium">Primary research</th>
              <th className="px-4 py-3 font-medium">Book</th>
              <th className="px-4 py-3 font-medium">Gap</th>
              <th className="px-4 py-3 font-medium">History</th>
              <th className="px-4 py-3 font-medium">Warnings</th>
              <th className="px-4 py-3 font-medium">Call</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(({ game, player }) => {
              const key = `${game.game_id}:${player.player_id}`;
              const open = openId === key;
              const pV1 = player.probabilities.player_td_v1;
              const pGbm = player.probabilities.m6_hist_gbm;
              const pM5 = player.probabilities.m5;
              const pPrimary = player.probabilities.primary_research;
              const pCRecent =
                pPrimary ??
                player.probabilities.c_recent ??
                null;
              const pCRecentRaw = player.probabilities.c_recent_raw_involved_conditional ?? null;
              const notGameLevel =
                game.league_id === "fbs" &&
                player.game_level_supported === false &&
                pCRecentRaw != null;
              const primaryLabel =
                player.model_source?.primary_label ||
                (game.league_id === "fbs" ? "C_recent — experimental research" : "primary");
              const gap =
                player.disagreement?.abs_delta ??
                (game.league_id === "fbs" &&
                (pCRecent ?? pCRecentRaw) != null &&
                pM5 != null
                  ? Math.abs((pCRecent ?? pCRecentRaw)! - pM5)
                  : pV1 != null && pGbm != null
                    ? Math.abs(pV1 - pGbm)
                    : null);
              const warnCodes = (player.research_warnings || [])
                .filter((w) => w.code !== "research_only_not_a_bet")
                .map((w) => w.code.replaceAll("_", " "));
              return (
                <Fragment key={key}>
                  <tr
                    className={`border-b border-white/5 ${gap != null && gap >= 0.1 ? "bg-heat/[0.04]" : "hover:bg-white/[0.03]"}`}
                  >
                    <td className="px-4 py-3">
                      <button type="button" className="text-left" onClick={() => setOpenId(open ? null : key)}>
                        <div className="flex items-center gap-2">
                          <LeagueBadge league={game.league_id} />
                          <span className="font-medium hover:text-gold">{player.player_name}</span>
                        </div>
                        <div className="mt-1 font-mono text-[10px] uppercase tracking-wider text-mute">
                          {player.position_group || "—"} · {player.team_name || "—"} · {game.away} @ {game.home}
                        </div>
                      </button>
                    </td>
                    <td className="px-4 py-3 font-mono">
                      {game.league_id === "nfl" ? (
                        <>
                          <span className="text-paper">{pct(pV1 ?? null)}</span>
                          <div className="text-[10px] uppercase tracking-wider text-mute">player td v1</div>
                        </>
                      ) : (
                        <>
                          <span className="text-mute">{pct(pM5 ?? null)}</span>
                          <div className="text-[10px] uppercase tracking-wider text-mute">
                            {player.model_source?.legacy_m5_label || "Legacy M5 comparison"}
                          </div>
                        </>
                      )}
                    </td>
                    <td className="px-4 py-3 font-mono">
                      {game.league_id === "nfl" ? (
                        <>
                          <span className="text-paper">{pct(pGbm ?? null)}</span>
                          <div className="text-[10px] uppercase tracking-wider text-mute">m6 histgbm</div>
                        </>
                      ) : notGameLevel ? (
                        <>
                          <span className="text-heat">Not a game-level projection</span>
                          <div className="text-[10px] uppercase tracking-wider text-mute">{primaryLabel}</div>
                          <div className="mt-0.5 text-[9px] normal-case tracking-normal text-mute">
                            raw involved-conditional {pct(pCRecentRaw)} (inspection only)
                          </div>
                          {player.model_source?.prediction_timestamp ? (
                            <div className="mt-0.5 text-[9px] normal-case tracking-normal text-mute">
                              pred {player.model_source.prediction_timestamp.slice(0, 19)}Z
                            </div>
                          ) : null}
                        </>
                      ) : pCRecent == null ? (
                        <>
                          <span className="text-heat">Research estimate unavailable</span>
                          <div className="text-[10px] uppercase tracking-wider text-mute">{primaryLabel}</div>
                        </>
                      ) : (
                        <>
                          <span className="text-paper">{pct(pCRecent)}</span>
                          <div className="text-[10px] uppercase tracking-wider text-mute">{primaryLabel}</div>
                          {player.model_source?.prediction_timestamp ? (
                            <div className="mt-0.5 text-[9px] normal-case tracking-normal text-mute">
                              pred {player.model_source.prediction_timestamp.slice(0, 19)}Z
                            </div>
                          ) : null}
                        </>
                      )}
                    </td>
                    <td className="px-4 py-3 font-mono">
                      {player.sportsbook?.status === "priced" && player.sportsbook.book_price != null ? (
                        <>
                          <span className="text-paper">
                            {player.sportsbook.book_price > 0 ? `+${player.sportsbook.book_price}` : player.sportsbook.book_price}
                          </span>
                          <div className="text-[10px] uppercase tracking-wider text-mute">
                            {(player.sportsbook.bookmaker || "book").slice(0, 18)} ·{" "}
                            {(player.sportsbook.price_freshness?.status || "unknown").replaceAll("_", " ")}
                          </div>
                        </>
                      ) : (
                        <>
                          <span className="text-mute">Unpriced</span>
                          <div className="text-[10px] uppercase tracking-wider text-mute">no quote in DB</div>
                        </>
                      )}
                    </td>
                    <td className="px-4 py-3 font-mono">
                      {gap == null ? (
                        <span className="text-mute">—</span>
                      ) : (
                        <span className={gap >= 0.1 ? "text-heat" : "text-mute"}>{(gap * 100).toFixed(1)} pp</span>
                      )}
                    </td>
                    <td className="px-4 py-3 font-mono text-mute">
                      n={player.sample_size ?? 0}
                      <div className="text-[10px] uppercase tracking-wider">
                        {(player.availability_status || "unknown").replaceAll("_", " ")}
                      </div>
                    </td>
                    <td className="px-4 py-3">
                      {warnCodes.length === 0 ? (
                        <span className="font-mono text-[10px] uppercase tracking-wider text-mute">research only</span>
                      ) : (
                        <div className="flex max-w-xs flex-wrap gap-1">
                          {warnCodes.slice(0, 3).map((code) => (
                            <span
                              key={code}
                              className="border border-heat/40 px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wider text-heat"
                            >
                              {code}
                            </span>
                          ))}
                          {warnCodes.length > 3 ? (
                            <span className="font-mono text-[10px] text-mute">+{warnCodes.length - 3}</span>
                          ) : null}
                        </div>
                      )}
                    </td>
                    <td className="px-4 py-3">
                      <span className="bg-ink-700 px-2 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-mute">
                        No bet
                      </span>
                    </td>
                  </tr>
                  {open ? (
                    <tr className="border-b border-white/10 bg-black/25">
                      <td colSpan={7} className="px-4 py-4">
                        <PlayerDetail player={player} />
                      </td>
                    </tr>
                  ) : null}
                </Fragment>
              );
            })}
          </tbody>
        </table>
        {rows.length === 0 ? <p className="px-5 py-8 text-sm text-mute">Nothing matches that research filter.</p> : null}
      </div>
    </div>
  );
}

function PlayerDetail({ player }: { player: ResearchPlayer }) {
  const warnings = player.research_warnings || [];
  const causes = player.disagreement?.causes || [];
  const feats = player.key_features || {};
  return (
    <div className="grid gap-4 lg:grid-cols-3">
      <div>
        <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-mute">Why the gap</p>
        <p className="mt-2 text-sm text-paper/90">
          {player.disagreement?.root_detail || "No ≥10pp disagreement on this row."}
        </p>
        {causes.length > 0 ? (
          <ul className="mt-3 space-y-1.5 text-[12px] text-mute">
            {causes.slice(0, 5).map((cause) => (
              <li key={cause}>· {cause}</li>
            ))}
          </ul>
        ) : null}
      </div>
      <div>
        <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-mute">Team scoring path</p>
        <dl className="mt-2 space-y-1 font-mono text-[12px] text-mute">
          <div>λ team off TD: {fmt(player.team_scoring?.team_lambda_off_td)}</div>
          <div>rush share: {fmt(player.team_scoring?.rush_td_share)}</div>
          <div>rec share: {fmt(player.team_scoring?.rec_td_share)}</div>
          <div>λ player: {fmt(player.team_scoring?.lambda_player_td)}</div>
          <div>prior TD rate: {fmt(feats.prior_td_rate)}</div>
          <div>roll3 TD: {fmt(feats.roll3_td_rate)}</div>
          <div>RZ targets: {fmt(feats.exp_rz_targets)}</div>
        </dl>
        {player.sparse_history ? (
          <p className="mt-3 text-[12px] text-heat">
            Sparse history: {(player.sparse_history.diagnosis || "").replaceAll("_", " ")}. Hierarchical fallback
            tested only — not applied.
          </p>
        ) : null}
      </div>
      <div>
        <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-mute">Research warnings</p>
        <ul className="mt-2 space-y-2 text-[12px] text-paper/85">
          {warnings.map((w) => (
            <li key={w.code} className="border-l border-heat/40 pl-2">
              <span className="font-mono text-[10px] uppercase tracking-wider text-heat">
                {w.code.replaceAll("_", " ")}
              </span>
              <div className="text-mute">{w.message}</div>
            </li>
          ))}
        </ul>
        {(player.missing_information || []).length > 0 ? (
          <p className="mt-3 font-mono text-[10px] uppercase tracking-wider text-mute">
            Missing: {(player.missing_information || []).join(" · ")}
          </p>
        ) : null}
        <p className="mt-3 text-[11px] text-mute">
          Ensemble shown only as reference when present (
          {pct(player.probabilities.ensemble_reference_only ?? null)}). Not used to hide disagreement.
        </p>
        {player.model_source ? (
          <dl className="mt-3 space-y-1 font-mono text-[11px] text-mute">
            <div>Model: {player.model_source.primary_label || player.model_source.primary || "—"}</div>
            <div>Locked: {player.model_source.locked_version || "—"}</div>
            <div>Pred time: {player.model_source.prediction_timestamp || "—"}</div>
            <div>Pred source: {(player.model_source.prediction_source || "—").replaceAll("_", " ")}</div>
            <div>
              Book:{" "}
              {player.sportsbook?.status === "priced" && player.sportsbook.book_price != null
                ? `${player.sportsbook.book_price > 0 ? "+" : ""}${player.sportsbook.book_price} ${player.sportsbook.bookmaker || ""} (${player.sportsbook.price_freshness?.status || "?"})`
                : player.sportsbook?.note || "unpriced — no quote in DB"}
            </div>
            {player.model_source.accuracy_unproven ? (
              <div className="text-heat">Accuracy superiority unproven · Validated Play off</div>
            ) : null}
          </dl>
        ) : null}
      </div>
    </div>
  );
}

function FilterChip({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`shrink-0 px-3 py-1.5 font-display text-lg uppercase tracking-wide ${
        active ? "bg-gold text-ink-950" : "border border-white/15 text-mute hover:text-paper"
      }`}
    >
      {children}
    </button>
  );
}

function fmt(v: number | null | undefined) {
  if (v == null || Number.isNaN(v)) return "—";
  return v.toFixed(3);
}
