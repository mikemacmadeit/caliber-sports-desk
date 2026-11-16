"use client";

import { useEffect, useState } from "react";
import { LockedCall } from "@/components/LockedCall";
import { QualifiedBoard, type QualifiedPrice, type QualifiedSpread } from "@/components/QualifiedBoard";
import { browserGet, peekBrowserCache } from "@/lib/browserApi";

type BestBets = {
  locked?: boolean;
  spreads?: QualifiedSpread[];
  prices?: QualifiedPrice[];
};

export function BestBetsPanel({ league, date }: { league: string; date: string }) {
  const path = `/v1/best-bets?league=${encodeURIComponent(league)}&date=${encodeURIComponent(date)}`;
  const cached = peekBrowserCache<BestBets>(path);
  const [card, setCard] = useState<BestBets | null>(cached);
  const [error, setError] = useState(false);

  useEffect(() => {
    const hit = peekBrowserCache<BestBets>(path);
    setCard(hit);
    setError(false);
    let cancel = false;
    browserGet<BestBets>(path)
      .then((data) => {
        if (!cancel) {
          setCard(data);
          setError(false);
        }
      })
      .catch(() => {
        if (!cancel && !hit) setError(true);
      });
    return () => {
      cancel = true;
    };
  }, [path]);

  if (error && !card) {
    return <p role="status" className="text-sm text-mute">Today's card is temporarily unavailable.</p>;
  }
  if (!card) {
    return <p role="status" className="text-sm text-mute">Opening the saved card…</p>;
  }
  const spreads = card.spreads || [];
  const prices = card.prices || [];
  if (card.locked && spreads.length + prices.length === 0) {
    return (
      <LockedCall
        title="This card is locked"
        detail="The day's spreads and props open at kickoff. Subscribers see them now — $19.99 a month."
      />
    );
  }
  if (spreads.length + prices.length === 0) {
    return <p className="plate px-5 py-6 text-sm text-mute">No picks for this day.</p>;
  }
  return (
    <div className="space-y-6">
      {card.locked ? (
        <LockedCall
          title="The rest of this card is locked"
          detail="Finished results stay up. Subscribers see the calls that have not kicked off — $19.99 a month."
        />
      ) : null}
      <QualifiedBoard spreads={spreads} prices={prices} />
    </div>
  );
}
