import Link from "next/link";

export default function Page() {
  const support = process.env.NEXT_PUBLIC_SUPPORT_EMAIL || "";
  return <article className="max-w-3xl space-y-6">
    <h1 className="font-display text-4xl">Using Caliber</h1>
    <section className="space-y-2"><h2 className="font-display text-2xl">Reading a pick</h2><p className="leading-relaxed text-paper/80">The main pick is the model's most likely eligible scorer. An alternate is another eligible candidate with its own explanation. An alternate is not automatically a good bet at the listed price.</p></section>
    <section className="space-y-2"><h2 className="font-display text-2xl">Understanding percentages</h2><p className="leading-relaxed text-paper/80">A 60% estimate means similar predictions should score about six times in ten over many games. It does not mean this player is certain to score. Model estimates and simulations remain research until validated.</p></section>
    <section className="space-y-2"><h2 className="font-display text-2xl">Prices and results</h2><p className="leading-relaxed text-paper/80">Odds belong to the named bookmaker at the displayed capture time. They can change. Win rates count scorer outcomes; betting returns require a recorded qualifying price. Live scores are provisional until the official final.</p></section>
    <section className="space-y-2"><h2 className="font-display text-2xl">Account help</h2><p className="leading-relaxed text-paper/80">Use Forgot password on the login page. Verification links expire after one hour. Manage or cancel a subscription from Account, then Billing. Account deletion removes login and contact details while retaining billing audit references.</p></section>
    <section className="space-y-2"><h2 className="font-display text-2xl">Historical records</h2><p className="leading-relaxed text-paper/80">Picks saved before kickoff are kept separate from later experiments. Older archives without verified publication times are labeled and excluded from verified performance. Missing results remain pending.</p></section>
    {support ? <p>Support: <a className="text-gold" href={`mailto:${support}`}>{support}</a></p> : <p className="text-sm text-mute">Support contact details will be added before public launch.</p>}
    <Link className="text-gold" href="/account">Account and billing</Link>
  </article>;
}
