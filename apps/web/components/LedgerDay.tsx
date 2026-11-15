"use client";

import { useMemo, useState } from "react";

export type LedgerRow = {
  pick_id: string;
  game_id?: string | null;
  market_id: string;
  league_id: string;
  sentence: string;
  detail?: string | null;
  result: string;
  kickoff_utc?: string | null;
  units?: number | null;
};

const GROUPS: [string, string][] = [
  ["point_spread", "Spreads"],
  ["anytime_td", "Anytime TD"],
  ["receptions", "Receptions"],
  ["rushing_yards", "Rushing yards"],
  ["receiving_yards", "Receiving yards"],
];

const FILTERS = [
  ["all", "All"],
  ["win", "Wins"],
  ["loss", "Losses"],
  ["pending", "Open"],
] as const;

type Filter = (typeof FILTERS)[number][0];

export function LedgerDay({
  title,
  rows,
}: {
  title: string;
  rows: LedgerRow[];
}) {
  const [filter, setFilter] = useState<Filter>("all");
  const [market, setMarket] = useState("all");
  const totals = useMemo(() => score(rows), [rows]);
  const markets = useMemo(
    () => GROUPS.map(([id, label]) => ({ id, label, ...score(rows.filter((row) => row.market_id === id)) })).filter((item) => item.count > 0),
    [rows],
  );
  const visible = rows.filter((row) => (market === "all" || row.market_id === market) && (filter === "all" || row.result === filter));

  if (!rows.length) {
    return (
      <section className="space-y-3" aria-label={`${title} results`}>
        <h2 className="font-display text-4xl uppercase leading-none tracking-wide">{title}</h2>
        <p className="text-sm text-mute">The desk did not name a pick for this day.</p>
      </section>
    );
  }

  return (
    <section className="space-y-6" aria-label={`${title} results`}>
      <div className="flex flex-wrap items-end justify-between gap-3">
        <h2 className="font-display text-4xl uppercase leading-none tracking-wide">{title}</h2>
        <p className="font-mono text-xs uppercase tracking-[0.16em] text-mute">{rows.length} picks</p>
      </div>

      <div className="grid gap-3 sm:grid-cols-4">
        <Tile label="Record" value={`${totals.win}–${totals.loss}`} hint={totals.pending ? `${totals.pending} still open` : totals.push ? `${totals.push} pushes` : "Graded"} />
        <Tile label="Hit rate" value={totals.hit == null ? "—" : percent(totals.hit)} hint={`${totals.graded} graded`} />
        <Tile label="Units" value={signed(totals.units)} hint={totals.priced ? `${totals.priced} priced picks` : "No prices graded"} tone={totals.units > 0 ? "win" : totals.units < 0 ? "loss" : undefined} />
        <Tile label="Still open" value={String(totals.pending)} hint={totals.push ? `${totals.push} pushes` : "Waiting on a stat"} />
      </div>

      <div className="plate overflow-x-auto">
        <table className="w-full min-w-[36rem] text-left">
          <caption className="sr-only">Totals by market</caption>
          <thead>
            <tr className="font-mono text-[10px] uppercase tracking-[0.16em] text-mute">
              <th className="px-4 py-3 font-normal">Market</th>
              <th className="px-4 py-3 font-normal">Record</th>
              <th className="px-4 py-3 font-normal">Hit rate</th>
              <th className="px-4 py-3 text-right font-normal">Units</th>
            </tr>
          </thead>
          <tbody>
            <TotalRow label="Whole day" tally={totals} strong />
            {markets.map((item) => (
              <TotalRow key={item.id} label={item.label} tally={item} />
            ))}
          </tbody>
        </table>
      </div>

      <div className="flex flex-wrap gap-2" aria-label="Show">
        {FILTERS.map(([value, label]) => {
          const count = value === "all" ? rows.length : rows.filter((row) => row.result === value).length;
          return (
            <button key={value} type="button" aria-pressed={filter === value} onClick={() => setFilter(value)} className={chip(filter === value)}>
              {label} {count}
            </button>
          );
        })}
        <span className="mx-1 hidden w-px bg-white/10 sm:block" />
        <button type="button" aria-pressed={market === "all"} onClick={() => setMarket("all")} className={chip(market === "all")}>
          Every market
        </button>
        {markets.map((item) => (
          <button key={item.id} type="button" aria-pressed={market === item.id} onClick={() => setMarket(item.id)} className={chip(market === item.id)}>
            {item.label}
          </button>
        ))}
      </div>

      {GROUPS.map(([id, label]) => {
        const group = visible.filter((row) => row.market_id === id);
        if (!group.length) return null;
        const full = score(rows.filter((row) => row.market_id === id));
        return (
          <div key={id} className="space-y-2">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <h3 className="font-display text-2xl uppercase leading-none tracking-wide">{label}</h3>
              <p className="font-mono text-xs uppercase tracking-[0.14em] text-mute">
                {full.win}–{full.loss}
                {full.hit == null ? "" : ` · ${percent(full.hit)}`}
                {full.priced ? ` · ${signed(full.units)}` : ""}
              </p>
            </div>
            <ul className="divide-y divide-white/10 border-y border-white/10">
              {group.map((row) => (
                <ResultLine key={row.pick_id} row={row} />
              ))}
            </ul>
          </div>
        );
      })}
      {visible.length === 0 ? <p className="text-sm text-mute">Nothing in this filter.</p> : null}
    </section>
  );
}

