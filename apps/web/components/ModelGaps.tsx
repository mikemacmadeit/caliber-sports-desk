import type { PropRow } from "@/lib/api";
import { american, marketLabel, pct } from "@/lib/format";

type Gap = {
  outcome_id: string;
  model_p: number;
  market_implied: number;
  delta: number;
};

type Row = Gap & {
  player_name: string;
  market_id: string;
  side: string;
  line: number | null;
  book_price: number | null;
  bookmaker: string | null;
};

export function ModelGaps({
  gaps,
  props,
  limit = 12,
}: {
  gaps: Gap[];
  props: PropRow[];
  limit?: number;
}) {
  if (!gaps.length) {
    return (
      <p className="text-sm leading-relaxed text-mute">
        Model and market are within 5 points on every priced outcome for this game.
      </p>
    );
  }

  const byOutcome = new Map(props.map((p) => [p.outcome_id, p]));
  const rows: Row[] = gaps
    .map((g) => {
      const prop = byOutcome.get(g.outcome_id);
      return {
        ...g,
        player_name: prop?.player_name || "Unknown player",
        market_id: prop?.market_id || "market",
        side: prop?.side || "",
        line: prop?.line ?? null,
        book_price: prop?.book_price ?? null,
        bookmaker: prop?.bookmaker ?? null,
      };
    })
    .sort((a, b) => Math.abs(b.delta) - Math.abs(a.delta))
    .slice(0, limit);

  const modelHigh = rows.filter((r) => r.delta > 0).length;
  const marketHigh = rows.length - modelHigh;

  return (
    <div className="space-y-5">
      <p className="text-sm leading-relaxed text-paper/75">
        Biggest gaps between our probability and the no-vig market price. Gold means the model is higher;
        heat means the market is higher.
      </p>
      <div className="flex flex-wrap gap-3 font-mono text-[10px] uppercase tracking-[0.18em] text-mute">
        <span>
          Showing {rows.length} of {gaps.length} gaps ≥5 pts
        </span>
        <span className="text-gold">Model higher · {modelHigh}</span>
        <span className="text-heat">Market higher · {marketHigh}</span>
      </div>
      <ul className="divide-y divide-white/5">
        {rows.map((row) => (
          <li key={row.outcome_id} className="grid gap-3 py-4 sm:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)_auto] sm:items-center">
            <div className="min-w-0">
              <div className="truncate font-medium text-paper">{row.player_name}</div>
              <div className="mt-1 font-mono text-[10px] uppercase tracking-wider text-mute">
                {marketLabel(row.market_id)}
                {row.side ? ` · ${row.side}` : ""}
                {row.line != null ? ` ${row.line}` : ""}
                {row.book_price != null ? ` · ${american(row.book_price)}` : ""}
              </div>
            </div>
            <GapBar model={row.model_p} market={row.market_implied} />
            <div className="text-right">
              <div
                className={`font-display text-3xl uppercase leading-none tracking-tight ${
                  row.delta < 0 ? "text-heat" : "text-gold"
                }`}
              >
                {row.delta >= 0 ? "+" : ""}
                {pct(row.delta)}
              </div>
              <div className="mt-1 font-mono text-[10px] uppercase tracking-wider text-mute">gap</div>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

function GapBar({ model, market }: { model: number; market: number }) {
  const modelPct = Math.max(0, Math.min(100, model * 100));
  const marketPct = Math.max(0, Math.min(100, market * 100));
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between font-mono text-[10px] uppercase tracking-wider">
        <span className="text-gold">Model {pct(model)}</span>
        <span className="text-mute">Market {pct(market)}</span>
      </div>
      <div className="relative h-2 overflow-hidden bg-white/5">
        <div className="absolute inset-y-0 left-0 bg-gold/70" style={{ width: `${modelPct}%` }} />
        <div
          className="absolute top-1/2 h-3 w-0.5 -translate-y-1/2 bg-paper"
          style={{ left: `${marketPct}%` }}
          title={`Market ${pct(market)}`}
        />
      </div>
    </div>
  );
}
