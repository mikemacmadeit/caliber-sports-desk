import Link from "next/link";

export type DeskStatus = {
  mode: "research" | "official";
  play_enabled: boolean;
  production_ready: boolean;
  blend_settled?: { player_games?: number; games?: number; wins?: number; losses?: number } | null;
  open_takes?: number;
  remaining?: string[];
  note?: string | null;
  odds?: { status?: string; ticks_upserted?: number; credits_spent?: number } | null;
};

export function DeskStatusBanner({ status }: { status: DeskStatus | null }) {
  if (!status) return null;
  const blend = status.blend_settled;
  const needGames = 20;
  const needPlayers = 200;
  const games = blend?.games ?? 0;
  const players = blend?.player_games ?? 0;
  const liveProgress =
    games >= needGames && players >= needPlayers
      ? "Live proof cleared"
      : `Live proof ${games}/${needGames} games · ${players}/${needPlayers} players`;

  return (
    <div className="plate grid gap-3 px-4 py-4 sm:grid-cols-4">
      <StatusCell
        label="Desk mode"
        value={status.play_enabled ? "Official Play" : "Research only"}
        hint={status.production_ready ? "Gates clear — unlock still manual" : "Validated Play stays off"}
      />
      <StatusCell label="Live proof" value={liveProgress} hint="Freeze before kickoff, grade after" />
      <StatusCell
        label="Open takes"
        value={String(status.open_takes ?? 0).padStart(2, "0")}
        hint="Marked desk picks still pending"
      />
      <div className="min-w-0">
        <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-mute">Next</p>
        <p className="mt-2 font-display text-2xl uppercase leading-none tracking-wide text-paper">
          {status.remaining?.[0] ? "Keep freezing" : status.production_ready ? "Manual unlock" : "Build live N"}
        </p>
        <p className="mt-2 truncate text-sm text-mute">
          {status.note || "Official Play does not flip on by itself."}{" "}
          <Link href="/track-record" className="text-gold">
            Ledger →
          </Link>
        </p>
      </div>
    </div>
  );
}

function StatusCell({ label, value, hint }: { label: string; value: string; hint: string }) {
  return (
    <div className="min-w-0">
      <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-mute">{label}</p>
      <p className="mt-2 truncate font-display text-2xl uppercase leading-none tracking-wide text-paper">{value}</p>
      <p className="mt-2 truncate font-mono text-[10px] uppercase tracking-[0.16em] text-mute">{hint}</p>
    </div>
  );
}
