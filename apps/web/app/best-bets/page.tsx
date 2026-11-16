import type { AtsRow } from "@/components/AtsBoard";
import { QualifiedBoard, type QualifiedPrice, type QualifiedSpread } from "@/components/QualifiedBoard";
import { PageHeader } from "@/components/PageHeader";
import { LeagueToggle } from "@/components/LeagueToggle";
import { DayStrip } from "@/components/DayStrip";
import savedBoard from "@/lib/ats/board.json";
import { apiGet } from "@/lib/api";
import { slateToday, type SlateDays } from "@/lib/days";
import { kickoffDate } from "@/lib/format";
import { finalsForDate, gradeFinishedCall } from "@/lib/gradeCalls";

type BestBets = {
  spreads?: QualifiedSpread[];
  prices?: QualifiedPrice[];
};

export default async function BestBetsPage({ searchParams }: { searchParams: Promise<{ league?: string; date?: string }> }) {
  const { league = "all", date = slateToday() } = await searchParams;
  const [card, days, finals] = await Promise.all([
    apiGet<BestBets>(`/v1/best-bets?league=${encodeURIComponent(league)}&date=${encodeURIComponent(date)}`).catch(() => null),
    apiGet<SlateDays>(`/v1/days?league=${encodeURIComponent(league)}`).catch(() => null),
    finalsForDate(date),
  ]);
  const prices = card?.prices || [];
  const spreads = card?.spreads?.length ? card.spreads : savedSpreads(league, date, finals);
  return <div className="space-y-8">
    <PageHeader kicker="This day's card" title="Qualified opportunities"
      lede="Every spread and desk player prop from the day. Anytime touchdowns stay on their own page. Green is a win, red is a loss, and the day's total sits at the top."
      action={<LeagueToggle value={league} basePath="/best-bets" date={date} today={slateToday()} />} />
    {days ? <DayStrip today={days.today} dates={days.dates} selected={date} basePath="/best-bets" league={league} /> : null}
    {!card && spreads.length === 0 ? <p role="status" className="text-sm text-mute">Today's card is temporarily unavailable.</p> : spreads.length + prices.length === 0 ? <p className="plate px-5 py-6 text-sm text-mute">No picks for this day.</p> : <QualifiedBoard spreads={spreads} prices={prices} />}
  </div>;
}

function roundEdge(value: number | null | undefined): number | null {
  if (value == null || Number.isNaN(value)) return null;
  return Math.round(Math.abs(value) * 10) / 10;
}

function savedSpreads(league: string, date: string, finals: Awaited<ReturnType<typeof finalsForDate>>): QualifiedSpread[] {
  return (savedBoard.rows as AtsRow[])
    .filter((row) => kickoffDate(row.kickoff_utc) === date && (league === "all" || row.league_id === league) && row.ats_pick_label)
    .map((row) => gradeFinishedCall(row, finals))
    .map((row) => ({
      game_id: row.game_id,
      league_id: row.league_id,
      kickoff_utc: row.kickoff_utc,
      game_status: row.game_status,
      home_score: row.home_score,
      away_score: row.away_score,
      away_team: row.away_team,
      home_team: row.home_team,
      ats_pick_label: row.ats_pick_label,
      ats_pick_result: row.ats_pick_result,
      edge_points: roundEdge(row.ats_cover_margin ?? row.pick_edge_points),
    }));
}
