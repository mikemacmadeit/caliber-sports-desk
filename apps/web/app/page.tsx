import { PageHeader } from "@/components/PageHeader";
import { TodayDesk } from "@/components/TodayDesk";
import { LeagueToggle } from "@/components/LeagueToggle";
import { DayStrip } from "@/components/DayStrip";
import { apiGet } from "@/lib/api";
import type { DeskData } from "@/lib/desk";
import { slateToday, type SlateDays } from "@/lib/days";

export default async function TodayPage({ searchParams }: { searchParams: Promise<{ league?: string; date?: string }> }) {
  const params = await searchParams;
  const league = ["all", "nfl", "fbs"].includes(params.league || "") ? params.league! : "all";
  const date = /^\d{4}-\d{2}-\d{2}$/.test(params.date || "") ? params.date! : slateToday();
  const query = new URLSearchParams({ league, date });
  const [initial, days] = await Promise.all([
    apiGet<DeskData>(`/v1/desk?${query}`, 10000).catch(() => null),
    apiGet<SlateDays>(`/v1/days?league=${league}`, 10000).catch(() => null),
  ]);
  return <div className="space-y-7">
    <PageHeader kicker="NFL & college football" title="Today, with context."
      lede="Research the opportunity. Understand the risk. Know your price."
      action={<LeagueToggle value={league} basePath="/" date={date} today={slateToday()} />} />
    <DayStrip today={days?.today || slateToday()} dates={days?.dates || [date]} selected={date} basePath="/" league={league} />
    <TodayDesk key={`${league}:${date}`} initial={initial} league={league} date={date} />
  </div>;
}
