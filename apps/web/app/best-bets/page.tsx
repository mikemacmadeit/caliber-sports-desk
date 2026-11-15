import { QualifiedBoard, type QualifiedPrice, type QualifiedSpread } from "@/components/QualifiedBoard";
import { PageHeader } from "@/components/PageHeader";
import { LeagueToggle } from "@/components/LeagueToggle";
import { DayStrip } from "@/components/DayStrip";
import { apiGet } from "@/lib/api";
import { slateToday, type SlateDays } from "@/lib/days";

type BestBets = {
  spreads?: QualifiedSpread[];
  prices?: QualifiedPrice[];
};

export default async function BestBetsPage({ searchParams }: { searchParams: Promise<{ league?: string; date?: string }> }) {
  const { league = "all", date = slateToday() } = await searchParams;
  const [card, days] = await Promise.all([
    apiGet<BestBets>(`/v1/best-bets?league=${encodeURIComponent(league)}&date=${encodeURIComponent(date)}`).catch(() => null),
    apiGet<SlateDays>(`/v1/days?league=${encodeURIComponent(league)}`).catch(() => null),
  ]);
  const spreads = card?.spreads || [];
  const prices = card?.prices || [];
  return <div className="space-y-8">
    <PageHeader kicker="This day's card" title="Qualified opportunities"
      lede="Every spread and desk player prop from the day. Anytime touchdowns stay on their own page. Green is a win, red is a loss, and the day's total sits at the top."
      action={<LeagueToggle value={league} basePath="/best-bets" date={date} today={slateToday()} />} />
    {days ? <DayStrip today={days.today} dates={days.dates} selected={date} basePath="/best-bets" league={league} /> : null}
    {!card ? <p role="status" className="text-sm text-mute">Today's card is temporarily unavailable.</p> : spreads.length + prices.length === 0 ? <p className="plate px-5 py-6 text-sm text-mute">No picks for this day.</p> : <QualifiedBoard spreads={spreads} prices={prices} />}
  </div>;
}
