import Link from "next/link";
import { dayParts, dayWindow, withQuery } from "@/lib/days";

export function DayStrip({
  today,
  dates,
  selected,
  basePath,
  league = "all",
  extra = {},
}: {
  today: string;
  dates: string[];
  selected: string;
  basePath: string;
  league?: string;
  extra?: Record<string, string | undefined>;
}) {
  const days = dayWindow(today, dates, selected);
  const has = new Set(dates);

  return (
    <div className="flex gap-2 overflow-x-auto pb-1">
      {days.map((iso) => {
        const { weekday, day } = dayParts(iso);
        const active = iso === selected;
        const href = withQuery(basePath, {
          league,
          date: iso === today ? undefined : iso,
          ...extra,
        });
        return (
          <Link
            key={iso}
            href={href}
            className={`min-w-[4.75rem] shrink-0 border px-3 py-2 ${
              active ? "border-gold bg-gold text-ink-950" : "border-white/10 bg-ink-950 text-paper hover:border-white/30"
            }`}
          >
            <span className={`block font-mono text-[10px] uppercase tracking-[0.16em] ${active ? "text-ink-950/70" : "text-mute"}`}>
              {iso === today ? "Today" : weekday}
            </span>
            <span className="mt-1 flex items-center justify-between gap-3 font-display text-3xl uppercase leading-none">
              {day}
              {has.has(iso) ? (
                <span className={`h-1.5 w-1.5 rounded-full ${active ? "bg-ink-950" : "bg-gold"}`} />
              ) : (
                <span className="h-1.5 w-1.5" />
              )}
            </span>
          </Link>
        );
      })}
    </div>
  );
}

export function EmptyDay({
  selected,
  today,
  next,
  href,
}: {
  selected: string;
  today: string;
  next: string | null;
  href: string | null;
}) {
  const { weekday, monthDay } = dayParts(selected);
  const when = selected === today ? "today" : `${weekday}, ${monthDay}`;
  return (
    <div className="plate px-6 py-8">
      <p className="font-display text-4xl uppercase leading-none">No games {when}</p>
      {next && href ? (
        <Link href={href} className="mt-4 inline-block font-display text-2xl uppercase tracking-wide text-gold">
          Next games · {dayParts(next).weekday} {dayParts(next).monthDay} →
        </Link>
      ) : (
        <p className="mt-3 text-sm text-mute">Nothing else is on the slate.</p>
      )}
    </div>
  );
}
