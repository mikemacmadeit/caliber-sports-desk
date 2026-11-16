"use client";

import { useEffect, useState } from "react";
import { LockedCall } from "@/components/LockedCall";
import { ParlayBoard } from "@/components/ParlayBoard";
import type { ParlayCard } from "@/lib/api";
import { browserGet } from "@/lib/browserApi";

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
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    if (initialCard && retry === 0) {
      setCard(initialCard);
      setError(null);
      return;
    }
    const ctrl = new AbortController();
    setCard(null);
    setError(null);
    browserGet<ParlayCard>(`/v1/parlays/card?league=${encodeURIComponent(league)}&date=${encodeURIComponent(date)}`, ctrl.signal)
      .then((data) => { if (!ctrl.signal.aborted) setCard(data); })
      .catch((err: unknown) => {
        if (ctrl.signal.aborted) return;
        setError(err instanceof Error ? err.message : "unavailable");
      });
    return () => {
      ctrl.abort();
    };
  }, [league, date, initialCard, retry]);

  if (error) {
    return (
      <p role="status" className="text-sm text-mute">
        The saved tickets could not load. <button className="underline" onClick={() => setRetry(n => n + 1)}>Retry</button>
      </p>
    );
  }
  if (!card) {
    return <p className="text-sm text-mute">Loading the saved tickets for this day.</p>;
  }
  if (card.locked) {
    return (
      <LockedCall
        title="The tickets are locked"
        detail="They open when the games kick off. Subscribers see them now — $19.99 a month."
      />
    );
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
