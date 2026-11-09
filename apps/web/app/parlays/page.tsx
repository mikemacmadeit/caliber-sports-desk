import { DayStrip, EmptyDay } from "@/components/DayStrip";
import { LeagueToggle } from "@/components/LeagueToggle";
import { PageHeader } from "@/components/PageHeader";
import { ParlayBoard } from "@/components/ParlayBoard";
import { apiGet, type ParlayCard } from "@/lib/api";
import { dayParts, slateToday, type SlateDays, withQuery } from "@/lib/days";
import { kickoffParts, pct } from "@/lib/format";

export const metadata = {
  title: "Parlays — Caliber",
  description: "The day's tickets, already built from the slate.",
};

export default async function ParlaysPage({
  searchParams,
}: {
  searchParams: Promise<{ league?: string; date?: string }>;
}) {
  const { league = "all", date: dateParam } = await searchParams;
  const days = await load<SlateDays>(`/v1/days?league=${league}`, 20000);
  const today = days.data?.today ?? slateToday();
  const selected = dateParam || today;
  const next = days.data?.dates.find((d) => d > selected) ?? null;
  const card = await load<ParlayCard>(`/v1/parlays/card?league=${league}&date=${selected}`, 120000);
  const when = selected === today ? `Today · ${dayParts(selected).monthDay}` : `${dayParts(selected).weekday} · ${dayParts(selected).monthDay}`;
  const tickets = card.data?.tickets ?? [];
  const lead = tickets[0];

  return (
    <div className="space-y-8">
      <PageHeader
        kicker={when}
        title="Parlay card"
        lede="Anytime touchdown tickets, including a six-player long shot, sit next to spread tickets from the model's side. A chance that sits far from the price stays off."
        action={<LeagueToggle value={league} basePath="/parlays" date={selected} today={today} />}
      />

      {days.data ? (
        <DayStrip today={today} dates={days.data.dates} selected={selected} basePath="/parlays" league={league} />
      ) : null}

      {card.error ? (
        <div className="plate border-danger/40 bg-danger/10 p-4 text-sm text-paper">The card didn’t load. {card.error}</div>
      ) : null}

      {card.data && card.data.games_on_slate === 0 ? (
        <EmptyDay
          selected={selected}
          today={today}
          next={next}
          href={next ? withQuery("/parlays", { league, date: next === today ? undefined : next }) : null}
        />
      ) : null}

      {card.data && card.data.games_on_slate > 0 && tickets.length > 0 ? (
        <>
          <div className="grid gap-3 sm:grid-cols-3">
            <Stat label="Tickets" value={String(tickets.length).padStart(2, "0")} hint="Built for this slate" />
            <Stat label="In play" value={String(card.data.eligible_legs).padStart(2, "0")} hint="Legs close to the number" />
            <Stat label="Lead" value={lead ? pct(lead.joint_probability, lead.joint_probability < 0.1 ? 1 : 0) : "—"} hint={lead?.title ?? ""} />
          </div>
          <ParlayBoard tickets={tickets} />
          <p className="max-w-3xl text-xs leading-relaxed text-mute">
            Built {kickoffParts(card.data.built_at).time}. {card.data.note}
          </p>
        </>
      ) : null}

      {card.data && card.data.games_on_slate > 0 && tickets.length === 0 && !card.error ? (
        <div className="plate px-5 py-6">
          <p className="font-display text-3xl uppercase leading-none">Nothing cleared</p>
          <p className="mt-3 max-w-xl text-sm text-mute">
            A leg joins when it beats a fresh price and the chance sits near that number. This slate does not have two of those in different games.
          </p>
        </div>
      ) : null}

      <p className="max-w-3xl text-xs leading-relaxed text-mute">
        {card.data?.disclaimer || "Research calls from the same models as the rest of the desk. Not a guarantee and not a sportsbook."}
      </p>
    </div>
  );
}

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="plate px-4 py-4">
      <div className="font-mono text-[10px] uppercase tracking-[0.22em] text-mute">{label}</div>
      <div className="mt-2 truncate font-display text-4xl uppercase leading-none tracking-wide text-paper">{value}</div>
      {hint ? <div className="mt-2 truncate font-mono text-[10px] uppercase tracking-[0.16em] text-mute">{hint}</div> : null}
    </div>
  );
}

async function load<T>(path: string, timeoutMs: number): Promise<{ data: T | null; error: string | null }> {
  try {
    return { data: await apiGet<T>(path, timeoutMs), error: null };
  } catch (e) {
    return { data: null, error: e instanceof Error ? e.message : "unavailable" };
  }
}
