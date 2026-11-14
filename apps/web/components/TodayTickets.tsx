"use client";

import { useEffect, useState } from "react";
import { ParlayBoard } from "@/components/ParlayBoard";
import type { ParlayCard } from "@/lib/api";

export function TodayTickets({
  league,
  date,
  initialCard = null,
}: {
  league: string;
  date: string;
  initialCard?: ParlayCard | null;
}) {
  const [card, setCard] = useState<ParlayCard | null>(initialCard);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (initialCard) {
      setCard(initialCard);
      setError(null);
      return;
    }
    const ctrl = new AbortController();
    const timer = window.setTimeout(() => ctrl.abort(), 20000);
    setCard(null);
    setError(null);
    fetch(`/api/backend/v1/parlays/card?league=${encodeURIComponent(league)}&date=${encodeURIComponent(date)}`, {
      signal: ctrl.signal,
    })
      .then(async (res) => {
        if (!res.ok) throw new Error(`card ${res.status}`);
        return (await res.json()) as ParlayCard;
      })
      .then((data) => setCard(data))
      .catch((err: unknown) => {
        if (ctrl.signal.aborted) return;
        setError(err instanceof Error ? err.message : "unavailable");
      })
      .finally(() => window.clearTimeout(timer));
    return () => {
      ctrl.abort();
      window.clearTimeout(timer);
    };
  }, [league, date, initialCard]);

  if (error) {
    return (
      <p className="text-sm text-mute">
        Nothing cleared for this day. A ticket needs two legs whose chance sits near a fresh price.
      </p>
    );
  }
  if (!card) {
    return <p className="text-sm text-mute">Building the tickets for this day.</p>;
  }
  if (card.tickets.length === 0) {
    return (
      <p className="text-sm text-mute">
        Nothing cleared for this day. A ticket needs two legs whose chance sits near a fresh price.
      </p>
    );
  }
  return <ParlayBoard tickets={card.tickets} />;
}
