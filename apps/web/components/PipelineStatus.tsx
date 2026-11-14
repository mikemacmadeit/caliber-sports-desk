"use client";
import { useState, useEffect } from "react";
export function PipelineStatus() {
  const [state, setState] = useState<"loading" | "ok" | "degraded" | "unavailable">("loading");
  useEffect(() => {
    let disposed = false;
    const check = async () => {
      try {
        const response = await fetch("/api/backend/v1/operations/status", { cache: "no-store", signal: AbortSignal.timeout(10000) });
        if (!response.ok) throw new Error();
        const body = await response.json();
        if (!disposed) setState(body.ok ? "ok" : "degraded");
      } catch { if (!disposed) setState("unavailable"); }
    };
    void check();
    const timer = setInterval(() => void check(), 60000);
    return () => { disposed = true; clearInterval(timer); };
  }, []);
  if (state === "ok" || state === "loading") return null;
  return <p role="status" className="border-b border-gold/30 bg-gold/10 px-5 py-3 text-sm">{state === "degraded" ? "Some data updates are delayed. Opportunities appear only while their prices and inputs pass verification." : "Live update status is unavailable. Check the verification time on each opportunity."}</p>;
}
