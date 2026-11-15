import Link from "next/link";

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

export function LedgerCalendar({
  month,
  selected,
  units,
  league,
}: {
  month: string;
  selected: string;
  units: Record<string, number>;
  league: string;
}) {
  const [year, monthNumber] = month.split("-").map(Number);
  const first = new Date(Date.UTC(year, monthNumber - 1, 1));
  const blanks = first.getUTCDay();
  const count = new Date(Date.UTC(year, monthNumber, 0)).getUTCDate();
  const label = first.toLocaleDateString("en-US", { month: "long", year: "numeric", timeZone: "UTC" });

  return (
    <section className="plate p-4 sm:p-5" aria-label="Pick calendar">
      <div className="flex items-center justify-between gap-3">
        <Link href={href(shiftMonth(month, -1), selected, league)} className="font-mono text-xs uppercase tracking-[0.16em] text-mute" aria-label="Previous month">
          ← {monthName(shiftMonth(month, -1))}
        </Link>
        <h2 className="font-display text-3xl uppercase leading-none tracking-wide">{label}</h2>
        <Link href={href(shiftMonth(month, 1), selected, league)} className="font-mono text-xs uppercase tracking-[0.16em] text-mute" aria-label="Next month">
          {monthName(shiftMonth(month, 1))} →
        </Link>
      </div>
      <div className="mt-4 grid grid-cols-7 gap-1 text-center font-mono text-[10px] uppercase tracking-[0.14em] text-mute">
        {WEEKDAYS.map((day) => (
          <div key={day} className="py-1">{day}</div>
        ))}
      </div>
      <div className="grid grid-cols-7 gap-1">
        {Array.from({ length: blanks }, (_, index) => (
          <div key={`blank-${index}`} />
        ))}
        {Array.from({ length: count }, (_, index) => {
          const day = String(index + 1).padStart(2, "0");
          const iso = `${month}-${day}`;
          const active = iso === selected;
          const total = units[iso];
          const figure = total == null ? "" : signed(total);
          return (
            <Link
              key={iso}
              href={href(month, iso, league)}
              aria-current={active ? "date" : undefined}
              aria-label={figure ? `${index + 1}, ${figure} units` : String(index + 1)}
              className={`flex min-h-14 flex-col items-center justify-center rounded-md border px-0.5 text-sm ${
                active ? "border-gold bg-gold text-ink-950" : "border-white/10 text-paper hover:border-white/30"
              }`}
            >
              <span className="font-display text-xl leading-none">{index + 1}</span>
              {figure ? (
                <span className={`mt-1 font-mono text-[10px] leading-none ${active ? "text-ink-950" : total > 0 ? "text-green-300" : total < 0 ? "text-red-300" : "text-mute"}`}>
                  {figure}
                </span>
              ) : null}
            </Link>
          );
        })}
      </div>
    </section>
  );
}

function signed(value: number) {
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}`;
}

function href(month: string, date: string, league: string) {
  const params = new URLSearchParams({ month, date });
  if (league !== "all") params.set("league", league);
  return `/track-record?${params}`;
}

function shiftMonth(month: string, delta: number) {
  const [year, monthNumber] = month.split("-").map(Number);
  const next = new Date(Date.UTC(year, monthNumber - 1 + delta, 1));
  return next.toISOString().slice(0, 7);
}

function monthName(month: string) {
  const [year, monthNumber] = month.split("-").map(Number);
  return new Date(Date.UTC(year, monthNumber - 1, 1)).toLocaleDateString("en-US", { month: "short", timeZone: "UTC" });
}
