import Link from "next/link";

export default function Page() {
  const support = process.env.NEXT_PUBLIC_SUPPORT_EMAIL || "";
  return <article className="max-w-3xl space-y-6">
    <h1 className="font-display text-4xl">Terms of use - draft</h1>
    <p className="rounded border border-gold/40 p-4 text-sm text-gold">Draft for owner review. Business terms and legal approval are still required before public launch.</p>
    <section className="space-y-2"><h2 className="font-display text-2xl">Research service</h2><p className="leading-relaxed text-paper/80">Caliber provides football research and probability estimates. It does not accept or place wagers. Predictions can be wrong, and no profit or particular result is guaranteed.</p></section>
    <section className="space-y-2"><h2 className="font-display text-2xl">Subscription management</h2><p className="leading-relaxed text-paper/80">The current application displays a monthly subscription. Checkout shows the amount before payment. Account, then Billing provides subscription management. Final pricing, cancellation timing, refund policy and legal operator details require approval before public launch.</p></section>
    <section className="space-y-2"><h2 className="font-display text-2xl">Responsible use</h2><p className="leading-relaxed text-paper/80">Use information only where lawful and appropriate for your age and location. Do not treat predictions as guaranteed income. Availability of a pick does not mean its price offers value.</p></section>
    <section className="space-y-2"><h2 className="font-display text-2xl">Before launch</h2><p className="leading-relaxed text-paper/80">The legal operator, support details, geographic availability, minimum age, data-use permissions, retention and refund terms must be reviewed and finalized. This draft does not establish those policies.</p></section>
    {support ? <p>Support: <a className="text-gold" href={`mailto:${support}`}>{support}</a></p> : <p className="text-sm text-mute">Support contact details will be added before public launch.</p>}
    <Link className="text-gold" href="/account">Account and billing</Link>
  </article>;
}
