import { PageHeader } from "@/components/PageHeader";
import { TodayPropPicks } from "@/components/TodayPropPicks";
import { LeagueToggle } from "@/components/LeagueToggle";
import { DayStrip } from "@/components/DayStrip";
import { apiGet, type PropPickCard } from "@/lib/api";
import { slateToday, type SlateDays } from "@/lib/days";

export default async function BestBetsPage({ searchParams }: { searchParams: Promise<{ league?: string; date?: string }> }) {
  const { league = "all", date = slateToday() } = await searchParams;
  const [card, days] = await Promise.all([
    apiGet<PropPickCard>(`/v1/props/picks?league=${encodeURIComponent(league)}&date=${encodeURIComponent(date)}`).catch(() => null),
    apiGet<SlateDays>(`/v1/days?league=${encodeURIComponent(league)}`).catch(() => null),
  ]);
  return <div className="space-y-8">
    <PageHeader kicker="Published before kickoff" title="Qualified opportunities"
      lede="Verified sportsbook prices, sufficient data, and estimated value after uncertainty. Every published selection keeps its original terms in the ledger."
      action={<LeagueToggle value={league} basePath="/best-bets" date={date} today={slateToday()} />} />
    {days ? <DayStrip today={days.today} dates={days.dates} selected={date} basePath="/best-bets" league={league} /> : null}
    <TodayPropPicks league={league} date={date} initialCard={card} />
  </div>;
}
