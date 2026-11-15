"use client";

import { useEffect, useState } from "react";
import { DeskRecord, PropDesk, type PropDeskCall } from "@/components/PropDesk";
import { browserGet, peekBrowserCache, rememberBrowserCache } from "@/lib/browserApi";

type DeskPayload = { calls?: PropDeskCall[] };

function deskPath(league: string, date: string) {
  return `/v1/props/desk?league=${league}&date=${date}`;
}

export function PropDeskPanel({ league, date }: { league: string; date: string }) {
  const path = deskPath(league, date);
  const cached = peekBrowserCache<DeskPayload>(path);
  const [calls, setCalls] = useState<PropDeskCall[]>(cached?.calls ?? []);
  const [loading, setLoading] = useState(!(cached?.calls?.length));
  const [error, setError] = useState(false);

  useEffect(() => {
    const warm = peekBrowserCache<DeskPayload>(path);
    if (warm?.calls?.length) {
      setCalls(warm.calls);
      setLoading(false);
      setError(false);
    } else {
      setLoading(true);
    }
    const ctrl = new AbortController();
    browserGet<DeskPayload>(path, ctrl.signal)
      .then((desk) => {
        if (ctrl.signal.aborted) return;
        rememberBrowserCache(path, desk);
        setCalls(desk.calls ?? []);
        setError(false);
      })
      .catch(() => {
        if (!ctrl.signal.aborted && !(warm?.calls?.length)) setError(true);
      })
      .finally(() => {
        if (!ctrl.signal.aborted) setLoading(false);
      });
    return () => ctrl.abort();
  }, [path]);

  return (
    <>
      {calls.length > 0 ? <DeskRecord calls={calls} /> : null}
      <section className="space-y-3">
        <h2 className="font-display text-4xl uppercase leading-none tracking-wide">Desk picks</h2>
        {loading && calls.length === 0 ? (
          <p className="text-sm text-mute">Loading the desk picks. The prices below are already up.</p>
        ) : error && calls.length === 0 ? (
          <p className="text-sm text-mute">The prices below are up. The desk picks are still loading.</p>
        ) : calls.length > 0 ? (
          <PropDesk calls={calls} />
        ) : (
          <div className="plate px-6 py-8">
            <p className="font-display text-4xl uppercase leading-none">No prop call for this day</p>
            <p className="mt-3 max-w-xl text-sm text-mute">A call is the strongest receptions, receiving, or rushing line in each game.</p>
          </div>
        )}
      </section>
    </>
  );
}
