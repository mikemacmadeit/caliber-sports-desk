import { DayStrip, EmptyDay } from "@/components/DayStrip";
import { PageHeader } from "@/components/PageHeader";
import { ScenarioLab } from "@/components/ScenarioLab";
import { apiGet, type SlateResponse } from "@/lib/api";
import { dayParts, type SlateDays, withQuery } from "@/lib/days";

export default async function ScenarioPage({
  searchParams,
}: {
  searchParams: Promise<{ date?: string }>;
}) {
  const { date: dateParam } = await searchParams;
  const days = await apiGet<SlateDays>("/v1/days?league=all");
  const selected = dateParam || days.today;
  const slate = await apiGet<SlateResponse>(`/v1/slate?league=all&date=${selected}`);
  const next = days.dates.find((d) => d > selected) ?? null;
  return (
    <div className="space-y-8">
      <PageHeader
        kicker={selected === days.today ? `Today · ${dayParts(selected).monthDay}` : dayParts(selected).monthDay}
        title="Scenario lab"
        lede="Change the weather on a game from this day. The official call stays put."
      />
      <DayStrip today={days.today} dates={days.dates} selected={selected} basePath="/scenario" />
      {slate.games.length === 0 ? (
        <EmptyDay
          selected={selected}
          today={days.today}
          next={next}
          href={next ? withQuery("/scenario", { date: next === days.today ? undefined : next }) : null}
        />
      ) : (
        <ScenarioLab games={slate.games} />
      )}
    </div>
  );
}
