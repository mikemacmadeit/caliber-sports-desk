import { evPct, pct } from "@/lib/format";

export function RecChip({ status }: { status: string | null | undefined }) {
  if (status === "demo_play") {
    return (
      <span className="inline-flex whitespace-nowrap border border-gold/50 px-2 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-gold">
        Demo
      </span>
    );
  }
  if (status === "scorer") {
    return (
      <span className="inline-flex whitespace-nowrap bg-flare px-2 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-ink-950">
        Scorer
      </span>
    );
  }
  const play = status === "play";
  return (
    <span
      className={`inline-flex whitespace-nowrap px-2 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-[0.16em] ${
        play ? "bg-gold text-ink-950" : "bg-ink-700 text-mute"
      }`}
    >
      {play ? "Play" : "No bet"}
    </span>
  );
}

export function TierChip({ tier }: { tier?: string | null }) {
  const label =
    tier === "validated"
      ? "Validated"
      : tier === "experimental"
        ? "Experimental"
        : tier === "historical"
          ? "Historical"
          :     tier === "upcoming"
            ? "Live"
            : tier === "research"
              ? "Research"
            : tier === "season"
              ? "Season"
              : "Demo";
  const hot = tier === "validated";
  return (
    <span
      className={`inline-flex border px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-[0.16em] ${
        hot ? "border-gold/60 text-gold" : "border-white/15 text-mute"
      }`}
    >
      {label}
    </span>
  );
}

export function EvChip({ ev }: { ev: number | null | undefined }) {
  if (ev == null) return <span className="font-mono text-mute">—</span>;
  const pos = ev >= 0.04;
  return (
    <span className={`font-mono text-sm font-medium ${pos ? "text-gold" : ev < 0 ? "text-heat" : "text-mute"}`}>
      {evPct(ev)}
    </span>
  );
}

export function Interval({ p, lo, hi }: { p: number; lo: number; hi: number }) {
  const showBand = Math.abs(hi - lo) > 1e-6;
  return (
    <span className="font-mono text-sm">
      <span className="text-paper">{pct(p)}</span>
      {showBand ? (
        <>
          {" "}
          <span className="text-mute">
            [{pct(lo)}–{pct(hi)}]
          </span>
        </>
      ) : null}
    </span>
  );
}

export function LeagueBadge({ league }: { league: string }) {
  const nfl = league === "nfl";
  return (
    <span
      className={`border px-1.5 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-[0.18em] ${
        nfl ? "border-gold/40 text-gold" : "border-signal text-signal"
      }`}
    >
      {league}
    </span>
  );
}

export function Freshness({ minutes }: { minutes: number | null | undefined }) {
  if (minutes == null) return null;
  return (
    <span className="font-mono text-[11px] uppercase tracking-wider text-mute">
      {Math.round(minutes)}m
    </span>
  );
}
