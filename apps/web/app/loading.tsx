export default function Loading() {
  return (
    <div className="space-y-6" aria-busy="true" aria-live="polite">
      <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-mute">Opening</p>
      <div className="h-10 w-56 bg-white/10" />
      <div className="h-5 w-full max-w-xl bg-white/5" />
      <div className="grid gap-3 md:grid-cols-2">
        <div className="h-32 bg-white/5" />
        <div className="h-32 bg-white/5" />
      </div>
    </div>
  );
}
