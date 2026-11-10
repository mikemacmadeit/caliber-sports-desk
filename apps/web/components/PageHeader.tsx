export function PageHeader({
  kicker,
  title,
  lede,
  action,
  accent = "lime",
}: {
  kicker?: string;
  title: string;
  lede?: string;
  action?: React.ReactNode;
  accent?: "lime" | "signal" | "flare";
}) {
  const kickerTone = accent === "flare" ? "kicker-flare" : accent === "signal" ? "kicker-signal" : "";
  return (
    <header className="flex flex-wrap items-end justify-between gap-6 border-b border-white/[0.08] pb-7">
      <div className="max-w-3xl">
        {kicker ? <p className={`kicker ${kickerTone}`}>{kicker}</p> : null}
        <h1 className="display mt-3 text-4xl leading-[0.92] text-paper sm:text-5xl md:text-7xl">{title}</h1>
        {lede ? <p className="mt-4 max-w-xl text-[15px] leading-relaxed text-paper/70">{lede}</p> : null}
      </div>
      {action ? <div className="pb-1">{action}</div> : null}
    </header>
  );
}
