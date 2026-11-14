import type { PropRow } from "@/lib/api";

/** Keep in lockstep with spe.shadow.atd_desk_takes */
export const VALUE_HAIRCUT = 0.04;
/** Same floor the desk already uses before it will name an unpriced player. */
export const SCORE_CHANCE_FLOOR = 0.22;

const W_CHANCE = 0.35;
const W_EDGE = 0.4;
const W_FRESH = 0.15;
const W_AVAIL = 0.1;

function clamp01(x: number) {
  return Math.max(0, Math.min(1, x));
}

function americanToImplied(american: number): number {
  if (american >= 100) return 100 / (american + 100);
  if (american <= -100) return (-american) / (-american + 100);
  return 0.5;
}

function edgeComponent(evHc: number | null | undefined) {
  if (evHc == null || Number.isNaN(evHc)) return 0.35;
  return clamp01(1 / (1 + Math.exp(-12 * evHc)));
}

function freshComponent(row: PropRow) {
  const freshness = row.price_freshness || row.forecast.price_freshness;
  const status = freshness?.status || "";
  const current = Boolean(freshness?.is_current_opportunity);
  if (status === "fresh" && current) return 1;
  if (status === "fresh") return 0.7;
  if (status === "stale") return 0.25;
  return 0.15;
}

function availComponent(row: PropRow) {
  const status = (row.forecast.availability_status || "unknown").toLowerCase();
  if (status === "out" || status === "doubtful") return 0;
  if (status === "questionable" || status === "uncertain") return 0.45;
  if (status === "probable" || status === "active" || status === "healthy") return 1;
  if (status === "missing_report" || status === "unknown") return 0.55;
  return 0.5;
}

export function deskPickScore(row: PropRow): number {
  const p = row.forecast.p_estimate ?? 0;
  const evHc = row.forecast.ev_after_haircut ?? row.forecast.ev;
  let total =
    W_CHANCE * clamp01(p) +
    W_EDGE * edgeComponent(evHc) +
    W_FRESH * freshComponent(row) +
    W_AVAIL * availComponent(row);
  if (row.book_price != null && p > 0) {
    const implied = americanToImplied(row.book_price);
    const gap = p - implied;
    if (gap > 0) total += Math.min(0.06, gap * 0.35);
  }
  return total;
}

function eligible(row: PropRow, _haircut = VALUE_HAIRCUT): boolean {
  const status = (row.forecast.availability_status || "").toLowerCase();
  if (["out", "doubtful", "inactive", "confirmed_inactive", "suspended", "questionable", "uncertain"].includes(status)) return false;
  if (row.desk_analysis && !row.desk_analysis.eligible) return false;
  if (row.forecast.p_estimate == null) return false;
  const p = row.forecast.p_estimate ?? 0;
  if (!Number.isFinite(p) || p < SCORE_CHANCE_FLOOR || p > 1) return false;
  // Freshness/edge are score factors, not vetoes. Research-only rows need a floor.
  if (row.book_price == null && p < 0.22) return false;
  return true;
}

export function beatsThePrice(row: PropRow): boolean {
  const chance = row.forecast.p_estimate;
  const price = row.book_price;
  if (chance == null || price == null) return false;
  return chance >= SCORE_CHANCE_FLOOR && chance > americanToImplied(price);
}

/** One scorer per game: the highest chance he scores tonight. Price is not a veto. */
export function selectDeskTakes(rows: PropRow[], haircut = VALUE_HAIRCUT): PropRow[] {
  if (rows.some(row => row.desk_selected !== undefined)) {
    return rows.filter(row => row.desk_selected && (row.locked || eligible(row, haircut)))
      .sort((a, b) => b.forecast.p_estimate - a.forecast.p_estimate);
  }
  const best = new Map<string, { chance: number; row: PropRow }>();
  for (const row of rows) {
    if (!eligible(row, haircut)) continue;
    const chance = row.forecast.p_estimate ?? 0;
    if (chance < 0.05) continue;
    const prior = best.get(row.game_id);
    if (!prior || chance > prior.chance || (chance === prior.chance && String(row.player_id) > String(prior.row.player_id))) best.set(row.game_id, { chance, row });
  }
  return [...best.values()]
    .sort((a, b) => b.chance - a.chance || a.row.player_name.localeCompare(b.row.player_name))
    .map((entry) => entry.row);
}

export function deskPickReason(row: PropRow): string {
  if (row.desk_analysis) return row.desk_analysis.reasons.join(" ");
  const summary = row.forecast.tonight?.summary;
  if (summary) return summary;
  const chance = row.forecast.p_estimate;
  const price = row.book_price;
  if (chance == null) return "No scoring estimate available.";
  if (price == null) return "Scoring estimate only; no current sportsbook price is available.";
  const implied = americanToImplied(price);
  const gap = Math.round((chance - implied) * 100);
  const priceText = price > 0 ? `+${price}` : String(price);
  const parts = [priceCompared(Math.round(chance * 100), priceText, Math.round(implied * 100), gap)];
  const work = usagePhrase(row.usage);
  if (work) parts.push(work);
  const matchup = row.forecast.matchup;
  if (matchup && matchup.factor >= 1.04) {
    parts.push(`This defense has allowed more ${matchup.kind === "rush" ? "rushing" : "receiving"} scores than usual.`);
  } else if (matchup && matchup.factor <= 0.96) {
    parts.push(`This defense has allowed fewer ${matchup.kind === "rush" ? "rushing" : "receiving"} scores than usual.`);
  }
  return parts.join(" ");
}

function usagePhrase(usage: PropRow["usage"]): string | null {
  if (!usage) return null;
  const bits: string[] = [];
  if (usage.carries != null && usage.carries >= 1) bits.push(countPhrase(usage.carries, "carry", "carries"));
  if (usage.targets != null && usage.targets >= 1) bits.push(countPhrase(usage.targets, "target", "targets"));
  if (usage.gl_carries != null && usage.gl_carries >= 0.2) bits.push(countPhrase(usage.gl_carries, "goal-line carry", "goal-line carries"));
  if (usage.rz_targets != null && usage.rz_targets >= 0.2) bits.push(countPhrase(usage.rz_targets, "red-zone target", "red-zone targets"));
  if (bits.length === 0) return null;
  return `Recent work is ${bits.join(", ")} a game.`;
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
