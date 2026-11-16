import { BestBetsPanel } from "@/components/BestBetsPanel";
import { PageHeader } from "@/components/PageHeader";
import { LeagueToggle } from "@/components/LeagueToggle";
import { DayStrip } from "@/components/DayStrip";
import { apiGet } from "@/lib/api";
import { validDay, validLeague, slateToday, type SlateDays } from "@/lib/days";

export default async function BestBetsPage({ searchParams }: { searchParams: Promise<{ league?: string; date?: string }> }) {
  const params = await searchParams;
  const league = validLeague(params.league);
  const date = validDay(params.date) || slateToday();
  const days = await apiGet<SlateDays>(`/v1/days?league=${encodeURIComponent(league)}`).catch(() => null);
  return <div className="space-y-8">
    <PageHeader kicker="This day's card" title="Qualified opportunities"
      lede="Every spread and desk player prop from the day. Anytime touchdowns stay on their own page. Green is a win, red is a loss, and the day's total sits at the top."
      action={<LeagueToggle value={league} basePath="/best-bets" date={date} today={slateToday()} />} />
    {days ? <DayStrip today={days.today} dates={days.dates} selected={date} basePath="/best-bets" league={league} /> : null}
    <BestBetsPanel key={`${league}:${date}`} league={league} date={date} />
  </div>;
}
