export type DeskChange = { kind: string; before: number | string; after: number | string; at: string; bookmaker: string | null };
export type DeskCard = {
  id: string; outcome_id: string; game_id: string; league: string; player: string; team: string;
  opponent: string; matchup: string; kickoff: string; market: string; pick: string;
  book: string | null; price: number | null; probability: number | null; break_even: number | null;
  conservative_ev: number | null; availability: string; model: string; prediction_at: string; data_cutoff: string;
  qualification: { eligible: boolean; reasons: string[]; verified_at: string | null; valid_until: string | null; minimum_american_odds: number | null };
  evidence: { label: string; value: string; source: string }[]; risks: string[];
  offers: { book: string; price: number; verified_at: string | null; valid_until?: string | null; current: boolean }[];
  changes: DeskChange[];
};
export type DeskData = {
  rows: DeskCard[]; total: number; offset: number; limit: number; books: string[];
  qualified: number; changed: number; blockers: { reason: string; count: number }[];
  status: "ready" | "warming" | "delayed"; date: string; league: string;
  checked_at: string; updated_at: string | null; note: string;
  games: { id: string; league: string; matchup: string; kickoff: string }[];
};
export type SavedSelection = Pick<DeskCard, "id" | "player" | "pick" | "kickoff" | "book" | "price" | "game_id"> & { saved_at: string };
export const odds = (n: number | null) => n == null ? "—" : n > 0 ? `+${n}` : String(n);
export const pct = (n: number | null) => n == null ? "—" : `${(n * 100).toFixed(1)}%`;
export const isOpen = (row: DeskCard, now: number) => Date.parse(row.kickoff) > now;
export const isCurrent = (row: DeskCard, now: number) => isOpen(row, now) && !!row.qualification.valid_until && Date.parse(row.qualification.valid_until) > now && !row.qualification.reasons.includes("price_not_verified_recently");
export function priceCheck(row: DeskCard, price: string, now: number) {
  const n = Number(price);
  if (!/^[+-]?\d+$/.test(price) || !Number.isInteger(n) || Math.abs(n) < 100 || Math.abs(n) > 100000) return { label: "Enter valid American odds, such as -110 or +150.", value: null };
  if (!isCurrent(row, now)) return { label: "Price verification has expired. Refresh before comparing.", value: null };
  if (!row.qualification.eligible) return { label: "This selection has not cleared every research check. Review its risks before using a price threshold.", value: null };
  const minimum = row.qualification.minimum_american_odds;
  const decimal = (odds: number) => odds > 0 ? 1 + odds / 100 : 1 + 100 / -odds;
  const clears = minimum != null && decimal(n) >= decimal(minimum);
  return { label: minimum == null ? "A price threshold is unavailable." : clears ? "Meets the model’s minimum price. Confirm the same line at your book." : "Below the model’s minimum price. Pass at these odds.", value: clears };
}
