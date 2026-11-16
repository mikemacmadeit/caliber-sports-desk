"use client";

import { useEffect, useState } from "react";
import { PropExplorer } from "@/components/PropExplorer";
import type { PropRow } from "@/lib/api";

export function PropBoard({ league, date, market }: { league: string; date: string; market: string }) {
  const path = `/api/backend/v1/props?market=${encodeURIComponent(market)}&league=${encodeURIComponent(league)}&date=${encodeURIComponent(date)}`;
  const [rows, setRows] = useState<PropRow[] | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    const ctrl = new AbortController();
    setRows(null);
    setError(false);
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      ctrl.abort();
    }, 20000);
    fetch(path, { cache: "no-store", signal: ctrl.signal })
      .then(async (res) => {
        if (!res.ok) throw new Error(String(res.status));
        const data = (await res.json()) as { rows?: PropRow[] };
        if (!ctrl.signal.aborted) setRows((data.rows || []).map(boardRow));
      })
      .catch(() => {
        if (timedOut || !ctrl.signal.aborted) setError(true);
      })
      .finally(() => clearTimeout(timer));
    return () => {
      clearTimeout(timer);
      ctrl.abort();
    };
  }, [path]);

  if (rows == null && !error) {
    return <p className="text-sm text-mute">Opening the priced board. The desk picks above are already the card.</p>;
  }
  if (error || !rows) {
    return (
      <div className="plate px-6 py-8">
        <p className="font-display text-4xl uppercase leading-none">Props unavailable</p>
        <p className="mt-3 max-w-xl text-sm text-mute">The desk could not load this market right now. Try again in a moment.</p>
      </div>
    );
  }
  if (!rows.length) {
    return (
      <div className="plate px-6 py-8">
        <p className="font-display text-4xl uppercase leading-none">No prices for this day</p>
        <p className="mt-3 max-w-xl text-sm text-mute">The games are on the board. This market has no locked number for them yet.</p>
      </div>
    );
  }
  return (
    <div className="space-y-8">
      <DayTally rows={rows} />
      <PropExplorer rows={rows} mode="flat" />
    </div>
  );
}

function boardRow(row: PropRow): PropRow {
  const forecast = row.forecast;
  return {
    outcome_id: row.outcome_id,
    game_id: row.game_id,
    league_id: row.league_id,
    market_id: row.market_id,
    player_id: row.player_id,
    player_name: row.player_name,
    position: row.position,
    team: row.team,
    team_mark: row.team_mark,
    opponent: row.opponent,
    away_abbr: row.away_abbr,
    home_abbr: row.home_abbr,
    away_mark: row.away_mark,
    home_mark: row.home_mark,
    kickoff_utc: row.kickoff_utc,
    side: row.side,
    line: row.line,
    pick: row.pick,
    model_lean: row.model_lean,
    bookmaker: row.bookmaker,
    book_price: row.book_price,
    source_timestamp: row.source_timestamp,
    price_freshness: row.price_freshness,
    result: row.result,
    actual: row.actual,
    projection: row.projection,
    projection_games: row.projection_games,
    projection_unit: row.projection_unit,
    data_tier: row.data_tier,
    research_signal: row.research_signal,
    forecast: {
      prediction_as_of: forecast?.prediction_as_of || "",
      model_version: forecast?.model_version || "",
      data_cutoff: forecast?.data_cutoff || "",
      markets: forecast?.markets || [],
      p_estimate: forecast?.p_estimate ?? 0,
      confidence_interval: forecast?.confidence_interval || [forecast?.p_estimate ?? 0, forecast?.p_estimate ?? 0],
      book_price: forecast?.book_price ?? null,
      source_timestamp: forecast?.source_timestamp ?? null,
      price_freshness: forecast?.price_freshness,
      availability_status: forecast?.availability_status,
      reason_codes: forecast?.reason_codes,
      disclaimer: forecast?.disclaimer || "",
      ev: forecast?.ev ?? null,
      ev_after_haircut: forecast?.ev_after_haircut ?? null,
      recommendation_status: forecast?.recommendation_status ?? null,
      data_tier: forecast?.data_tier,
      research_only: forecast?.research_only,
    },
  };
}

function DayTally({ rows }: { rows: PropRow[] }) {
  const sides = rows.filter(
    (row) => row.model_lean === true || (row.model_lean == null && row.side !== "under" && row.side !== "no"),
  );
  const wins = sides.filter((row) => row.result === "win").length;
  const losses = sides.filter((row) => row.result === "loss").length;
  const pushes = sides.filter((row) => row.result === "push").length;
  const pending = sides.length - wins - losses - pushes - sides.filter((row) => row.result === "void").length;
  const parts = [`${wins} wins`, `${losses} losses`, `${pending} pending`];
  if (pushes) parts.push(`${pushes} pushes`);
  return <p className="text-sm text-mute">Today · {parts.join(" · ")}</p>;
}
