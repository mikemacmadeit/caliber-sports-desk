import Link from "next/link";
import { DayStrip, EmptyDay } from "@/components/DayStrip";
import { LeagueToggle } from "@/components/LeagueToggle";
import { PageHeader } from "@/components/PageHeader";
import { PriceFreshnessBadge } from "@/components/PropTable";
import { apiGet, type PropRow } from "@/lib/api";
import { selectDeskTakes, valueAtPrice } from "@/lib/atdDeskTakes";
import { dayParts, slateToday, type SlateDays, withQuery } from "@/lib/days";
import { timesInHundred } from "@/lib/calls";
import { kickoffParts } from "@/lib/format";

export const metadata = {
  title: "Best bets — Caliber",
  description: "The desk’s strongest spread sides and multifactor Anytime TD picks for the day.",
};

type AtsRow = {
  game_id: string;
  league_id: string;
  kickoff_utc: string;
  home_team: string;
  away_team: string;
  pick_label?: string | null;
  pick_market?: string | null;
  pick_probability?: number | null;
  pick_chance_label?: string | null;
  pick_edge_points?: number | null;
  p_cover?: number | null;
  predicted_margin: number;
  predicted_home_score: number;
  predicted_away_score: number;
  home_handicap?: number | null;
  favored_side: string;
  model_vs_market?: number | null;
  game_status?: string | null;
  home_score?: number | null;
  away_score?: number | null;
  pick_result?: "correct" | "miss" | "push" | null;
  projection_withheld?: boolean;
};

type AtsBoard = {
  disclaimer: string;
  rows: AtsRow[];
};

const TOP_DESK_PICKS = 6;

