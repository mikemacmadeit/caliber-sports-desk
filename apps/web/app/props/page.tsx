import Link from "next/link";
import { DayStrip, EmptyDay } from "@/components/DayStrip";
import { LeagueToggle } from "@/components/LeagueToggle";
import { PageHeader } from "@/components/PageHeader";
import { PropExplorer } from "@/components/PropExplorer";
import { TodayPropPicks } from "@/components/TodayPropPicks";
import { apiGet, type PropRow } from "@/lib/api";
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
    days = await apiGet<SlateDays>(`/v1/days?league=${league}`, 30000);
  } catch (e) {
    daysError = e instanceof Error ? e.message : "days unavailable";
  }

  const selected = dateParam || days?.today || new Date().toISOString().slice(0, 10);
  let data: { rows: PropRow[] } | null = null;
  let propsError: string | null = null;
  try {
    data = await apiGet<{ rows: PropRow[] }>(
      `/v1/props?market=${market}&league=${league}&date=${selected}`,
      60000,
    );
  } catch (e) {
    propsError = e instanceof Error ? e.message : "props unavailable";
  }

  const next = days?.dates.find((d) => d > selected) ?? null;
  const rows = data?.rows ?? [];

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
        lede="The calls for this day sit first, one per market per game. The board under them is every priced line."
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
      {(daysError || propsError) && (
        <div className="plate border border-gold/30 px-6 py-4 text-sm text-mute">
          {daysError ? <p>Schedule load failed: {daysError}</p> : null}
          {propsError ? <p>Props load failed: {propsError}</p> : null}
          <p className="mt-1">Refresh after the API is back — this page no longer hard-crashes on a timeout.</p>
        </div>
      )}
      <TodayPropPicks league={league} date={selected} market={market} />
      <div className="flex flex-wrap gap-2">
        {MARKETS.map((m) => (
          <a
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
          </a>
        ))}
      </div>
      {rows.length === 0 ? (
        days?.dates.includes(selected) ? (
          <div className="plate px-6 py-8">
            <p className="font-display text-4xl uppercase leading-none">
              {propsError ? "Props unavailable" : "No prices for this day"}
            </p>
            <p className="mt-3 max-w-xl text-sm text-mute">
              {propsError
                ? "The desk could not load this market right now. Try again in a moment."
                : "The games are on the board. This market has no locked number for them yet — or every game has already kicked off."}
            </p>
            <Link
              href={withQuery("/", { league, date: days && selected === days.today ? undefined : selected })}
              className="mt-4 inline-block font-display text-2xl uppercase tracking-wide text-gold"
            >
              Open the slate →
            </Link>
          </div>
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
        )
      ) : (
        <PropExplorer rows={rows} mode="flat" />
      )}
    </div>
  );
}
