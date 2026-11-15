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
  apiBase,
  month,
  selected,
  league,
  title,
}: {
  apiBase: string;
  month: string;
  selected: string;
  league: string;
  title: string;
}) {
  const [data, setData] = useState<DayCard | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 90000);
    setData(null);
    setFailed(false);
    const base = apiBase.replace(/\/$/, "");
    fetch(`${base}/v1/published/day?date=${selected}&league=${league}`, { signal: ctrl.signal })
      .then((res) => {
        if (!res.ok) throw new Error(String(res.status));
        return res.json() as Promise<DayCard>;
      })
      .then(setData)
      .catch(() => setFailed(true))
      .finally(() => clearTimeout(timer));
    return () => {
      clearTimeout(timer);
      ctrl.abort();
    };
  }, [apiBase, selected, league]);

  return (
    <>
      <LedgerCalendar month={month} selected={selected} marked={data?.dates ?? []} league={league} />
      {failed ? (
        <p role="alert">The record is temporarily unavailable. Results have not been replaced with zeros.</p>
      ) : !data ? (
        <p role="status">Loading this day.</p>
      ) : (
        <LedgerDay title={title} rows={data.rows ?? []} />
      )}
      <p className="max-w-3xl text-xs leading-relaxed text-mute">
        {data?.disclaimer || "Research results, not proof of future returns. A missing stat stays pending."}
      </p>
    </>
  );
}
