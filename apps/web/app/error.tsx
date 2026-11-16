"use client";

export default function ErrorPage({ reset }: { reset: () => void }) {
  return <section className="plate max-w-xl space-y-4 p-6" role="alert">
    <h1 className="font-display text-3xl">The desk is temporarily unavailable</h1>
    <p>We couldn’t load the latest information. Your account and saved picks have not been changed.</p>
    <button type="button" onClick={reset} className="min-h-11 bg-gold px-4 text-ink-950">Try again</button>
  </section>;
}
