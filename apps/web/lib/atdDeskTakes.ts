import type { PropRow } from "@/lib/api";

/** Keep in lockstep with spe.shadow.atd_desk_takes */
export const VALUE_HAIRCUT = 0.04;
/** Minimum modeled scoring probability for a research Desk candidate. */
export const SCORE_CHANCE_FLOOR = 0.22;
function americanToImplied(american: number): number {
  if (american >= 100) return 100 / (american + 100);
  if (american <= -100) return (-american) / (-american + 100);
  return 0.5;
}

function eligible(row: PropRow, _haircut = VALUE_HAIRCUT): boolean {
  const status = (row.forecast.availability_status || "").toLowerCase();
  if (["out", "doubtful", "inactive", "confirmed_inactive", "suspended", "questionable", "uncertain"].includes(status)) return false;
  if (row.desk_analysis && !row.desk_analysis.eligible) return false;
  if (row.forecast.p_estimate == null) return false;
  const p = row.forecast.p_estimate ?? 0;
  if (!Number.isFinite(p) || p < SCORE_CHANCE_FLOOR || p > 1) return false;
  return true;
}

export function beatsThePrice(row: PropRow): boolean {
  const chance = row.forecast.p_estimate;
  const price = row.book_price;
  if (chance == null || price == null) return false;
  return chance >= SCORE_CHANCE_FLOOR && chance > americanToImplied(price);
}

/** The server selects once. Filters, stale caches, and old APIs cannot invent picks. */
export function selectDeskTakes(rows: PropRow[], haircut = VALUE_HAIRCUT): PropRow[] {
  return rows.filter(row => row.desk_selected === true && (row.locked || (eligible(row, haircut) &&
    Boolean(row.desk_analysis?.valid_until) && Date.parse(row.desk_analysis!.valid_until!) > Date.now())))
    .sort((a, b) => b.forecast.p_estimate - a.forecast.p_estimate);
}

export function deskScoringLine(row: PropRow): string | null {
  const bits: string[] = [];
  const gl = row.usage?.gl_carries;
  const rz = row.usage?.rz_targets;
  const scores = (row.usage?.rush_td ?? 0) + (row.usage?.rec_td ?? 0);
  if (gl != null && gl >= 0.2) bits.push(countPhrase(gl, "goal-line carry", "goal-line carries"));
  if (rz != null && rz >= 0.2) bits.push(countPhrase(rz, "red-zone target", "red-zone targets"));
  if (scores >= 0.3) bits.push(`${trimNum(scores)} recent scores`);
  if (bits.length === 0) return null;
  return `${bits.join(", ")} a game`;
}

export function deskPickReason(row: PropRow): string {
  if (row.desk_analysis) return row.desk_analysis.reasons.join(" ");
  const chance = row.forecast.p_estimate;
  const price = row.book_price;
  const parts: string[] = [];
  const path = deskScoringLine(row);
  if (path) parts.push(`${path}.`);
  if (chance == null || price == null) {
    parts.push("No posted price, so this player is not a pick.");
  } else {
    const implied = americanToImplied(price);
    const gap = Math.round((chance - implied) * 100);
    const priceText = price > 0 ? `+${price}` : String(price);
    parts.push(priceCompared(Math.round(chance * 100), priceText, Math.round(implied * 100), gap));
  }
  const matchup = row.forecast.matchup;
  if (matchup && matchup.factor >= 1.04) {
    parts.push(`This defense has allowed more ${matchup.kind === "rush" ? "rushing" : "receiving"} scores than usual.`);
  } else if (matchup && matchup.factor <= 0.96) {
    parts.push(`This defense has allowed fewer ${matchup.kind === "rush" ? "rushing" : "receiving"} scores than usual.`);
  }
  return parts.join(" ");
}

function priceCompared(chancePct: number, priceText: string, impliedPct: number, gap: number) {
  const base = `Chance is ${chancePct}%. ${priceText} means the book has this around ${impliedPct}%.`;
  const points = Math.abs(gap) === 1 ? "point" : "points";
  if (gap > 0) return `${base} Our chance is ${gap} ${points} higher.`;
  if (gap < 0) return `${base} Our chance is ${Math.abs(gap)} ${points} lower.`;
  return `${base} That matches.`;
}

function countPhrase(value: number, singular: string, plural: string) {
  const text = trimNum(value);
  return `${text} ${text === "1" ? singular : plural}`;
}

function trimNum(value: number): string {
  const rounded = Math.round(value * 10) / 10;
  return Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1);
}

/** Fresh edge shortlist (secondary value list). */
export function valueAtPrice(rows: PropRow[], haircut = VALUE_HAIRCUT): PropRow[] {
  return rows.filter(row => Boolean(row.published_pick_id) && row.qualification?.eligible &&
    Boolean(row.qualification.valid_until) && Date.parse(row.qualification.valid_until!) > Date.now() &&
    (row.forecast.ev_after_haircut ?? -1) >= haircut);
}
