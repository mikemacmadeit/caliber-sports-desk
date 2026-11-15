import { LedgerCalendar } from "@/components/LedgerCalendar";
import { LedgerDay, type LedgerRow } from "@/components/LedgerDay";

export type LedgerCard = {
  dates?: string[];
  rows?: LedgerRow[];
  disclaimer?: string;
};

export function LedgerRecord({
  month,
  selected,
  league,
  title,
  card,
  units,
}: {
  month: string;
  selected: string;
  league: string;
  title: string;
  card: LedgerCard | null;
  units: Record<string, number>;
}) {
  return (
    <>
      <LedgerCalendar month={month} selected={selected} units={units} league={league} />
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
