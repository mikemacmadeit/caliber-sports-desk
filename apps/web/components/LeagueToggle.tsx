import Link from "next/link";
import { withQuery } from "@/lib/days";

export function LeagueToggle({
  value,
  basePath,
  date,
  today,
  extra = {},
}: {
  value: string;
  basePath: string;
  date?: string;
  today?: string;
  extra?: Record<string, string | undefined>;
}) {
  const opts = [
    { id: "all", label: "Both" },
    { id: "nfl", label: "NFL" },
    { id: "fbs", label: "FBS" },
  ];
  return (
    <div className="inline-flex border border-white/10 bg-ink-950 p-1">
      {opts.map((o) => {
        const href = withQuery(basePath, {
          league: o.id,
          date: date && date !== today ? date : undefined,
          ...extra,
        });
        const active = value === o.id || (o.id === "all" && !value);
        return (
          <Link
            key={o.id}
            href={href}
            className={`px-4 py-1.5 font-display text-lg uppercase tracking-wide ${
              active ? "bg-gold text-ink-950" : "text-mute hover:text-paper"
            }`}
          >
            {o.label}
          </Link>
        );
      })}
    </div>
  );
}