export default async function BestBetsPage({
  searchParams,
}: {
  searchParams: Promise<{ league?: string; date?: string }>;
}) {
  const { league = "all", date: dateParam } = await searchParams;
  const days = await load<SlateDays>(`/v1/days?league=${league}`, 20000);
  const today = days.data?.today ?? slateToday();
  const selected = dateParam || today;
  const next = days.data?.dates.find((d) => d > selected) ?? null;
  const dateQuery = selected !== today ? selected : undefined;

  const [ats, atd] = await Promise.all([
    load<AtsBoard>(`/v1/ats?league=${league}&sort=cover_prob&side=all`, 120000),
    load<{ rows: PropRow[] }>(`/v1/props?market=anytime_td&league=${league}&date=${selected}`, 90000),
  ]);

  const scorerRows = selectDeskTakes(atd.data?.rows ?? []).slice(0, TOP_DESK_PICKS);
  const valueRows = valueAtPrice(atd.data?.rows ?? []);
  const total = scorerRows.length + valueRows.length;
  const when = selected === today ? `Today · ${dayParts(selected).monthDay}` : `${dayParts(selected).weekday} · ${dayParts(selected).monthDay}`;

  return (
    <div className="space-y-10">
      <PageHeader
        kicker={when}
        title="Best bets"
        lede="A research lean is not a best pick. Spreads stay unranked until the card can separate games. Anytime TD desk picks are the player most likely to score in each game."
        action={<LeagueToggle value={league} basePath="/best-bets" date={selected} today={today} />}
      />

      {days.data ? (
        <DayStrip today={today} dates={days.data.dates} selected={selected} basePath="/best-bets" league={league} />
      ) : null}

      {days.error && !days.data ? (
        <div className="plate border-danger/40 bg-danger/10 p-4 text-sm text-paper">
          The day list didn’t load. {days.error}
        </div>
      ) : null}

      {total > 0 ? (
        <div className="grid gap-3 sm:grid-cols-3">
          <Stat label="Shortlist" value={String(valueRows.length).padStart(2, "0")} hint="Fresh price beats the chance" />
          <Stat label="Desk picks" value={String(scorerRows.length).padStart(2, "0")} hint="One per game, most likely scorer" />
          <Stat
            label="Lead pick"
            value={scorerRows[0] ? (pct(scorerRows[0].forecast.p_estimate) ?? "—") : "None"}
            hint={scorerRows[0]?.player_name ?? "Nothing cleared the desk"}
          />
        </div>
      ) : null}

      <div className="grid items-start gap-8 lg:grid-cols-2">
        <section className="space-y-3">
          <SectionHead title="Spread shortlist" href={withQuery("/ats", { league, sort: "kickoff" })} link="Full card" />
          <div className="plate px-5 py-6">
            <p className="font-display text-3xl uppercase leading-none">Nothing strong enough</p>
            <p className="mt-3 max-w-xl text-sm text-mute">
              Every side in a league is stamped with the same old rate. That cannot say which game today is the stronger number. The full card still shows a research lean for each game.
            </p>
          </div>
        </section>

        <section className="space-y-3">
          <SectionHead
            title="At this price"
            href={withQuery("/anytime-td", { league, date: dateQuery })}
            link="Full board"
          />
          {valueRows.length > 0 ? (
            valueRows.map((row, index) =>
              index === 0 ? (
                <FeaturedScorer key={row.outcome_id} row={row} kicker="At this price" />
              ) : (
                <ScorerBox key={row.outcome_id} row={row} rank={index + 1} showPrice />
              ),
            )
          ) : (
            <div className="plate px-5 py-6">
              <p className="font-display text-3xl uppercase leading-none">No fresh price beats the chance</p>
              <p className="mt-3 text-sm text-mute">
                A player can be the most likely scorer and still be a bad price. Stale numbers stay off this list.
              </p>
            </div>
          )}
        </section>
      </div>

      {scorerRows.length > 0 ? (
        <section className="space-y-3">
          <SectionHead
            title="Desk picks"
            href={withQuery("/anytime-td", { league, date: dateQuery })}
            link="Full board"
          />
            {scorerRows.map((row, index) =>
              index === 0 ? (
                <FeaturedScorer key={row.outcome_id} row={row} kicker="Desk pick" />
              ) : (
                <ScorerBox key={row.outcome_id} row={row} rank={index + 1} />
              ),
            )}
          </section>
        ) : null}

      {atd.error ? (
        <div className="plate border-danger/40 bg-danger/10 p-4 text-sm text-paper">
          The Anytime TD list didn’t load. {atd.error}
        </div>
      ) : null}

      {total === 0 && !ats.error && !atd.error ? (
        <EmptyDay
          selected={selected}
          today={today}
          next={next}
          href={next ? withQuery("/best-bets", { league, date: next === today ? undefined : next }) : null}
        />
      ) : null}

      <p className="max-w-3xl text-xs leading-relaxed text-mute">
        {ats.data?.disclaimer ||
          "Research calls from the same models as the rest of the desk. Not a guarantee and not a sportsbook."}
      </p>
    </div>
  );
}

function SectionHead({ title, href, link }: { title: string; href: string; link: string }) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-3 border-b border-white/10 pb-2">
      <h2 className="font-display text-4xl uppercase leading-none tracking-wide">{title}</h2>
      <Link href={href} className="font-display text-xl uppercase tracking-wide text-gold">
        {link} →
      </Link>
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

