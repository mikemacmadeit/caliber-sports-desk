import Link from "next/link";
import { LedgerRecord, type LedgerCard, type YearRecord } from "@/components/LedgerRecord";
import { PageHeader } from "@/components/PageHeader";
import { apiGet } from "@/lib/api";
import { dayParts, slateToday } from "@/lib/days";

export default async function TrackRecordPage({
  searchParams,
}: {
  searchParams: Promise<{ league?: string; date?: string; month?: string }>;
}) {
  const params = await searchParams;
  const league = ["all", "nfl", "fbs"].includes(params.league || "") ? params.league! : "all";
  const selected = /^\d{4}-\d{2}-\d{2}$/.test(params.date || "") ? params.date! : previousDay(slateToday());
  const month = /^\d{4}-\d{2}$/.test(params.month || "") ? params.month! : selected.slice(0, 7);
  const when = dayParts(selected);
  const [card, calendar, year] = await Promise.all([
    apiGet<LedgerCard>(`/v1/published/day?date=${selected}&league=${league}`, 12000).catch(() => null),
    apiGet<{ units: Record<string, number> }>(`/v1/published/calendar?month=${month}&league=${league}`, 12000).catch(() => null),
    apiGet<YearRecord>(`/v1/published/year?year=${selected.slice(0, 4)}&league=${league}`, 12000).catch(() => null),
  ]);
  const units = calendar?.units ?? {};

  return (
    <div className="space-y-8">
      <PageHeader
        kicker="Original picks · original prices"
        title="The permanent record"
        lede="Pick a day. The spreads, the prop calls, and every anytime touchdown the desk named stay here, including the ones that lost."
      />
      <nav className="flex flex-wrap gap-3" aria-label="League">
        {["all", "nfl", "fbs"].map((item) => (
          <Link key={item} href={leagueHref(item, selected, month)} className={item === league ? "text-gold" : "text-mute"}>
            {item.toUpperCase()}
          </Link>
        ))}
      </nav>
      {!card || !calendar || !year ? <p role="status" className="text-sm text-mute">Some records are temporarily unavailable. Refresh to try again.</p> : null}
      <LedgerRecord
        month={month}
        selected={selected}
        league={league}
        title={`${when.weekday} · ${when.monthDay}`}
        card={card}
        units={units}
        year={year}
      />
    </div>
  );
}

function leagueHref(league: string, date: string, month: string) {
  const params = new URLSearchParams({ date, month });
  if (league !== "all") params.set("league", league);
  return `/track-record?${params}`;
}

function previousDay(iso: string) {
  const [year, month, day] = iso.split("-").map(Number);
  const date = new Date(Date.UTC(year, month - 1, day));
  date.setUTCDate(date.getUTCDate() - 1);
  return date.toISOString().slice(0, 10);
}
