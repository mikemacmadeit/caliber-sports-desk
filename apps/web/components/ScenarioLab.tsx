"use client";

import { useState } from "react";
import type { SlateGame } from "@/lib/api";
import { browserGet, browserPost } from "@/lib/browserApi";
import { mpsToMph, pct } from "@/lib/format";

type AnytimeTd = {
  player_id: string;
  player_name: string;
  probability: number;
};

type SimulationDetail = {
  matchup?: {
    away: string;
    home: string;
    away_name: string;
    home_name: string;
  };
  draw_count: number;
  scenario: string;
  research_only: boolean;
  overrides?: Record<string, unknown>;
  summary?: {
    anytime_td?: AnytimeTd[];
    score_quantiles?: { home?: number[]; away?: number[] };
    joint_td_pairs?: number;
  };
  convergence?: {
    max_td_mc_se?: number;
    target_se?: number;
  };
  note?: string;
  disclaimer?: string;
};

type LabResult = {
  createdNote: string;
  detail: SimulationDetail;
};

export function ScenarioLab({ games }: { games: SlateGame[] }) {
  const [gameId, setGameId] = useState(games[0]?.game_id ?? "");
  const [gameQuery, setGameQuery] = useState("");
  const [draws, setDraws] = useState(10000);
  const [windMph, setWindMph] = useState("");
  const [result, setResult] = useState<LabResult | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function run() {
    setBusy(true);
    setResult(null);
    setError("");
    const overrides: Record<string, unknown> = {};
    if (windMph.trim()) {
      const mph = Number(windMph);
      if (!Number.isFinite(mph)) {
        setError("Enter wind as a number in miles per hour, or leave it blank.");
        setBusy(false);
        return;
      }
      overrides.wind_mps = mph / 2.237;
    }
    try {
      const body = await browserPost<{ run_id: string; note?: string }>("/v1/simulations", {
        game_id: gameId,
        draw_count: draws,
        seed: 42,
        scenario_overrides: overrides,
      });
      const detail = await browserGet<SimulationDetail>(`/v1/simulations/${body.run_id}`);
      setResult({
        createdNote: typeof body.note === "string" ? body.note : "",
        detail,
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : "";
      setError(
        message.includes("Failed to fetch") || message.toLowerCase().includes("network")
          ? "Could not reach the simulation service. Try again in a moment."
          : message || "Something went wrong running the scenario.",
      );
    } finally {
      setBusy(false);
    }
  }

  const selected = games.find((game) => game.game_id === gameId);
  const note = selected?.research;
  const needle = gameQuery.trim().toLowerCase();
  const gameChoices = games.filter((game) => {
    if (!needle) return true;
    return `${game.away.abbreviation} ${game.home.abbreviation} ${game.away.name} ${game.home.name}`
      .toLowerCase()
      .includes(needle);
  });

  return (
    <div className="plate p-5 md:p-6">
      <p className="mb-4 font-mono text-[10px] uppercase tracking-[0.16em] text-mute">
        {note?.headline
          ? `Research signal stays speculative unless a sourced number validates it. ${note.headline}`
          : "A research scenario does not replace the official call."}
      </p>
      <div className="grid gap-4 md:grid-cols-3">
        <label className="font-mono text-[10px] uppercase tracking-[0.2em] text-mute">
          Game
          <input
            className="field-input mt-2"
            value={gameQuery}
            placeholder="Find a team"
            onChange={(e) => setGameQuery(e.target.value)}
          />
          <select
            className="field-input mt-2"
            value={gameId}
            onChange={(e) => setGameId(e.target.value)}
          >
            {gameChoices.map((g) => (
              <option key={g.game_id} value={g.game_id}>
                {g.away.abbreviation} @ {g.home.abbreviation} ({g.league_id.toUpperCase()})
              </option>
            ))}
          </select>
        </label>
        <label className="font-mono text-[10px] uppercase tracking-[0.2em] text-mute">
          How many simulations
          <input
            type="number"
            className="field-input mt-2 font-mono"
            value={draws}
            min={1000}
            max={50000}
            onChange={(e) => setDraws(Number(e.target.value))}
          />
        </label>
        <label className="font-mono text-[10px] uppercase tracking-[0.2em] text-mute">
          Wind override, mph
          <input
            className="field-input mt-2 font-mono"
            value={windMph}
            onChange={(e) => setWindMph(e.target.value)}
            placeholder="Blank keeps the official read"
          />
        </label>
      </div>
      <button onClick={run} disabled={busy || !gameId} className="btn-primary mt-5">
        {busy ? "Simulating…" : "Run the draws"}
      </button>
      {error && (
        <p className="mt-5 border border-heat/40 bg-ink-950 px-4 py-3 text-sm text-heat">{error}</p>
      )}
      {result && <ScenarioResults result={result} fallbackGame={selected} />}
    </div>
  );
}

function ScenarioResults({
  result,
  fallbackGame,
}: {
  result: LabResult;
  fallbackGame?: SlateGame;
}) {
  const { detail, createdNote } = result;
  const away =
    detail.matchup?.away ?? fallbackGame?.away.abbreviation ?? "Away";
  const home =
    detail.matchup?.home ?? fallbackGame?.home.abbreviation ?? "Home";
  const awayName = detail.matchup?.away_name ?? fallbackGame?.away.name ?? away;
  const homeName = detail.matchup?.home_name ?? fallbackGame?.home.name ?? home;
  const scores = detail.summary?.score_quantiles;
  const anytime = (detail.summary?.anytime_td ?? [])
    .filter((row) => row.probability > 0)
    .slice(0, 8);
  const windOverride =
    detail.overrides && typeof detail.overrides.wind_mps === "number"
      ? mpsToMph(detail.overrides.wind_mps as number)
      : null;
  const stable =
    detail.convergence?.max_td_mc_se != null &&
    detail.convergence?.target_se != null &&
    detail.convergence.max_td_mc_se <= detail.convergence.target_se;

  return (
    <div className="mt-6 space-y-5 border-t border-white/10 pt-5">
      <div>
        <p className="font-mono text-[10px] uppercase tracking-[0.2em] text-mute">What we ran</p>
        <h3 className="mt-1 font-display text-3xl uppercase leading-none text-paper">
          {away} @ {home}
        </h3>
        <p className="mt-2 max-w-2xl text-sm leading-relaxed text-mute">
          {detail.research_only
            ? `Research what-if for ${awayName} at ${homeName}. The official pick stays unchanged.`
            : `Official read for ${awayName} at ${homeName}.`}
          {windOverride ? ` Wind forced to about ${windOverride}.` : " Weather left at the official forecast."}{" "}
          Based on {detail.draw_count.toLocaleString()} simulated games.
          {stable ? " The run looks stable enough to read." : " Treat the edges as directional only."}
        </p>
        {(createdNote || detail.disclaimer) && (
          <p className="mt-2 text-sm leading-relaxed text-mute">
            {createdNote || detail.disclaimer}
          </p>
        )}
      </div>

      <div className="grid gap-3 md:grid-cols-3">
        <Stat
          label={`${home} typical score`}
          value={scoreBand(scores?.home)}
          hint="Middle half of simulated finals"
        />
        <Stat
          label={`${away} typical score`}
          value={scoreBand(scores?.away)}
          hint="Middle half of simulated finals"
        />
        <Stat
          label="Scenario type"
          value={detail.research_only ? "Research" : "Official"}
          hint={
            detail.research_only
              ? "What-if only — not a live recommendation"
              : "Uses the live weather and assumptions"
          }
        />
      </div>

      <div>
        <p className="font-mono text-[10px] uppercase tracking-[0.2em] text-mute">
          Anytime touchdown chances
        </p>
        {anytime.length === 0 ? (
          <p className="mt-3 text-sm text-mute">No player touchdown chances came back for this run.</p>
        ) : (
          <ul className="mt-3 divide-y divide-white/10 border border-white/10">
            {anytime.map((row) => (
              <li
                key={row.player_id}
                className="flex items-baseline justify-between gap-4 bg-ink-950 px-4 py-3"
              >
                <span className="text-sm text-paper">{row.player_name}</span>
                <span className="font-display text-2xl uppercase leading-none text-gold">
                  {pct(row.probability)}
                </span>
              </li>
            ))}
          </ul>
        )}
        <p className="mt-3 text-sm leading-relaxed text-mute">
          Chance each player scores at least one touchdown across the simulated games.
          {typeof detail.summary?.joint_td_pairs === "number" && detail.summary.joint_td_pairs > 0
            ? " Same-draw parlays are available if you want to price two scorers together."
            : ""}
        </p>
      </div>
    </div>
  );
}

function scoreBand(values?: number[]): string {
  if (!values || values.length < 5) return "—";
  const low = Math.round(values[1]);
  const high = Math.round(values[3]);
  if (low === high) return String(low);
  return `${low}–${high}`;
}

function Stat({ label, value, hint }: { label: string; value: string; hint: string }) {
  return (
    <div className="border border-white/10 bg-ink-950 px-4 py-3">
      <div className="font-mono text-[10px] uppercase tracking-[0.2em] text-mute">{label}</div>
      <div className="mt-1 font-display text-4xl uppercase leading-none text-gold">{value}</div>
      <div className="mt-2 text-xs leading-relaxed text-mute">{hint}</div>
    </div>
  );
}
