"use client";

import { useMemo, useState } from "react";
import type { PropRow, SlateGame } from "@/lib/api";
import { browserPost } from "@/lib/browserApi";
import { evPct, pct } from "@/lib/format";

export function ParlayForm({ games, props }: { games: SlateGame[]; props: PropRow[] }) {
  const [gameId, setGameId] = useState(games[0]?.game_id ?? "");
  const [gameQuery, setGameQuery] = useState("");
  const [legA, setLegA] = useState("");
  const [legB, setLegB] = useState("");
  const [american, setAmerican] = useState("260");
  const [out, setOut] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const candidates = useMemo(
    () => props.filter((p) => p.game_id === gameId && p.player_id),
    [props, gameId],
  );
  const gameChoices = useMemo(() => {
    const needle = gameQuery.trim().toLowerCase();
    return games.filter((game) => {
      if (!needle) return true;
      return `${game.away.abbreviation} ${game.home.abbreviation} ${game.away.name} ${game.home.name}`
        .toLowerCase()
        .includes(needle);
    });
  }, [games, gameQuery]);

  async function evaluate() {
    if (!legA || !legB) return;
    setBusy(true);
    setError("");
    setOut(null);
    try {
      const payload = await browserPost<Record<string, unknown>>("/v1/parlays/evaluate", {
        game_id: gameId,
        book_american: Number(american),
        legs: [
          { player_id: legA, market_id: "anytime_td", side: "yes" },
          { player_id: legB, market_id: "anytime_td", side: "yes" },
        ],
      });
      setOut(payload);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not evaluate the parlay.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="plate space-y-5 p-5 md:p-6">
      <div className="grid gap-4 md:grid-cols-4">
        <Field label="Game">
          <input
            className="field-input mb-2"
            value={gameQuery}
            placeholder="Find a team"
            onChange={(e) => setGameQuery(e.target.value)}
          />
          <select
            className="field-input"
            value={gameId}
            onChange={(e) => {
              setGameId(e.target.value);
              setLegA("");
              setLegB("");
            }}
          >
            {gameChoices.map((g) => (
              <option key={g.game_id} value={g.game_id}>
                {g.away.abbreviation} @ {g.home.abbreviation}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Leg A">
          <select className="field-input" value={legA} onChange={(e) => setLegA(e.target.value)}>
            <option value="">Select</option>
            {candidates.map((c) => (
              <option key={c.player_id} value={c.player_id ?? ""}>
                {c.player_name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Leg B">
          <select className="field-input" value={legB} onChange={(e) => setLegB(e.target.value)}>
            <option value="">Select</option>
            {candidates.map((c) => (
              <option key={c.player_id} value={c.player_id ?? ""}>
                {c.player_name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Offered price">
          <input className="field-input font-mono" value={american} onChange={(e) => setAmerican(e.target.value)} />
        </Field>
      </div>
      <button onClick={evaluate} disabled={busy || !legA || !legB || legA === legB} className="btn-primary">
        {busy ? "Running…" : "Price the ticket"}
      </button>
      {error && (
        <p className="border border-heat/40 bg-ink-950 px-4 py-3 text-sm text-heat">{error}</p>
      )}
      {out && (
        <div className="grid gap-3 md:grid-cols-3">
          <Stat
            label="Joint probability"
            value={
              out.joint_available === false || out.joint_probability == null
                ? "Unavailable"
                : pct(out.joint_probability as number)
            }
          />
          <Stat label="Independent product" value={pct(out.naive_independent as number)} />
          <Stat
            label="Edge vs quote"
            value={out.joint_available === false || out.ev == null ? "—" : evPct(out.ev as number)}
          />
          <p className="font-mono text-[11px] uppercase leading-relaxed tracking-wider text-mute md:col-span-3">
            {String(out.method).replaceAll("_", " ")}
            {out.warning ? ` · ${String(out.warning)}` : ""}
            {out.joint_available === false
              ? " · Independent product is reference only — not a correlated joint."
              : ""}
          </p>
        </div>
      )}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="font-mono text-[10px] uppercase tracking-[0.2em] text-mute">
      {label}
      <div className="mt-2">{children}</div>
    </label>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="border border-white/10 bg-ink-950 px-4 py-3">
      <div className="font-mono text-[10px] uppercase tracking-[0.2em] text-mute">{label}</div>
      <div className="mt-1 font-display text-4xl uppercase leading-none text-gold">{value}</div>
    </div>
  );
}
