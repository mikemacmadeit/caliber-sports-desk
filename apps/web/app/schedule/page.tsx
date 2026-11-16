import Link from "next/link";
import { CallCard } from "@/components/CallCard";
import { DayStrip, EmptyDay } from "@/components/DayStrip";
import { LeagueToggle } from "@/components/LeagueToggle";
import { PageHeader } from "@/components/PageHeader";
import { SlateBoard } from "@/components/SlateBoard";
import { TodayPropPicks } from "@/components/TodayPropPicks";
import { TodayTickets } from "@/components/TodayTickets";
import type { AtsRow } from "@/components/AtsBoard";
import { apiGet, type ParlayCard, type PropPickCard, type SlateGame, type SlateResponse } from "@/lib/api";
import { validDay, validLeague, dayParts, slateToday, type SlateDays, withQuery } from "@/lib/days";
import { callsForDate } from "@/lib/atsDay";

export default async function TodayPage({
  searchParams,
}: {
  searchParams: Promise<{ league?: string; date?: string }>;
}) {
  const params = await searchParams;
  const league = validLeague(params.league);
  const dateParam = validDay(params.date);
  const selectedEarly = dateParam || slateToday();
  let data: SlateResponse | null = null;
  let days: SlateDays | null = null;
  let calls: AtsRow[] = [];
  let propCard: PropPickCard | null = null;
  let ticketCard: ParlayCard | null = null;
  let error: string | null = null;
  try {
    const [dayList, slate, board, picks, tickets] = await Promise.all([
      apiGet<SlateDays>(`/v1/days?league=${league}`, 8000).catch(() => null),
      apiGet<SlateResponse>(`/v1/slate?league=${league}&date=${selectedEarly}`, 8000).catch(() => null),
      apiGet<{ rows: AtsRow[] }>(`/v1/ats?league=${league}&sort=kickoff&date=${selectedEarly}`, 8000).catch(() => null),
      apiGet<PropPickCard>(`/v1/props/picks?league=${league}&date=${selectedEarly}`, 8000).catch(() => null),
      apiGet<ParlayCard>(`/v1/parlays/card?league=${league}&date=${selectedEarly}`, 8000).catch(() => null),
    ]);
    days = dayList;
    if (!slate || !board) error = "Some schedule or matchup information is temporarily unavailable.";
    data = slate;
    calls = await callsForDate(board?.rows ?? [], selectedEarly, league);
    propCard = picks;
    ticketCard = tickets;
  } catch (e) {
    error = error || (e instanceof Error ? e.message : "API unavailable");
  }
  const today = days?.today ?? selectedEarly;
  const selected = dateParam || today;
  const next = days?.dates.find((d) => d > selected) ?? null;
  const visible = [...(data?.games ?? [])].sort(byKick);
  const openCalls = calls.filter((row) => row.game_status !== "final");
  const settledCalls = calls.filter((row) => row.game_status === "final");
  const shownOpen = openCalls.slice(0, 8);
  const when = selected
    ? selected === today
      ? `Today · ${dayParts(selected).monthDay}`
      : `${dayParts(selected).weekday} · ${dayParts(selected).monthDay}`
    : "Slate";

  return (
    <div className="space-y-12">
      <PageHeader
        kicker={when}
        title="The full slate"
        lede="Game schedules, spread research, published props, and parlay research."
        action={<LeagueToggle value={league} basePath="/schedule" date={selected} today={today} />}
      />
      {days ? <DayStrip today={today} dates={days.dates} selected={selected} basePath="/schedule" league={league} /> : null}

      {error && (
        <div className="plate border-danger/40 bg-danger/10 p-4 text-sm text-paper">
          {error} <a className="underline" href={withQuery("/schedule", { league, date: selected })}>Try again</a>
        </div>
      )}

      {data && visible.length === 0 ? (
        <EmptyDay
          selected={selected}
          today={today}
          next={next}
          href={next ? withQuery("/schedule", { league, date: next === today ? undefined : next }) : null}
        />
      ) : null}

      {shownOpen.length > 0 ? (
        <section className="space-y-4">
          <Section title="Matchup research" meta={`${openCalls.length} still to come`} href={withQuery("/ats", { league, sort: "kickoff" })} />
          <div className="grid gap-3 md:grid-cols-2">
            {shownOpen.map((row) => (
              <CallCard key={row.game_id} row={row} />
            ))}
          </div>
          {openCalls.length > shownOpen.length ? (
            <Link href={withQuery("/ats", { league, sort: "kickoff" })} className="inline-block font-display text-xl uppercase tracking-wide text-gold">
              Rest of the card →
            </Link>
          ) : null}
        </section>
      ) : null}

      {selected ? (
        <section className="space-y-4">
          <Section title="Published opportunities" meta="Verified prices" href={withQuery("/props", { league, date: selected === today ? undefined : selected })} />
          <TodayPropPicks league={league} date={selected} initialCard={propCard} />
        </section>
      ) : null}

      {selected ? (
        <section className="space-y-4">
          <Section title="Parlay research" meta="Built from this day" href={withQuery("/parlays", { league, date: selected === today ? undefined : selected })} />
          <TodayTickets league={league} date={selected} initialCard={ticketCard} />
        </section>
      ) : null}

      {settledCalls.length > 0 ? (
        <section className="space-y-4">
          <Section title="Recent matchup research" meta={`${settledCalls.length} finals`} href="/track-record" />
          <div className="grid gap-3 md:grid-cols-2">
            {settledCalls.slice(0, 4).map((row) => (
              <CallCard key={row.game_id} row={row} />
            ))}
          </div>
        </section>
      ) : null}

      {data && visible.length > 0 ? (
        <section className="space-y-4">
          <Section title="The schedule" meta={`${visible.length} games`} />
          <SlateBoard games={visible} versions={data.model_versions} />
        </section>
      ) : null}
    </div>
  );
}

function byKick(a: SlateGame, b: SlateGame) {
  return +new Date(a.kickoff_utc) - +new Date(b.kickoff_utc);
}

function Section({ title, meta, href }: { title: string; meta: string; href?: string }) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-3 border-b border-white/10 pb-3">
      <h2 className="font-display text-4xl uppercase leading-none tracking-wide">{title}</h2>
      {href ? (
        <Link href={href} className="font-display text-xl uppercase tracking-wide text-gold">
          {meta} →
        </Link>
      ) : (
        <p className="font-mono text-[11px] uppercase tracking-[0.16em] text-mute">{meta}</p>
      )}
    </div>
  );
}
