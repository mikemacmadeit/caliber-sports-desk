import type { PropRow } from "@/lib/api";

/** Keep in lockstep with spe.shadow.atd_desk_takes */
export const VALUE_HAIRCUT = 0.04;
/** Same floor the desk already uses before it will name an unpriced player. */
export const SCORE_CHANCE_FLOOR = 0.22;
/** A desk pick has to be a real chance, not a longshot whose price looks generous. */
const MIN_DESK_CHANCE = 0.18;

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
  if (row.selection_safety && !row.selection_safety.eligible) return false;
  if (row.desk_analysis?.eligible === false) return false;
  if (row.desk_analysis?.combination_status === "active" &&
      !(row.price_freshness || row.forecast.price_freshness)?.is_current_opportunity) return false;
  const status = (row.forecast.availability_status || "").toLowerCase();
  if (status === "out" || status === "doubtful" || status === "confirmed_inactive" || status === "inactive" || status === "suspended" || status === "questionable" || status === "uncertain") return false;
  if (row.forecast.p_estimate == null) return false;
  const p = row.forecast.p_estimate ?? 0;
  if (!Number.isFinite(p) || p <= 0 || p > 1) return false;
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

/** Season chance, cut when this team is a big dog. Favorites stay as they are. */
function tonightChance(row: PropRow): number | null {
  const raw = row.forecast.p_estimate;
  if (raw == null) return null;
  if (row.desk_analysis?.combination_status === "active") return raw;
  const spread = row.team_spread;
  if (spread == null || spread <= 3) return raw;
  const factor = Math.max(0.35, 1 - (spread - 3) / 40);
  return raw * factor;
}

function priceGap(row: PropRow): number | null {
  const chance = tonightChance(row);
  const price = row.book_price;
  if (chance == null || price == null) return null;
  return chance - americanToImplied(price);
}

function usageNum(row: PropRow, key: "gl_carries" | "rz_targets" | "rush_td" | "rec_td" | "carries" | "targets"): number {
  const value = row.usage?.[key];
  return value == null || Number.isNaN(value) ? 0 : value;
}

/** A price gap is not a reason. He has to be the one getting the scoring work. */
function hasScoringRole(row: PropRow): boolean {
  if (row.desk_analysis?.combination_status === "active") {
    const c = row.desk_analysis.combination;
    return !!c && c.rush_lambda + c.rec_lambda > 0;
  }
  const scores = usageNum(row, "rush_td") + usageNum(row, "rec_td");
  if (usageNum(row, "gl_carries") >= 0.4 || usageNum(row, "rz_targets") >= 0.5) return true;
  return scores >= 0.45 && (usageNum(row, "carries") >= 5 || usageNum(row, "targets") >= 3.5);
}

/** Goal-line and red-zone work first. Recent scores cover a league without those splits. */
export function scoringRole(row: PropRow): number {
  if (row.desk_analysis?.combination_status === "active") return row.forecast.p_estimate ?? 0;
  return usageNum(row, "gl_carries") * 4 + usageNum(row, "rz_targets") * 2 + (usageNum(row, "rush_td") + usageNum(row, "rec_td")) * 1.5;
}

/** One scorer per game: scoring touches, and only when our chance beats the price. */
export function selectDeskTakes(rows: PropRow[], haircut = 0): PropRow[] {
  const best = new Map<string, { role: number; gap: number; row: PropRow }>();
  const combinedGames = new Set(rows.filter(r => r.desk_analysis?.combination_status === "active").map(r => r.game_id));
  const combined = new Map<string, PropRow[]>();
  for (const row of rows) {
    if (!eligible(row, haircut)) continue;
    if (combinedGames.has(row.game_id)) {
      if (row.desk_analysis?.combination_status === "active" && row.book_price != null && Math.abs(row.book_price) >= 100 && (row.forecast.p_estimate ?? 0) >= .40 && row.market_context?.usable && (row.team_implied_points ?? 0) >= 21) {
        combined.set(row.game_id, [...(combined.get(row.game_id) || []), row]);
      }
      continue;
    }
    const chance = tonightChance(row) ?? 0;
    if (chance < MIN_DESK_CHANCE) continue;
    const gap = priceGap(row);
    if (gap == null || gap <= haircut) continue;
    if (!hasScoringRole(row)) continue;
    const role = scoringRole(row);
    const prior = best.get(row.game_id);
    if (!prior || role > prior.role || (role === prior.role && (gap > prior.gap ||
        (gap === prior.gap && (row.player_id || "") > (prior.row.player_id || ""))))) {
      best.set(row.game_id, { role, gap, row });
    }
  }
  for (const [game, candidates] of combined) {
    const peak = Math.max(...candidates.map(r => r.forecast.p_estimate ?? 0));
    const affordable = candidates.filter(r => r.book_price! >= -200 && peak - r.forecast.p_estimate! <= .10 + 1e-12);
    const ranked = (candidates.some(r => r.selection_safety) ? candidates : affordable.length ? affordable : candidates).slice().sort((a, b) =>
      b.forecast.p_estimate! - a.forecast.p_estimate! || (priceGap(b) ?? -1) - (priceGap(a) ?? -1) ||
      ((a.player_id || "") < (b.player_id || "") ? 1 : (a.player_id || "") > (b.player_id || "") ? -1 : 0));
    const row = ranked[0];
    best.set(game, {role: row.forecast.p_estimate!, gap: priceGap(row) ?? 0, row});
  }
  return [...best.values()]
    .sort((a, b) => b.role - a.role || b.gap - a.gap || a.row.player_name.localeCompare(b.row.player_name))
    .map((entry) => entry.row);
}

export function deskScoringLine(row: PropRow): string | null {
  const bits: string[] = [];
  const gl = row.usage?.gl_carries;
  const rz = row.usage?.rz_targets;
  const scores = (row.usage?.rush_td ?? 0) + (row.usage?.rec_td ?? 0);
  if (gl != null && gl >= 0.2) bits.push(countPhrase(gl, "goal-line carry", "goal-line carries"));
  if (rz != null && rz >= 0.2) bits.push(countPhrase(rz, "red-zone target", "red-zone targets"));
  if (scores >= 0.3) bits.push(countPhrase(scores, "recent score", "recent scores"));
  if (bits.length === 0) return null;
  return `${bits.join(", ")} a game`;
}

export function deskPickReason(row: PropRow): string {
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
