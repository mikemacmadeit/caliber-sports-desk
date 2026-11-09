import type { GameDetail, PropRow } from "@/lib/api";
import { pct } from "@/lib/format";

type Feature = GameDetail["key_features"][number];

export function UsagePanel({ features, props }: { features: Feature[]; props: PropRow[] }) {
  const names = new Map(props.map((p) => [p.player_id, p.player_name]));
  const bestByPlayer = new Map<
    string,
    {
      id: string;
      name: string;
      sample: number;
      missingness: number;
      targets: number | null;
      rushShare: number | null;
      workloadDelta: number | null;
      recYpg: number | null;
      rushYpg: number | null;
    }
  >();

  for (const f of features) {
    if (!f.player_id || !f.blob?.usage) continue;
    const name = names.get(f.player_id);
    if (!name) continue;
    const usage = (f.blob.usage || {}) as Record<string, number | null | undefined>;
    const efficiency = (f.blob.efficiency || {}) as Record<string, number | null | undefined>;
    const next = {
      id: f.player_id,
      name,
      sample: Number(f.blob.sample_size ?? 0),
      missingness: f.missingness,
      targets: num(usage.targets_per_game),
      rushShare: num(usage.team_td_share),
      workloadDelta: num(usage.workload_delta_3v10),
      recYpg: num(efficiency.rec_yards_per_game),
      rushYpg: num(efficiency.rush_yards_per_game),
    };
    const prev = bestByPlayer.get(f.player_id);
    const nextScore = (next.targets ?? 0) + (next.rushYpg ?? 0) + next.sample;
    const prevScore = prev ? (prev.targets ?? 0) + (prev.rushYpg ?? 0) + prev.sample : -1;
    if (!prev || nextScore >= prevScore) bestByPlayer.set(f.player_id, next);
  }

  const rows = [...bestByPlayer.values()]
    .sort(
      (a, b) =>
        (b.targets ?? 0) + (b.rushYpg ?? 0) - ((a.targets ?? 0) + (a.rushYpg ?? 0)),
    )
    .slice(0, 12);

  if (!rows.length) {
    return (
      <p className="text-sm leading-relaxed text-mute">
        No player usage snapshots are attached to this game yet.
      </p>
    );
  }

  return (
    <div className="space-y-4">
      <p className="text-sm leading-relaxed text-paper/75">
        Recent workload that fed the pregame number. Missing fields stay blank — we do not invent usage.
      </p>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[720px] text-left text-sm">
          <thead className="font-mono text-[10px] uppercase tracking-[0.18em] text-mute">
            <tr className="border-b border-white/10">
              <th className="px-3 py-2 font-medium">Player</th>
              <th className="px-3 py-2 font-medium">Sample</th>
              <th className="px-3 py-2 font-medium">Targets / g</th>
              <th className="px-3 py-2 font-medium">Rec yds / g</th>
              <th className="px-3 py-2 font-medium">Rush yds / g</th>
              <th className="px-3 py-2 font-medium">TD share</th>
              <th className="px-3 py-2 font-medium">Workload Δ</th>
              <th className="px-3 py-2 font-medium">Missing</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row, idx) => (
              <tr key={`${row.id}-${idx}`} className="border-b border-white/5 last:border-0">
                <td className="px-3 py-2.5 font-medium">{row.name}</td>
                <td className="px-3 py-2.5 font-mono text-mute">{row.sample || "—"}</td>
                <td className="px-3 py-2.5 font-mono">{fmt(row.targets, 1)}</td>
                <td className="px-3 py-2.5 font-mono">{fmt(row.recYpg, 1)}</td>
                <td className="px-3 py-2.5 font-mono">{fmt(row.rushYpg, 1)}</td>
                <td className="px-3 py-2.5 font-mono">{row.rushShare == null ? "—" : pct(row.rushShare)}</td>
                <td className="px-3 py-2.5 font-mono">
                  {row.workloadDelta == null
                    ? "—"
                    : `${row.workloadDelta >= 0 ? "+" : ""}${row.workloadDelta.toFixed(1)}`}
                </td>
                <td className="px-3 py-2.5 font-mono text-mute">{pct(row.missingness, 0)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function num(v: number | null | undefined): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function fmt(v: number | null, digits: number): string {
  return v == null ? "—" : v.toFixed(digits);
}
