import { LedgerCalendar } from "@/components/LedgerCalendar";
import { LedgerDay, type LedgerRow } from "@/components/LedgerDay";
import { LockedCall } from "@/components/LockedCall";

export type LedgerCard = {
  record_status?: string;
  note?: string;
  dates?: string[];
  rows?: LedgerRow[];
  locked?: boolean;
  disclaimer?: string;
};

export type YearMarket = { label: string; win: number; loss: number; units: number; priced: number };

export type YearRecord = {
  year: string;
  days: number;
  win: number;
  loss: number;
  push: number;
  pending: number;
  units: number;
  priced: number;
  markets: YearMarket[];
};

export function LedgerRecord({
  month,
  selected,
  league,
  title,
  card,
  units,
  year,
}: {
  month: string;
  selected: string;
  league: string;
  title: string;
  card: LedgerCard | null;
  units: Record<string, number>;
  year: YearRecord | null;
}) {
  return (
    <>
      {year ? <YearTotal year={year} /> : null}
      <LedgerCalendar month={month} selected={selected} units={units} league={league} />
      {card?.locked ? (
        <LockedCall
          title="Open calls are locked"
          detail="Finished results stay on the record. Subscribers see a call before kickoff — $19.99 a month."
        />
      ) : null}
      {card?.note ? <p role="status" className="text-sm text-gold">{card.note}</p> : null}
      {card ? (
        <LedgerDay title={title} rows={card.rows ?? []} />
      ) : (
        <p role="alert">The record is temporarily unavailable. Results have not been replaced with zeros.</p>
      )}
      <p className="max-w-3xl text-xs leading-relaxed text-mute">
        {card?.disclaimer || "Research results, not proof of future returns. A missing stat stays pending."}
      </p>
    </>
  );
}

function YearTotal({ year }: { year: YearRecord }) {
  const graded = year.win + year.loss;
  const hit = graded ? year.win / graded : null;
  const open = year.pending ? `${year.pending} still open` : year.push ? `${year.push} pushes` : "Graded";
  return (
    <section className="plate p-4 sm:p-5" aria-label={`${year.year} record`}>
      <div className="flex flex-wrap items-end justify-between gap-3">
        <h2 className="font-display text-3xl uppercase leading-none tracking-wide">{year.year}</h2>
        <p className="font-mono text-xs uppercase tracking-[0.16em] text-mute">
          {year.days} {year.days === 1 ? "day" : "days"} · spreads, touchdowns, and props
        </p>
      </div>
      <div className="mt-4 grid gap-3 sm:grid-cols-4">
        <YearTile label="Record" value={`${year.win}–${year.loss}`} hint={open} />
        <YearTile label="Hit rate" value={hit == null ? "—" : `${(hit * 100).toFixed(1)}%`} hint={`${graded} graded`} />
        <YearTile
          label="Units"
          value={year.priced ? signed(year.units) : "—"}
          hint={year.priced ? `${year.priced} priced picks` : "No prices graded"}
          tone={year.priced && year.units > 0 ? "win" : year.priced && year.units < 0 ? "loss" : undefined}
        />
        <YearTile label="Days" value={String(year.days)} hint="Saved on the record" />
      </div>
      {year.markets.length ? (
        <p className="mt-4 font-mono text-xs uppercase tracking-[0.12em] text-mute">
          {year.markets.map((market) => `${market.label} ${market.win}–${market.loss}`).join(" · ")}
        </p>
      ) : null}
    </section>
  );
}

function YearTile({ label, value, hint, tone }: { label: string; value: string; hint: string; tone?: "win" | "loss" }) {
  return (
    <div>
      <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-mute">{label}</p>
      <p className={`mt-2 font-display text-4xl uppercase leading-none tracking-wide ${tone === "win" ? "text-green-300" : tone === "loss" ? "text-red-300" : "text-paper"}`}>{value}</p>
      <p className="mt-2 text-xs text-mute">{hint}</p>
    </div>
  );
}

function signed(value: number) {
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}`;
}