function TotalRow({ label, tally, strong = false }: { label: string; tally: Tally; strong?: boolean }) {
  return (
    <tr className={`border-t border-white/10 ${strong ? "bg-white/[0.03]" : ""}`}>
      <th className="px-4 py-3 text-left font-normal">
        <span className={strong ? "font-display text-xl uppercase tracking-wide" : ""}>{label}</span>
        <span className="mt-2 flex h-1.5 overflow-hidden rounded-full bg-white/10">
          <span className="bg-green-500" style={{ width: share(tally.win, tally.count) }} />
          <span className="bg-red-500" style={{ width: share(tally.loss, tally.count) }} />
        </span>
      </th>
      <td className="px-4 py-3 font-mono text-sm">
        <span className="text-green-300">{tally.win}</span>
        <span className="text-mute">–</span>
        <span className="text-red-300">{tally.loss}</span>
        {tally.pending ? <span className="text-mute"> · {tally.pending} open</span> : null}
      </td>
      <td className="px-4 py-3 font-mono text-sm">{tally.hit == null ? "—" : percent(tally.hit)}</td>
      <td className={`px-4 py-3 text-right font-mono text-sm ${toneClass(tally.priced ? tally.units : 0)}`}>
        {tally.priced ? signed(tally.units) : "—"}
      </td>
    </tr>
  );
}

function ResultLine({ row }: { row: LedgerRow }) {
  const won = row.result === "win";
  const lost = row.result === "loss";
  const label = won ? "Win" : lost ? "Loss" : row.result === "push" ? "Push" : "Open";
  return (
    <li className="grid grid-cols-[auto_1fr_auto] items-center gap-3 py-2.5">
      <span aria-label={label} className={`flex h-6 w-6 items-center justify-center rounded-full text-sm font-bold ${won ? "bg-green-500 text-black" : lost ? "bg-red-500 text-white" : "bg-white/10 text-mute"}`}>
        {won ? "✓" : lost ? "✕" : "–"}
      </span>
      <span className="min-w-0">
        <span className="block truncate text-sm text-paper">{row.sentence}</span>
        {row.detail ? <span className="mt-0.5 block truncate text-xs text-mute">{row.detail}</span> : null}
      </span>
      <span className={`font-mono text-xs ${toneClass(row.units)}`}>{row.units == null ? "" : signed(row.units)}</span>
    </li>
  );
}

function Tile({ label, value, hint, tone }: { label: string; value: string; hint: string; tone?: "win" | "loss" }) {
  return (
    <div className="plate px-4 py-4">
      <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-mute">{label}</p>
      <p className={`mt-2 font-display text-4xl uppercase leading-none tracking-wide ${tone === "win" ? "text-green-300" : tone === "loss" ? "text-red-300" : "text-paper"}`}>{value}</p>
      <p className="mt-2 text-xs text-mute">{hint}</p>
    </div>
  );
}

type Tally = { count: number; win: number; loss: number; push: number; pending: number; graded: number; hit: number | null; units: number; priced: number };

function score(rows: LedgerRow[]): Tally {
  const win = rows.filter((row) => row.result === "win").length;
  const loss = rows.filter((row) => row.result === "loss").length;
  const push = rows.filter((row) => row.result === "push").length;
  const pending = rows.length - win - loss - push;
  const pricedRows = rows.filter((row) => row.units != null && row.result !== "pending");
  const units = pricedRows.reduce((sum, row) => sum + Number(row.units), 0);
  const graded = win + loss;
  return { count: rows.length, win, loss, push, pending, graded, hit: graded ? win / graded : null, units, priced: pricedRows.length };
}

function chip(active: boolean) {
  return `rounded-full border px-3 py-1.5 text-sm ${active ? "border-gold bg-gold text-ink-950" : "border-white/15 text-paper"}`;
}

function percent(value: number) {
  return `${(value * 100).toFixed(1)}%`;
}

function signed(value: number) {
  const text = `${value >= 0 ? "+" : ""}${value.toFixed(2)}`;
  return text;
}

function toneClass(value: number | null | undefined) {
  if (value == null || value === 0) return "text-mute";
  return value > 0 ? "text-green-300" : "text-red-300";
}

function share(part: number, total: number) {
  if (!total) return "0%";
  return `${(part / total) * 100}%`;
}
