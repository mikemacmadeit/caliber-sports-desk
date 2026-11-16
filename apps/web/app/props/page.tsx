import Link from "next/link";
import { DayStrip, EmptyDay } from "@/components/DayStrip";
import { LeagueToggle } from "@/components/LeagueToggle";
import { PageHeader } from "@/components/PageHeader";
import { PropBoard } from "@/components/PropBoard";
import { PropDeskPanel } from "@/components/PropDeskPanel";
import { TodayPropPicks } from "@/components/TodayPropPicks";
import { apiGet } from "@/lib/api";
import { dayParts, type SlateDays, withQuery } from "@/lib/days";

const MARKETS = [
  { id: "receptions", label: "Receptions" },
  { id: "receiving_yards", label: "Receiving" },
  { id: "rushing_yards", label: "Rushing" },
  { id: "anytime_td", label: "Anytime TD" },
];

export default async function PropsPage({
  searchParams,
}: {
  searchParams: Promise<{ league?: string; market?: string; date?: string }>;
}) {
  const { league = "all", market = "receptions", date: dateParam } = await searchParams;

  let days: SlateDays | null = null;
  let daysError: string | null = null;
  try {
    days = await apiGet<SlateDays>(`/v1/days?league=${league}`, 10000);
  } catch (e) {
    daysError = e instanceof Error ? e.message : "days unavailable";
  }

  const selected = dateParam || days?.today || new Date().toISOString().slice(0, 10);
  const next = days?.dates.find((d) => d > selected) ?? null;
  const onSlate = !days || days.dates.includes(selected);

  return (
    <div className="space-y-8">
      <PageHeader
        accent="signal"
        kicker={
          days && selected === days.today
            ? `Today · ${dayParts(selected).monthDay}`
            : dayParts(selected).monthDay
        }
        title="Props"
        lede="One receptions, receiving, or rushing prop for every game. The strongest chance under 90%. The board under it is every priced line."
        action={
          <LeagueToggle
            value={league}
            basePath="/props"
            date={selected}
            today={days?.today || selected}
            extra={{ market }}
          />
        }
      />
      {days ? (
        <DayStrip
          today={days.today}
          dates={days.dates}
          selected={selected}
          basePath="/props"
          league={league}
          extra={{ market }}
        />
      ) : null}
      {daysError ? (
        <div className="plate border border-gold/30 px-6 py-4 text-sm text-mute">
          <p>Schedule load failed: {daysError}</p>
          <p className="mt-1">Refresh after the API is back — this page no longer hard-crashes on a timeout.</p>
        </div>
      ) : null}
      <PropDeskPanel league={league} date={selected} />
      <TodayPropPicks league={league} date={selected} market={market} hideWhenEmpty />
      <div className="flex flex-wrap gap-2">
        {MARKETS.map((m) => (
          <Link
            key={m.id}
            href={withQuery("/props", {
              league,
              market: m.id,
              date: days && selected === days.today ? undefined : selected,
            })}
            className={`px-4 py-1.5 font-display text-lg uppercase tracking-wide ${
              market === m.id ? "bg-gold text-ink-950" : "border border-white/15 text-mute hover:text-paper"
            }`}
          >
            {m.label}
          </Link>
        ))}
      </div>
      {onSlate ? (
        <PropBoard league={league} date={selected} market={market} />
      ) : (
        <EmptyDay
          selected={selected}
          today={days?.today || selected}
          next={next}
          href={
            next
              ? withQuery("/props", {
                  league,
                  market,
                  date: days && next === days.today ? undefined : next,
                })
              : null
          }
        />
      )}
    </div>
  );
}

