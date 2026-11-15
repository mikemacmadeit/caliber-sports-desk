import Link from "next/link";
import savedSaturday from "@/lib/ledger/2026-11-14.json";
import { LedgerRecord, type LedgerCard } from "@/components/LedgerRecord";
import { PageHeader } from "@/components/PageHeader";
import { dayParts, slateToday } from "@/lib/days";

const SAVED: Record<string, LedgerCard & { rows: SavedRow[] }> = {
  "2026-11-14": savedSaturday,
};

type SavedRow = { league_id: string; result: string; units?: number | null };

export function unitsFor(league: string): Record<string, number> {
  const totals: Record<string, number> = {};
  for (const [date, card] of Object.entries(SAVED)) {
    const rows = league === "all" ? card.rows : (card.rows ?? []).filter((row) => row.league_id === league);
    const priced = rows.filter((row) => row.units != null && row.result !== "pending");
    if (!priced.length) continue;
    totals[date] = priced.reduce((sum, row) => sum + Number(row.units), 0);
  }
  return totals;
}

export default async function TrackRecordPage({
  searchParams,
}: {
  searchParams: Promise<{ league?: string; date?: string; month?: string }>;
}) {
  const params = await searchParams;
  const league = ["all", "nfl", "fbs"].includes(params.league || "") ? params.league! : "all";
  const selected = /^\d{4}-\d{2}-\d{2}$/.test(params.date || "") ? params.date! : previousDay(slateToday());
  const month = /^\d{4}-\d{2}$/.test(params.month || "") ? params.month! : selected.slice(0, 7);
  const when = dayParts(selected);
  const card = await loadCard(selected, league);

  return (
    <div className="space-y-8">
      <PageHeader
        kicker="Original picks · original prices"
        title="The permanent record"
        lede="Pick a day. The spreads, the prop calls, and every anytime touchdown the desk named stay here, including the ones that lost."
      />
      <nav className="flex flex-wrap gap-3" aria-label="League">
        {["all", "nfl", "fbs"].map((item) => (
          <Link key={item} href={leagueHref(item, selected, month)} className={item === league ? "text-gold" : "text-mute"}>
            {item.toUpperCase()}
          </Link>
        ))}
      </nav>
      <LedgerRecord
        month={month}
        selected={selected}
        league={league}
        title={`${when.weekday} · ${when.monthDay}`}
        card={card}
        units={unitsFor(league)}
      />
    </div>
  );
}

function leagueHref(league: string, date: string, month: string) {
  const params = new URLSearchParams({ date, month });
  if (league !== "all") params.set("league", league);
  return `/track-record?${params}`;
}

async function loadCard(date: string, league: string): Promise<LedgerCard | null> {
  const saved = SAVED[date];
  if (saved) {
    const rows = league === "all" ? saved.rows : (saved.rows ?? []).filter((row) => row.league_id === league);
    return { ...saved, rows };
  }
  const api = process.env.API_URL || process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
  try {
    const res = await fetch(`${api}/v1/published/day?date=${date}&league=${league}`, {
      cache: "no-store",
      signal: AbortSignal.timeout(12000),
    });
    if (!res.ok) return null;
    return (await res.json()) as LedgerCard;
  } catch {
    return null;
  }
}

function previousDay(iso: string) {
  const [year, month, day] = iso.split("-").map(Number);
  const date = new Date(Date.UTC(year, month - 1, day));
  date.setUTCDate(date.getUTCDate() - 1);
  return date.toISOString().slice(0, 10);
}
