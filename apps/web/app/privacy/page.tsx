import Link from "next/link";

export default function Page() {
  const support = process.env.NEXT_PUBLIC_SUPPORT_EMAIL || "";
  return <article className="max-w-3xl space-y-6">
    <h1 className="font-display text-4xl">Privacy notice - draft</h1>
    <p className="rounded border border-gold/40 p-4 text-sm text-gold">Draft for owner review. Business terms and legal approval are still required before public launch.</p>
    <section className="space-y-2"><h2 className="font-display text-2xl">Account information</h2><p className="leading-relaxed text-paper/80">The application stores your email address, a salted password hash, hashed session and recovery tokens, verification status, and subscription status. Caliber does not store payment card numbers; payment card entry is handled by Stripe.</p></section>
    <section className="space-y-2"><h2 className="font-display text-2xl">Cookies and providers</h2><p className="leading-relaxed text-paper/80">An essential session cookie keeps you signed in. The application uses hosting providers for web/API/database service, Stripe for billing, and a configured email provider for account messages. Operational logs can include request and network information.</p></section>
    <section className="space-y-2"><h2 className="font-display text-2xl">Account removal</h2><p className="leading-relaxed text-paper/80">The account page can remove login and contact details and revoke sessions. References needed for billing reconciliation remain. Retention periods, the legal operator, privacy contact and applicable customer rights must be approved before public launch.</p></section>
    {support ? <p>Support: <a className="text-gold" href={`mailto:${support}`}>{support}</a></p> : <p className="text-sm text-mute">Support contact details will be added before public launch.</p>}
    <Link className="text-gold" href="/account">Account and billing</Link>
  </article>;
}
