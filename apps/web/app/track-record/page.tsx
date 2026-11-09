import Link from "next/link";
import type { AtsRow } from "@/components/AtsBoard";
import { PageHeader } from "@/components/PageHeader";
import { TodayPropPicks } from "@/components/TodayPropPicks";
import { slateToday } from "@/lib/days";
import { apiGet } from "@/lib/api";
import { gradeWord, ledgerLines } from "@/lib/calls";
import { kickoffParts } from "@/lib/format";

export const metadata = {
  title: "Ledger — Caliber",
  description: "What the published calls did after the games ended.",
};

export default async function TrackRecordPage() {
  const board = await apiGet<{ rows: AtsRow[] }>("/v1/ats?league=all&sort=kickoff", 60000).catch(() => null);
  const rows = [...(board?.rows ?? [])].sort((a, b) => b.kickoff_utc.localeCompare(a.kickoff_utc));
  const finals = rows.filter((row) => row.game_status === "final");
  const open = rows.filter((row) => row.game_status !== "final");
  const winRight = finals.filter((row) => row.pick_result === "correct").length;
  const winWrong = finals.filter((row) => row.pick_result === "miss").length;
  const coverRight = finals.filter((row) => row.ats_pick_result === "correct").length;
  const coverWrong = finals.filter((row) => row.ats_pick_result === "miss").length;

  return (
    <div className="space-y-10">
      <PageHeader
        kicker="After the games"
        title="The ledger"
        lede="Each line is a call from the card, then what it did. Model scores and abstentions sit on Health."
      />

      <div className="grid gap-3 sm:grid-cols-2">
        <Record kicker="To win" right={winRight} wrong={winWrong} />
        <Record kicker="Against the number" right={coverRight} wrong={coverWrong} />
      </div>

      {finals.length === 0 && open.length === 0 ? (
        <p className="text-sm text-mute">No calls on the card yet.</p>
      ) : null}

      {finals.length > 0 ? (
        <section className="space-y-3">
          <h2 className="font-display text-4xl uppercase leading-none tracking-wide">Finals</h2>
          <ul className="divide-y divide-white/10 border-y border-white/10">
            {finals.slice(0, 40).map((row) => (
              <LedgerRow key={row.game_id} row={row} />
            ))}
          </ul>
        </section>
      ) : null}

      {open.length > 0 ? (
        <section className="space-y-3">
          <h2 className="font-display text-4xl uppercase leading-none tracking-wide">Still out</h2>
          <ul className="divide-y divide-white/10 border-y border-white/10">
            {open.slice(0, 20).map((row) => (
              <LedgerRow key={row.game_id} row={row} />
            ))}
          </ul>
        </section>
      ) : null}

      <section className="space-y-3">
        <h2 className="font-display text-4xl uppercase leading-none tracking-wide">Prop calls still out</h2>
        <TodayPropPicks league="all" date={slateToday()} pending />
      </section>

      <Link href="/data-quality" className="inline-block font-display text-xl uppercase tracking-wide text-gold">
        Model scores →
      </Link>
    </div>
  );
}

function Record({ kicker, right, wrong }: { kicker: string; right: number; wrong: number }) {
  const decided = right + wrong;
  const rate = decided === 0 ? null : `${Math.round((right / decided) * 100)}%`;
  return (
    <div className="plate px-4 py-4">
      <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-mute">{kicker}</p>
      <p className="mt-2 font-display text-5xl leading-none tracking-wide">
        {right} right <span className="text-mute">·</span> {wrong} wrong
      </p>
      {rate ? <p className="mt-2 font-mono text-[11px] uppercase tracking-[0.16em] text-gold">{rate}</p> : null}
    </div>
  );
}

function LedgerRow({ row }: { row: AtsRow }) {
  const lines = ledgerLines(row);
  const when = kickoffParts(row.kickoff_utc);
  return (
    <li>
      <Link href={`/games/${row.game_id}`} className="grid gap-1 py-3 sm:grid-cols-[9rem_1fr]">
        <span className="font-mono text-[10px] uppercase tracking-[0.16em] text-mute">
          {row.league_id} · {when.day}
        </span>
        <span>
          <span className="block text-sm text-paper/70">
            {row.away_team} at {row.home_team}
            {row.game_status === "final" && row.away_score != null && row.home_score != null
              ? ` · ${row.away_score}–${row.home_score}`
              : ""}
          </span>
          <span className={`mt-1 block text-sm ${tone(row.pick_result)}`}>{lines.win}</span>
          {lines.cover ? <span className={`mt-1 block text-sm ${tone(row.ats_pick_result)}`}>{lines.cover}</span> : null}
        </span>
      </Link>
    </li>
  );
}

function tone(result: AtsRow["pick_result"]) {
  if (result === "correct") return "text-gold";
  if (result === "miss") return "text-heat";
  if (gradeWord(result) === "pending") return "text-mute";
  return "text-paper";
}
