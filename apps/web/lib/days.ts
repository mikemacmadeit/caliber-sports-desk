export type SlateDays = {
  today: string;
  timezone: string;
  dates: string[];
};

export function slateToday(now = new Date()): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/Chicago",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(now);
}

export function addDay(iso: string): string {
  const [y, m, d] = iso.split("-").map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d));
  dt.setUTCDate(dt.getUTCDate() + 1);
  return dt.toISOString().slice(0, 10);
}

export function dayParts(iso: string): { weekday: string; monthDay: string; day: string } {
  const [y, m, d] = iso.split("-").map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d, 18));
  const zone = "America/Chicago";
  return {
    weekday: dt.toLocaleDateString("en-US", { weekday: "short", timeZone: zone }),
    monthDay: dt.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: zone }),
    day: dt.toLocaleDateString("en-US", { day: "numeric", timeZone: zone }),
  };
}

function shiftDay(iso: string, delta: number): string {
  const [y, m, d] = iso.split("-").map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d));
  dt.setUTCDate(dt.getUTCDate() + delta);
  return dt.toISOString().slice(0, 10);
}

/** Chips start on today. A full season of dates must not push the strip back to September. */
export function dayWindow(today: string, dates: string[], selected: string): string[] {
  const upcoming = dates.filter((d) => d >= today).sort();
  const last = upcoming.length ? upcoming[upcoming.length - 1] : today;
  let start = today;
  if (selected > today) {
    const horizon = shiftDay(selected, -15);
    if (horizon > start) start = horizon;
  }
  const end = last > shiftDay(start, 15) ? shiftDay(start, 15) : last;
  const out: string[] = [];
  let cursor = start;
  while (cursor <= end && out.length < 16) {
    out.push(cursor);
    cursor = addDay(cursor);
  }
  return out.length ? out : [today];
}

export function withQuery(path: string, query: Record<string, string | undefined>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value) params.set(key, value);
  }
  const qs = params.toString();
  return qs ? `${path}?${qs}` : path;
}
