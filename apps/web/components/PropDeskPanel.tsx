"use client";

import { useEffect, useState } from "react";
import { LockedCall } from "@/components/LockedCall";
import { DeskRecord, PropDesk, type PropDeskCall } from "@/components/PropDesk";
import { browserGet, peekBrowserCache, rememberBrowserCache } from "@/lib/browserApi";

type DeskPayload = { calls?: PropDeskCall[]; locked?: boolean };

function deskPath(league: string, date: string) {
  return `/v1/props/desk?league=${league}&date=${date}`;
}

export function PropDeskPanel({ league, date, initial }: { league: string; date: string; initial?: PropDeskCall[] }) {
  const path = deskPath(league, date);
  const cached = peekBrowserCache<DeskPayload>(path);
  const seeded = (initial ?? cached?.calls ?? []);
  const [calls, setCalls] = useState<PropDeskCall[]>(seeded);
  const [locked, setLocked] = useState(Boolean(cached?.locked));
  const [loading, setLoading] = useState(seeded.length === 0);
  const [error, setError] = useState(false);
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    setCalls(initial ?? []);
    setLocked(false);
    setError(false);
    const warm = peekBrowserCache<DeskPayload>(path);
    const warmed = (warm?.calls ?? []);
    if (warm) {
      setCalls(warmed);
      setLocked(Boolean(warm?.locked));
      setLoading(false);
      setError(false);
    } else if (seeded.length === 0) {
      setLoading(true);
    }
    const ctrl = new AbortController();
    browserGet<DeskPayload>(path, ctrl.signal)
      .then((desk) => {
        if (ctrl.signal.aborted) return;
        rememberBrowserCache(path, desk);
        const next = desk.calls ?? [];
        setCalls(next);
        setLocked(Boolean(desk.locked));
        setError(false);
      })
      .catch(() => {
        if (!ctrl.signal.aborted) setError(true);
      })
      .finally(() => {
        if (!ctrl.signal.aborted) setLoading(false);
      });
    return () => ctrl.abort();
  }, [path, retry]);

  return (
    <>
      {error ? <p role="alert" className="text-sm text-heat">{calls.length ? "Refresh failed. These are the last saved picks." : "Picks are temporarily unavailable."} <button className="underline" onClick={() => setRetry(n => n + 1)}>Retry</button></p> : null}
      {calls.length > 0 ? <DeskRecord calls={calls} /> : null}
      <section className="space-y-3">
        <h2 className="font-display text-4xl uppercase leading-none tracking-wide">Desk picks</h2>
        {loading && calls.length === 0 && !locked ? (
          <p className="text-sm text-mute">Loading the desk picks. The prices below are already up.</p>
        ) : error && calls.length === 0 && !locked ? (
          <p className="text-sm text-mute">The picks could not load. Please retry; this is not an empty slate.</p>
        ) : locked && calls.length === 0 ? (
          <LockedCall
            title="Desk picks are locked"
            detail="One call per game opens at kickoff. Subscribers see the desk now — $19.99 a month."
          />
        ) : calls.length > 0 ? (
          <>
            {locked ? (
              <LockedCall
                title="The rest of the desk is locked"
                detail="Finished games stay up. Subscribers see the calls that have not kicked off — $19.99 a month."
              />
            ) : null}
            <PropDesk calls={calls} />
          </>
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
