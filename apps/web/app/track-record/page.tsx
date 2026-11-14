import Link from "next/link";
import { PageHeader } from "@/components/PageHeader";
import { apiGet } from "@/lib/api";

type Summary = { league: string; market: string; win: number; loss: number; push: number; void: number; pending: number; units: number; roi: number | null };
type RecordRow = { pick_id: string; sentence: string; league_id: string; market_id: string; published_at: string; bookmaker: string; book_price: number; p: number; result: string; units: number | null; forecast?: { model_version?: string } };
type History = { rows: RecordRow[]; summaries: Summary[]; total: number; methodology: string };
const markets = [["all", "Published props"], ["anytime_td", "Anytime TD"], ["receptions", "Receptions"], ["rushing_yards", "Rushing yards"], ["receiving_yards", "Receiving yards"], ["point_spread", "ATS research"]];
export default async function TrackRecordPage({ searchParams }: { searchParams: Promise<{ league?: string; market?: string; page?: string }> }) {
  const params = await searchParams;
  const league = ["all", "nfl", "fbs"].includes(params.league || "") ? params.league! : "all";
  const market = markets.some(([key]) => key === params.market) ? params.market! : "all";
  const page = Math.max(0, Math.min(10000, Number.parseInt(params.page || "0", 10) || 0));
  const url = new URLSearchParams({ league, market, offset: String(page * 50), limit: "50" });
  const data = await apiGet<History>(`/v1/published/history?${url}`, 10000).catch(() => null);
  const href = (p: number, lg = league, mk = market) => `/track-record?${new URLSearchParams({ league: lg, market: mk, page: String(p) })}`;
  return <div className="space-y-8">
    <PageHeader kicker="Original picks · original prices" title="The permanent record" lede="Published before kickoff. Graded against recorded results. Losing picks stay visible." />
    <nav className="flex flex-wrap gap-3" aria-label="League">{["all", "nfl", "fbs"].map(lg => <Link key={lg} href={href(0, lg)} className={lg === league ? "text-gold" : "text-mute"}>{lg.toUpperCase()}</Link>)}</nav>
    <nav className="flex flex-wrap gap-3 text-sm" aria-label="Market">{markets.map(([mk, label]) => <Link key={mk} href={href(0, league, mk)} className={mk === market ? "text-gold" : "text-mute"}>{label}</Link>)}</nav>
    {!data ? <p role="alert">The record is temporarily unavailable. Results have not been replaced with zeros.</p> : <>
      <p className="max-w-3xl text-sm text-mute">{data.methodology}</p>
      <div className="grid gap-3 md:grid-cols-2">{data.summaries.map(s => <article key={`${s.league}-${s.market}`} className="plate p-5">
        <p className="font-mono text-xs uppercase text-mute">{s.league} · {s.market.replaceAll("_", " ")}</p>
        <p className="mt-3 font-display text-3xl">{s.win} wins · {s.loss} losses</p>
        <p className="mt-2 text-gold">{s.units >= 0 ? "+" : ""}{s.units.toFixed(2)} units · ROI {s.roi == null ? "—" : `${(s.roi * 100).toFixed(1)}%`}</p>
        <p className="mt-2 text-sm text-mute">{s.push} pushes · {s.void} voids · {s.pending} pending</p>
      </article>)}</div>
      {data.total === 0 ? <p className="plate p-6">No published records yet for this filter. History starts when the first qualifying pick is saved before kickoff.</p> : null}
      <div className="space-y-3">{data.rows.map(row => <article key={row.pick_id} className="plate p-5">
        <p className="text-xs text-mute">{new Date(row.published_at).toLocaleString("en-US", { timeZone: "America/Chicago" })} Central · {row.league_id.toUpperCase()}</p>
        <p className="mt-2">{row.sentence}</p>
        <p className="mt-2 text-sm text-mute">{row.bookmaker} · {row.book_price > 0 ? "+" : ""}{row.book_price} · {Math.round(row.p * 100)}% at publication</p>
        <p className="mt-2 text-gold">{row.result.toUpperCase()}{row.units != null ? ` · ${row.units >= 0 ? "+" : ""}${row.units.toFixed(2)} units` : ""}</p>
        <details className="mt-3 text-xs text-mute"><summary>Publication details</summary><p className="mt-2 break-all">ID: {row.pick_id}<br />Model: {row.forecast?.model_version || "Recorded with prediction"}</p></details>
      </article>)}</div>
      <nav className="flex gap-6">{page > 0 ? <Link href={href(page - 1)}>← Newer</Link> : null}{(page + 1) * 50 < data.total ? <Link href={href(page + 1)}>Older →</Link> : null}</nav>
    </>}
  </div>;
}
