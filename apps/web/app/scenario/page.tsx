import { DayStrip, EmptyDay } from "@/components/DayStrip";
import { LockedCall } from "@/components/LockedCall";
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
  const [slate, me] = await Promise.all([
    apiGet<SlateResponse>(`/v1/slate?league=all&date=${selected}`),
    apiGet<{ user: { subscribed: boolean } | null }>("/v1/auth/me").catch(() => ({ user: null })),
  ]);
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
      ) : me.user?.subscribed ? (
        <ScenarioLab games={slate.games} />
      ) : (
        <LockedCall
          title="Scenario lab is part of the subscription"
          detail="Rerunning a game is on the $19.99 plan. The schedule above stays open."
        />
      )}
    </div>
  );
}