function FeaturedScorer({ row, kicker = "Most likely to score" }: { row: PropRow; kicker?: string }) {
  const chance = pct(row.forecast.p_estimate);
  const price = american(row.book_price);
  const when = kickoffParts(row.kickoff_utc);
  return (
    <Link href={`/games/${row.game_id}`} className="plate group block px-5 py-5">
      <div className="flex items-start justify-between gap-3">
        <p className="font-mono text-[10px] uppercase tracking-[0.2em] text-mute">
          01 · {row.league_id} · anytime td · {when.time}
        </p>
        <span className="font-display text-4xl leading-none text-flare">{chance ?? "—"}</span>
      </div>
      <p className="mt-6 font-mono text-[10px] uppercase tracking-[0.18em] text-mute">{kicker}</p>
      <p className="mt-2 font-display text-4xl uppercase leading-none tracking-wide text-paper sm:text-5xl">{row.player_name}</p>
      <p className="mt-5 font-display text-2xl uppercase leading-none tracking-wide text-flare">Anytime TD</p>
      <p className="mt-3 text-sm text-paper/70">
        {row.team} vs {row.opponent}
        {price ? ` · ${price}` : ""}
      </p>
      <div className="mt-2">
        <PriceFreshnessBadge freshness={row.price_freshness || row.forecast.price_freshness} />
      </div>
      <p className="mt-5 text-sm leading-relaxed text-paper/80">
        {row.player_name} to score, {timesInHundred(row.forecast.p_estimate) ?? "chance unpublished"}
        {price ? `, pays ${price}` : ""}.
      </p>
      {situationLine(row.forecast.reason_codes) ? (
        <p className="mt-2 font-mono text-[10px] uppercase tracking-wider text-mute">
          {situationLine(row.forecast.reason_codes)}
        </p>
      ) : null}
    </Link>
  );
}

function ScorerBox({ row, rank, showPrice = false }: { row: PropRow; rank: number; showPrice?: boolean }) {
  const chance = pct(row.forecast.p_estimate);
  const price = american(row.book_price);
  const when = kickoffParts(row.kickoff_utc);
  return (
    <Link href={`/games/${row.game_id}`} className="plate group grid grid-cols-[auto_1fr_auto] items-center gap-3 px-4 py-3">
      <span className="font-display text-3xl leading-none text-gold/40">{String(rank).padStart(2, "0")}</span>
      <span className="min-w-0">
        <span className="block truncate font-display text-2xl uppercase leading-none tracking-wide text-paper">{row.player_name}</span>
        <span className="mt-1 block truncate font-mono text-[10px] uppercase tracking-[0.16em] text-mute">
          {row.team} vs {row.opponent} · {when.time}
          {showPrice && price ? ` · ${price}` : ""}
        </span>
        {situationLine(row.forecast.reason_codes) ? (
          <span className="mt-1 block truncate font-mono text-[10px] uppercase tracking-[0.16em] text-paper/60">
            {situationLine(row.forecast.reason_codes)}
          </span>
        ) : null}
        {showPrice ? (
          <span className="mt-1 block">
            <PriceFreshnessBadge freshness={row.price_freshness || row.forecast.price_freshness} />
          </span>
        ) : null}
      </span>
      <span className="text-right">
        <span className="block font-display text-2xl leading-none text-gold">{chance ?? "—"}</span>
        <span className="mt-1 block font-mono text-[10px] uppercase tracking-wider text-mute">TD</span>
      </span>
    </Link>
  );
}

function american(price: number | null | undefined) {
  if (price == null) return null;
  return price > 0 ? `+${price}` : String(price);
}

function situationLine(codes: string[] | undefined) {
  if (!codes?.length) return null;
  const bits: string[] = [];
  if (codes.includes("RULED_OUT")) bits.push("Out");
  if (codes.includes("CURRENT_ROLE")) bits.push("Backup, this week's touches");
  if (codes.includes("MATCHUP")) bits.push("This defense");
  if (codes.includes("WEATHER")) bits.push("Wind or rain");
  return bits.length ? bits.join(" · ") : null;
}

function pct(p: number | null | undefined) {
  if (p == null || Number.isNaN(p)) return null;
  return `${Math.round(p * 100)}%`;
}

async function load<T>(path: string, timeoutMs: number): Promise<{ data: T | null; error: string | null }> {
  try {
    return { data: await apiGet<T>(path, timeoutMs), error: null };
  } catch (e) {
    return { data: null, error: e instanceof Error ? e.message : "unavailable" };
  }
}
