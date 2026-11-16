"use client";

import { useEffect } from "react";
import { usePathname } from "next/navigation";

/** Send bounded measurements only. Never send URLs, error messages, or customer input. */
export function ClientTelemetry() {
  const path = usePathname();
  useEffect(() => {
    const route = /^\/games\//.test(path) ? "/games/:id" : path;
    const allowed = ["/", "/anytime-td", "/props", "/ats", "/schedule", "/track-record", "/account", "/games/:id", "/login", "/signup"];
    if (!allowed.includes(route)) return;
    let sent = 0;
    const send = (kind: string, value: number) => {
      if (++sent > 6) return;
      void fetch("/api/backend/v1/client-metrics", { method: "POST", keepalive: true,
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ route, kind, value }),
        signal: AbortSignal.timeout(5000) }).catch(() => {});
    };
    const error = () => send("error", 1);
    window.addEventListener("error", error);
    window.addEventListener("unhandledrejection", error);
    let paint = 0;
    let shift = 0;
    let interaction = 0;
    const observers: PerformanceObserver[] = [];
    for (const type of ["largest-contentful-paint", "layout-shift", "event"]) {
      if (typeof PerformanceObserver === "undefined" || !PerformanceObserver.supportedEntryTypes?.includes(type)) continue;
      const observer = new PerformanceObserver(list => {
        for (const entry of list.getEntries()) {
          if (type === "largest-contentful-paint") paint = entry.startTime;
          if (type === "layout-shift" && !(entry as PerformanceEntry & { hadRecentInput?: boolean }).hadRecentInput)
            shift += (entry as PerformanceEntry & { value: number }).value;
          if (type === "event") interaction = Math.max(interaction, entry.duration);
        }
      });
      observer.observe({ type, buffered: false });
      observers.push(observer);
    }
    const flush = () => {
      if (paint) send("paint_ms", Math.min(paint, 120000));
      if (interaction) send("interaction_max_ms", Math.min(interaction, 120000));
      if (shift) send("layout_shift_total", Math.min(shift, 100));
      paint = shift = interaction = 0;
    };
    const hidden = () => { if (document.visibilityState === "hidden") flush(); };
    document.addEventListener("visibilitychange", hidden);
    return () => {
      flush(); observers.forEach(observer => observer.disconnect());
      window.removeEventListener("error", error); window.removeEventListener("unhandledrejection", error);
      document.removeEventListener("visibilitychange", hidden);
    };
  }, [path]);
  return null;
}
