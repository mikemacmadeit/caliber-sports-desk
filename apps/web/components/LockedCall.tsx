import Link from "next/link";

export function LockedCall({
  title = "This call opens at kickoff",
  detail = "The record stays public. Subscribers see the call before the game — $19.99 a month.",
}: {
  title?: string;
  detail?: string;
}) {
  return (
    <div className="plate px-5 py-6">
      <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-gold">Locked</p>
      <p className="mt-3 font-display text-3xl uppercase leading-none tracking-wide">{title}</p>
      <p className="mt-3 max-w-xl text-sm leading-relaxed text-paper/70">{detail}</p>
      <div className="mt-5 flex flex-wrap gap-3">
        <Link
          href="/signup"
          className="inline-flex min-h-11 items-center bg-gold px-4 font-display text-xl uppercase tracking-wide text-ink-950"
        >
          Subscribe
        </Link>
        <Link href="/login" className="inline-flex min-h-11 items-center px-4 font-display text-xl uppercase tracking-wide text-paper">
          Log in
        </Link>
      </div>
    </div>
  );
}
