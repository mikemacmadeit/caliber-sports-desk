"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { type DeskCard, type DeskData, type SavedSelection, odds, pct, isCurrent, isOpen, priceCheck } from "@/lib/desk";

const STORAGE = "caliber.desk.v1";
const MARKETS = [["", "All props"], ["anytime_td", "Anytime TD"], ["receptions", "Receptions"], ["receiving_yards", "Receiving yards"], ["rushing_yards", "Rushing yards"]];
function time(value: string | null) {
  return value ? new Date(value).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short" }) : "Not verified";
}

export function TodayDesk({ initial, league, date }: { initial: DeskData | null; league: string; date: string }) {
  const [data, setData] = useState(initial);
  const [book, setBook] = useState("");
  const [market, setMarket] = useState("");
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [tab, setTab] = useState("all");
  const [page, setPage] = useState(0);
  const [saved, setSaved] = useState<SavedSelection[]>([]);
  const [ready, setReady] = useState(false);
  const [storageError, setStorageError] = useState(false);
  const [error, setError] = useState(false);
  const [loading, setLoading] = useState(false);
  const [now, setNow] = useState(0);
  const [revision, setRevision] = useState(0);
  const [check, setCheck] = useState<DeskCard | null>(null);
  const [enteredPrice, setEnteredPrice] = useState("");
  const [announcement, setAnnouncement] = useState("");
  const requestId = useRef(0);
  const lastQuery = useRef("");

  useEffect(() => {
    setNow(Date.now());
    try {
      const value = JSON.parse(localStorage.getItem(STORAGE) || "{}");
      if (typeof value.book === "string") setBook(value.book.slice(0, 100));
      if (Array.isArray(value.saved)) setSaved(value.saved.filter((s: SavedSelection) => s && typeof s.id === "string" && /^[a-f0-9]{24}$/.test(s.id) && typeof s.player === "string" && typeof s.kickoff === "string").slice(0, 100));
    } catch { setStorageError(true); }
    setReady(true);
    const clock = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(clock);
  }, []);
  useEffect(() => {
    if (!ready) return;
    try { localStorage.setItem(STORAGE, JSON.stringify({ book, saved })); } catch { setStorageError(true); }
  }, [book, saved, ready]);
  useEffect(() => { const timer = setTimeout(() => { setQuery(search); setPage(0); }, 250); return () => clearTimeout(timer); }, [search]);
  const savedIds = saved.map(s => s.id).join(",");
  const refresh = useCallback(() => setRevision(r => r + 1), []);
  useEffect(() => {
    if (!ready) return;
    const controller = new AbortController();
    const id = ++requestId.current;
    const params = new URLSearchParams({ league, date, offset: String(page * 30), limit: "30" });
    if (book) params.set("book", book);
    if (market) params.set("market", market);
    if (query) params.set("search", query);
    if (tab === "saved") params.set("saved", savedIds);
    setLoading(true);
    // Remove old-filter results immediately; never relabel a different book's prices.
    if (lastQuery.current !== params.toString()) setData(null);
    lastQuery.current = params.toString();
    const timer = setTimeout(() => controller.abort(), 15000);
    fetch(`/api/backend/v1/desk?${params}`, { cache: "no-store", signal: controller.signal })
      .then(async response => { if (!response.ok) throw new Error("unavailable"); return response.json() as Promise<DeskData>; })
      .then(value => { if (id === requestId.current) { setData(value); setError(false); setNow(Date.now()); setAnnouncement(`Updated. ${value.total} research selections.`); } })
      .catch(() => { if (id === requestId.current && !controller.signal.aborted) setError(true); else if (id === requestId.current) setError(true); })
      .finally(() => { clearTimeout(timer); if (id === requestId.current) setLoading(false); });
    return () => { ++requestId.current; controller.abort(); clearTimeout(timer); };
  }, [league, date, book, market, query, tab, page, ready, revision, savedIds]);
  useEffect(() => {
    const poll = setInterval(() => { if (document.visibilityState === "visible") refresh(); }, 60000);
    const visible = () => { if (document.visibilityState === "visible") refresh(); };
    document.addEventListener("visibilitychange", visible);
    return () => { clearInterval(poll); document.removeEventListener("visibilitychange", visible); };
  }, [refresh]);
  useEffect(() => { setPage(0); setCheck(null); }, [league, date]);

  function toggleSave(row: DeskCard) {
    const exists = saved.some(s => s.id === row.id);
    if (!exists && saved.length >= 100) { setAnnouncement("Your watchlist is full. Remove a selection to save another."); return; }
    setSaved(current => exists ? current.filter(s => s.id !== row.id) : [...current, {
      id: row.id, player: row.player, pick: row.pick, kickoff: row.kickoff, book: row.book,
      price: row.price, game_id: row.game_id, saved_at: new Date().toISOString(),
    }]);
    setAnnouncement(exists ? `${row.player} removed from saved research.` : `${row.player} saved on this device.`);
  }
  function changeFilter(fn: () => void) { fn(); setPage(0); setCheck(null); }
  const rows = (data?.rows || []).filter(r => !now || isOpen(r, now));
  const currentCheck = check && data?.rows.find(r => r.id === check.id && r.book === check.book);
  const checked = currentCheck && now ? priceCheck(currentCheck, enteredPrice, now) : null;
  const shownBooks = data?.books || initial?.books || [];
  const sameGame = Object.values(saved.reduce<Record<string, number>>((acc, r) => { acc[r.game_id] = (acc[r.game_id] || 0) + 1; return acc; }, {})).some(n => n > 1);

  return <div className="space-y-6">
    <div className="sr-only" role="status" aria-live="polite">{announcement}</div>
    <section className="desk-intro grid gap-5 border border-white/10 p-5 md:grid-cols-[1fr_auto] md:p-7">
      <div><p className="kicker">Your game-day research</p><h2 className="mt-3 font-display text-3xl uppercase md:text-4xl">The matchup. The price. The reasons.</h2><p className="mt-2 max-w-xl text-sm text-mute">Compare the available evidence, follow the players you care about, and see when a price stops making sense.</p></div>
      <div className="flex items-center gap-7 border-t border-white/10 pt-4 md:border-l md:border-t-0 md:pl-7 md:pt-0">
        <Metric label="Upcoming games" value={data?.games.length} />
        <Metric label="Research selections" value={data?.total} />
        <Metric label="Meet checks" value={data?.qualified} />
      </div>
    </section>
    <div className="flex flex-wrap items-center justify-between gap-3 text-xs text-mute">
      <p>{loading ? "Checking your board…" : error ? "Updates unavailable" : data?.status === "warming" ? "Some markets are still being prepared" : data?.status === "delayed" ? "Board updates are delayed" : "Research board"} · {data?.updated_at ? `Built ${time(data.updated_at)}` : "Awaiting a completed update"}</p>
      <button onClick={refresh} disabled={loading} className="min-h-10 px-3 text-gold disabled:opacity-40">{loading ? "Refreshing…" : "Refresh prices ↻"}</button>
    </div>
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-[1fr_1fr_1.4fr]">
      <label className="text-xs text-mute">Sportsbook<select aria-label="Sportsbook" value={book} onChange={e => changeFilter(() => setBook(e.target.value))} className="field-input mt-2"><option value="">Best observed price</option>{book && !shownBooks.includes(book) && <option value={book}>{book}</option>}{shownBooks.map(b => <option key={b}>{b}</option>)}</select></label>
      <label className="text-xs text-mute">Market<select aria-label="Market" value={market} onChange={e => changeFilter(() => setMarket(e.target.value))} className="field-input mt-2">{MARKETS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      <label className="text-xs text-mute">Player or team<input type="search" aria-label="Player or team" value={search} onChange={e => setSearch(e.target.value)} placeholder="Search this slate…" maxLength={100} className="field-input mt-2" /></label>
    </div>
    <div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/10">
      <div className="flex gap-5">{[["all", "Research board"], ["saved", `Saved (${saved.length})`]].map(([value, label]) => <button key={value} aria-pressed={tab === value} onClick={() => changeFilter(() => setTab(value))} className={`min-h-12 border-b-2 text-sm ${tab === value ? "border-gold text-gold" : "border-transparent text-mute"}`}>{label}</button>)}</div>
      <Link href="/track-record" className="py-3 text-sm text-gold">View published results →</Link>
    </div>
    {storageError && <p role="alert" className="text-sm text-amber-300">This browser could not store your preferences. Saved research may be lost when you leave.</p>}
    {tab === "saved" && <div className="space-y-3"><p className="text-xs text-mute">Saved on this device. Live matches use the selected date and filters. Updates appear here while the page is open.</p>
      {sameGame && <p className="border-l-2 border-amber-300 pl-3 text-sm text-amber-200">You saved multiple selections from the same game. Their outcomes can be related; individual probabilities do not establish a parlay’s chance of winning.</p>}
      {saved.length > 0 && <details className="text-sm"><summary className="cursor-pointer text-mute">All saved research · including other dates</summary><ul className="mt-3 divide-y divide-white/10">{saved.map(s => <li key={s.id} className="flex items-center justify-between gap-3 py-3"><div><p>{s.player} · {s.pick}</p><p className="mt-1 text-xs text-mute">{time(s.kickoff)} · Saved at {s.book} {odds(s.price)} · Historical bookmark</p></div><button aria-label={`Remove saved ${s.player}`} className="min-h-11 px-3 text-mute" onClick={() => setSaved(v => v.filter(r => r.id !== s.id))}>Remove</button></li>)}</ul></details>}
    </div>}
    {error && !loading && <div role="alert" className="plate p-6"><p>We couldn’t verify this board. Current opportunities are paused.</p><button className="mt-3 text-gold" onClick={refresh}>Try again →</button></div>}
    {loading && !data && <div aria-label="Loading research" className="grid gap-4 md:grid-cols-2">{[0, 1].map(n => <div key={n} className="plate h-64 animate-pulse p-6"><div className="h-5 w-2/3 bg-white/5" /><div className="mt-5 h-20 bg-white/5" /></div>)}</div>}
    {!loading && !error && data && rows.length === 0 && <div className="plate space-y-3 p-7"><h3 className="font-display text-3xl uppercase">{tab === "saved" ? "No saved matches on this board" : data.games.length ? "Research is being prepared" : "No upcoming games on this date"}</h3><p className="max-w-xl text-sm text-mute">{tab === "saved" ? "Save a selection from the research board, or change the date and filters to find your bookmarks." : book || market || query ? "No selections match these filters. Try another sportsbook, market, or player." : data.games.length ? "The schedule is available below. Detailed selections appear after their data has been checked." : "Choose another date above to explore the next slate."}</p>{(book || market || query) && <button className="text-sm text-gold" onClick={() => { setBook(""); setMarket(""); setSearch(""); setQuery(""); setPage(0); }}>Clear filters →</button>}</div>}
    {!loading && !error && data && data.qualified === 0 && rows.length > 0 && <details className="border-l-2 border-amber-300 bg-amber-300/5 px-4 py-3"><summary className="cursor-pointer text-sm text-amber-200">Research is available. No selection currently clears every check.</summary><ul className="mt-3 space-y-2 text-sm text-mute">{data.blockers.map(b => <li key={b.reason}>{b.reason} <span className="text-paper">({b.count})</span></li>)}</ul></details>}
    {!error && <div className="grid items-start gap-4 xl:grid-cols-2">{rows.map(row => {
      const fresh = now > 0 && isCurrent(row, now);
      const eligible = fresh && row.qualification.eligible;
      const savedHere = saved.some(s => s.id === row.id);
      return <article key={row.id} className={`desk-card plate overflow-hidden ${eligible ? "border-gold/30" : ""}`}>
        <div className="flex items-center justify-between gap-2 border-b border-white/10 px-5 py-3"><p className="font-mono text-[10px] uppercase tracking-wider text-mute">{row.league.toUpperCase()} · {row.matchup}</p><span className={`text-[11px] ${eligible ? "text-gold" : "text-mute"}`}>{eligible ? "Meets research checks" : fresh ? "Research only" : "Price check needed"}</span></div>
        <div className="p-5"><p className="text-xs text-mute">{time(row.kickoff)}</p><div className="mt-3 flex items-start justify-between gap-4"><div><Link href={`/games/${encodeURIComponent(row.game_id)}`} className="font-display text-3xl uppercase leading-none hover:text-gold">{row.player}</Link><p className="mt-2 text-sm text-paper/90">{row.pick}</p><p className="mt-1 text-xs text-mute">{row.team} vs {row.opponent}</p></div><div className="text-right"><p className={`font-mono text-2xl ${fresh ? "text-gold" : "text-mute"}`}>{odds(row.price)}</p><p className="mt-1 max-w-28 text-xs text-mute">{row.book || "No book"}</p></div></div>
        <div className="mt-5 grid grid-cols-3 divide-x divide-white/10 border-y border-white/10 py-4"><SmallStat label="Model estimate" value={pct(row.probability)} /><SmallStat label="Price break-even" value={pct(row.break_even)} /><SmallStat label="Minimum price" value={eligible ? odds(row.qualification.minimum_american_odds) : "—"} /></div>
        <p className="mt-2 text-[11px] text-mute">Experimental estimates · minimum price applies only while all checks pass.</p>
        {row.changes.length > 0 && <div className="mt-4 border-l-2 border-signal bg-signal/5 px-3 py-2"><p className="text-xs font-medium text-signal">What changed</p>{row.changes.map(c => <p key={`${c.kind}-${c.at}`} className="mt-1 text-xs text-paper/80">{c.kind === "probability" ? `Model estimate ${pct(Number(c.before))} → ${pct(Number(c.after))}` : c.kind === "price" ? `${c.bookmaker} ${odds(Number(c.before))} → ${odds(Number(c.after))}` : `Availability ${c.before} → ${c.after}`} <span className="text-mute">· {time(c.at)}</span></p>)}</div>}
        <div className="mt-5"><h3 className="text-xs font-semibold uppercase tracking-widest text-paper/80">Behind the estimate</h3>{row.evidence.length ? <dl className="mt-3 grid grid-cols-2 gap-3">{row.evidence.map(f => <div key={f.label}><dt className="text-xs text-mute">{f.label}</dt><dd className="mt-1 text-sm">{f.value}</dd><dd className="mt-1 text-[10px] text-mute">{f.source}</dd></div>)}</dl> : <p className="mt-2 text-sm text-mute">Detailed usage evidence is unavailable.</p>}</div>
        <details className="mt-5 border-t border-white/10 pt-4"><summary className="cursor-pointer text-sm text-mute">Risks, sources & sportsbook prices</summary><p className="mt-3 text-xs text-paper/80">Availability: {row.availability.replaceAll("_", " ")}</p><ul className="mt-3 list-disc space-y-2 pl-4 text-xs text-mute">{row.risks.map(r => <li key={r}>{r}</li>)}</ul>{row.offers.length > 0 && <div className="mt-4 space-y-2">{row.offers.map(o => <div key={o.book} className="flex justify-between gap-3 text-xs"><span>{o.book} <span className="text-mute">· {time(o.verified_at)}</span></span><span className={o.current && fresh && !!o.valid_until && Date.parse(o.valid_until) > now ? "text-gold" : "text-mute"}>{odds(o.price)}{!o.current || !fresh || !o.valid_until || Date.parse(o.valid_until) <= now ? " · recheck" : ""}</span></div>)}</div>}<p className="mt-4 break-words text-[10px] text-mute">Model: {row.model}<br />Prediction: {time(row.prediction_at)}<br />Data cutoff: {time(row.data_cutoff)}<br />Price observed: {time(row.qualification.verified_at)}</p></details>
        <div className="mt-5 flex flex-wrap gap-3"><button aria-pressed={savedHere} aria-label={`${savedHere ? "Unsave" : "Save"} ${row.player} ${row.pick}`} onClick={() => toggleSave(row)} className={`min-h-11 border px-4 text-sm ${savedHere ? "border-gold text-gold" : "border-white/15 text-paper"}`}>{savedHere ? "✓ Saved" : "+ Save research"}</button><button disabled={!fresh} onClick={() => { setCheck(row); setEnteredPrice(String(row.price || "")); }} className="min-h-11 px-3 text-sm text-gold disabled:text-mute disabled:opacity-40">Check my price →</button></div>
        {check?.id === row.id && <div className="mt-4 border border-white/15 bg-ink-950 p-4"><div className="flex justify-between gap-3"><label htmlFor={`price-${row.id}`} className="text-sm">Your odds for this exact selection</label><button aria-label="Close price check" onClick={() => setCheck(null)} className="px-2 text-mute">✕</button></div><input id={`price-${row.id}`} inputMode="text" maxLength={8} value={enteredPrice} onChange={e => setEnteredPrice(e.target.value)} className="field-input mt-3" /><p role="status" className={`mt-3 text-sm ${checked?.value ? "text-gold" : "text-amber-200"}`}>{checked?.label || "Waiting for current verification."}</p></div>}
        </div></article>;
    })}</div>}
    {!loading && data && data.total > 30 && <nav aria-label="Research pages" className="flex items-center justify-between"><button disabled={page === 0} onClick={() => setPage(p => p - 1)} className="min-h-11 px-3 text-gold disabled:opacity-30">← Previous</button><p className="text-xs text-mute">{page * 30 + 1}–{Math.min((page + 1) * 30, data.total)} of {data.total}</p><button disabled={(page + 1) * 30 >= data.total} onClick={() => setPage(p => p + 1)} className="min-h-11 px-3 text-gold disabled:opacity-30">Next →</button></nav>}
    {data && data.games.length > 0 && <section className="border-t border-white/10 pt-6"><h2 className="font-display text-3xl uppercase">Upcoming on this slate</h2><div className="mt-4 grid gap-2 sm:grid-cols-2 xl:grid-cols-3">{data.games.map(g => <Link key={g.id} href={`/games/${encodeURIComponent(g.id)}`} className="flex items-center justify-between gap-3 border border-white/10 p-4 text-sm hover:border-gold/40"><span>{g.matchup}<span className="mt-1 block text-xs text-mute">{g.league.toUpperCase()} · {time(g.kickoff)}</span></span><span className="text-gold">↗</span></Link>)}</div></section>}
    <p className="max-w-3xl text-xs leading-relaxed text-mute">Research estimates can be wrong. A completed data check is not proof of predictive accuracy. Prices are observed quotes and may change at the sportsbook. Saving research does not place or record a wager.</p>
  </div>;
}
function Metric({ label, value }: { label: string; value?: number }) { return <div><p className="font-display text-3xl text-paper md:text-4xl">{value ?? "—"}</p><p className="mt-1 max-w-20 text-[10px] uppercase tracking-wider text-mute">{label}</p></div>; }
function SmallStat({ label, value }: { label: string; value: string }) { return <div className="px-2 first:pl-0"><p className="text-[10px] text-mute">{label}</p><p className="mt-1 font-mono text-lg">{value}</p></div>; }
