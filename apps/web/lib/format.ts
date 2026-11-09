export function american(n: number | null | undefined): string {
  if (n == null) return "—";
  return n > 0 ? `+${n}` : String(n);
}

export function pct(p: number | null | undefined, digits = 1): string {
  if (p == null || Number.isNaN(p)) return "—";
  return `${(p * 100).toFixed(digits)}%`;
}

export function evPct(ev: number | null | undefined): string {
  if (ev == null) return "—";
  const v = ev * 100;
  return `${v >= 0 ? "+" : ""}${v.toFixed(1)}%`;
}

const SLATE_TZ = "America/Chicago";

/** API datetimes are UTC. A stamp with no zone is still UTC, not local. */
function parseUtc(iso: string): Date {
  const hasZone = /(?:[zZ]|[+-]\d{2}:?\d{2})$/.test(iso);
  return new Date(hasZone ? iso : `${iso}Z`);
}

export function kickoffParts(iso: string): { day: string; time: string } {
  const d = parseUtc(iso);
  return {
    day: d.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric", timeZone: SLATE_TZ }),
    time: d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", timeZone: SLATE_TZ }),
  };
}

export function kickoffMs(iso: string | null | undefined): number {
  if (!iso) return Number.POSITIVE_INFINITY;
  const t = parseUtc(iso).getTime();
  return Number.isNaN(t) ? Number.POSITIVE_INFINITY : t;
}

export function kickoffDate(iso: string): string {
  return parseUtc(iso).toLocaleDateString("en-CA", { timeZone: SLATE_TZ });
}

export function kickoff(iso: string): string {
  const d = parseUtc(iso);
  return d.toLocaleString("en-US", {
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZone: SLATE_TZ,
    timeZoneName: "short",
  });
}

export function cToF(c: number | null | undefined): string {
  if (c == null) return "—";
  return `${Math.round((c * 9) / 5 + 32)}°F`;
}

export function mpsToMph(m: number | null | undefined): string {
  if (m == null) return "—";
  return `${Math.round(m * 2.237)} mph`;
}

export function marketLabel(id: string): string {
  return (
    {
      anytime_td: "Anytime TD",
      receptions: "Receptions",
      receiving_yards: "Receiving yards",
      rushing_yards: "Rushing yards",
      point_spread: "Spread",
    }[id] || id
  );
}
