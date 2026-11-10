import Link from "next/link";
import { CallCard } from "@/components/CallCard";
import { DayStrip, EmptyDay } from "@/components/DayStrip";
import { LeagueToggle } from "@/components/LeagueToggle";
import { PageHeader } from "@/components/PageHeader";
import { SlateBoard } from "@/components/SlateBoard";
import { TodayPropPicks } from "@/components/TodayPropPicks";
import { TodayTickets } from "@/components/TodayTickets";
import type { AtsRow } from "@/components/AtsBoard";
import { apiGet, type SlateGame, type SlateResponse } from "@/lib/api";
import { dayParts, type SlateDays, withQuery } from "@/lib/days";
import { kickoffDate } from "@/lib/format";

export default async function TodayPage({
  searchParams,
}: {
  searchParams: Promise<{ league?: string; date?: string }>;
}) {
  const { league = "all", date: dateParam } = await searchParams;
  let data: SlateResponse | null = null;
  let days: SlateDays | null = null;
  let calls: AtsRow[] = [];
  let error: string | null = null;
  try {
    days = await apiGet<SlateDays>(`/v1/days?league=${league}`);
    // If today is dark, land on the next day that actually has games.
    const fallback =
      days.dates.includes(days.today) ? days.today : (days.dates[0] ?? days.today);
    const selectedDate = dateParam || fallback;
    const [slate, board] = await Promise.all([
      apiGet<SlateResponse>(`/v1/slate?league=${league}&date=${selectedDate}`),
      apiGet<{ rows: AtsRow[] }>(`/v1/ats?league=${league}&sort=kickoff`, 30000).catch(() => ({ rows: [] as AtsRow[] })),
    ]);
    data = slate;
    calls = board.rows.filter((row) => kickoffDate(row.kickoff_utc) === selectedDate);
  } catch (e) {
    error = e instanceof Error ? e.message : "API unavailable";
  }
  const today = days?.today ?? "";
  const fallback =
    days && days.dates.includes(today) ? today : (days?.dates[0] ?? today);
  const selected = dateParam || fallback;
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
        title="Today"
        lede="The win, the number, and the tickets. The schedule sits under them."
        action={<LeagueToggle value={league} basePath="/" date={selected} today={today} />}
      />
      {days ? <DayStrip today={today} dates={days.dates} selected={selected} basePath="/" league={league} /> : null}

      {error && (
        <div className="plate border-danger/40 bg-danger/10 p-4 text-sm text-paper">
          Desk is dark. The API at localhost:8000 isn’t answering. Start it with{" "}
          <code className="font-mono text-gold">scripts/dev.ps1</code>. {error}
        </div>
      )}

      {data && visible.length === 0 ? (
        <EmptyDay
          selected={selected}
          today={today}
          next={next}
          href={next ? withQuery("/", { league, date: next === today ? undefined : next }) : null}
        />
      ) : null}

      {shownOpen.length > 0 ? (
        <section className="space-y-4">
          <Section title="The calls" meta={`${openCalls.length} still to come`} href={withQuery("/ats", { league, sort: "kickoff" })} />
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
          <Section title="The props" meta="One call per market" href={withQuery("/props", { league, date: selected === today ? undefined : selected })} />
          <TodayPropPicks league={league} date={selected} />
        </section>
      ) : null}

      {selected ? (
        <section className="space-y-4">
          <Section title="The tickets" meta="Built from this day" href={withQuery("/parlays", { league, date: selected === today ? undefined : selected })} />
          <TodayTickets league={league} date={selected} />
        </section>
      ) : null}

      {settledCalls.length > 0 ? (
        <section className="space-y-4">
          <Section title="What happened" meta={`${settledCalls.length} finals`} href="/track-record" />
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
