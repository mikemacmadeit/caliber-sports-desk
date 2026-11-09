export function MarginBar({
  margin,
  handicap,
  awayColor,
  homeColor,
  awayName,
  homeName,
}: {
  margin: number | null | undefined;
  handicap: number | null | undefined;
  awayColor: string;
  homeColor: string;
  awayName: string;
  homeName: string;
}) {
  if (margin == null || Number.isNaN(margin)) return null;
  const number = handicap == null || Number.isNaN(handicap) ? null : -handicap;
  const span = Math.max(7, Math.abs(margin), number == null ? 0 : Math.abs(number)) * 1.2;
  const at = (value: number) => {
    const pct = ((value + span) / (span * 2)) * 100;
    return `${Math.min(98, Math.max(2, pct))}%`;
  };

  return (
    <div className="mt-4" aria-hidden>
      <div className="relative h-2">
        <div
          className="absolute inset-0"
          style={{
            background: `linear-gradient(90deg, ${awayColor} 0%, rgba(255,255,255,0.08) 50%, ${homeColor} 100%)`,
          }}
        />
        <span className="absolute bottom-[-3px] top-[-3px] w-px bg-white/40" style={{ left: "50%" }} />
        {number != null ? (
          <span
            className="absolute bottom-[-4px] top-[-4px] w-[2px] bg-paper"
            style={{ left: at(number) }}
          />
        ) : null}
        <span
          className="absolute top-1/2 h-3 w-3 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-ink-950 bg-gold"
          style={{ left: at(margin) }}
        />
      </div>
      <div className="mt-2 flex items-center justify-between gap-3 font-mono text-[10px] uppercase tracking-[0.14em] text-mute">
        <span className="truncate" style={{ color: awayColor }}>
          {shortName(awayName)}
        </span>
        <span className="shrink-0">{number == null ? "Projected margin" : "Score against the number"}</span>
        <span className="truncate text-right" style={{ color: homeColor }}>
          {shortName(homeName)}
        </span>
      </div>
    </div>
  );
}

function shortName(name: string) {
  const parts = name.trim().split(/\s+/);
  return parts[parts.length - 1] || name;
}
