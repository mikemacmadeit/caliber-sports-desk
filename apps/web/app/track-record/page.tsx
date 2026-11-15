import Link from "next/link";
import { LedgerCalendar } from "@/components/LedgerCalendar";
import { LedgerDay, type LedgerRow } from "@/components/LedgerDay";
import { PageHeader } from "@/components/PageHeader";
import { apiGet } from "@/lib/api";
import { dayParts, slateToday } from "@/lib/days";

type DayCard = {
  date: string;
  dates: string[];
  rows: LedgerRow[];
  disclaimer?: string;
};

export default async function TrackRecordPage({
  searchParams,
}: {
  searchParams: Promise<{ league?: string; date?: string; month?: string }>;
}) {
  const params = await searchParams;
  const league = ["all", "nfl", "fbs"].includes(params.league || "") ? params.league! : "all";
  const selected = /^\d{4}-\d{2}-\d{2}$/.test(params.date || "") ? params.date! : previousDay(slateToday());
  const month = /^\d{4}-\d{2}$/.test(params.month || "") ? params.month! : selected.slice(0, 7);
  const data = await apiGet<DayCard>(
    `/v1/published/day?date=${selected}&league=${league}`,
    30000,
  ).catch(() => null);
  const when = dayParts(selected);

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
      <LedgerCalendar month={month} selected={selected} marked={data?.dates ?? []} league={league} />
      {!data ? (
        <p role="alert">The record is temporarily unavailable. Results have not been replaced with zeros.</p>
      ) : (
        <LedgerDay title={`${when.weekday} · ${when.monthDay}`} rows={data.rows} />
      )}
      <p className="max-w-3xl text-xs leading-relaxed text-mute">
        {data?.disclaimer || "Research results, not proof of future returns. A missing stat stays pending."}
      </p>
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
