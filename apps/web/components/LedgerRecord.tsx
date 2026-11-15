"use client";

import { useEffect, useState } from "react";
import { LedgerCalendar } from "@/components/LedgerCalendar";
import { LedgerDay, type LedgerRow } from "@/components/LedgerDay";

type DayCard = {
  dates?: string[];
  rows?: LedgerRow[];
  disclaimer?: string;
};

export function LedgerRecord({
  month,
  selected,
  league,
  title,
}: {
  month: string;
  selected: string;
  league: string;
  title: string;
}) {
  const [card, setCard] = useState<{ key: string; data: DayCard } | null>(null);
  const [failed, setFailed] = useState(false);
  const key = `${selected}|${league}`;
  const data = card?.key === key ? card.data : null;

  useEffect(() => {
    let active = true;
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 90000);
    setFailed(false);
    fetch(`/api/backend/v1/published/day?date=${selected}&league=${league}`, { signal: ctrl.signal })
      .then((res) => {
        if (!res.ok) throw new Error(String(res.status));
        return res.json() as Promise<DayCard>;
      })
      .then((next) => {
        if (active) setCard({ key, data: next });
      })
      .catch((error: unknown) => {
        if (!active || (error instanceof DOMException && error.name === "AbortError")) return;
        setFailed(true);
      })
      .finally(() => clearTimeout(timer));
    return () => {
      active = false;
      clearTimeout(timer);
      ctrl.abort();
    };
  }, [key, selected, league]);

  return (
    <>
      <LedgerCalendar month={month} selected={selected} marked={data?.dates ?? []} league={league} />
      {data ? (
        <LedgerDay title={title} rows={data.rows ?? []} />
      ) : failed ? (
        <p role="alert">The record is temporarily unavailable. Results have not been replaced with zeros.</p>
      ) : (
        <p role="status">Loading this day.</p>
      )}
      <p className="max-w-3xl text-xs leading-relaxed text-mute">
        {data?.disclaimer || "Research results, not proof of future returns. A missing stat stays pending."}
      </p>
    </>
  );
}
