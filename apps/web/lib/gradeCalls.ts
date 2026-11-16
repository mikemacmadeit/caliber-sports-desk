import type { Grade } from "@/lib/calls";
import { cacheFile, readJsonFile, writeJsonFile } from "@/lib/diskJson";
import { slateToday } from "@/lib/days";

type GradedRow = {
  game_id?: string;
  home_team: string;
  away_team: string;
  game_status?: string | null;
  home_score?: number | null;
  away_score?: number | null;
  pick_market?: string | null;
  projection_withheld?: boolean;
  favored_side?: string | null;
  pick_result?: Grade;
  ats_side?: "home" | "away" | string | null;
  ats_line?: number | null;
  home_handicap?: number | null;
  ats_pick_result?: Grade;
  kickoff_utc?: string | null;
  rookie_qb_start?: "home" | "away" | null;
  pick_team?: string | null;
  pick_label?: string | null;
  pick_line?: number | null;
  pick_basis?: string | null;
  ats_team?: string | null;
  ats_pick_label?: string | null;
  ats_basis?: string | null;
  ats_locked?: boolean;
};

/** A quarterback's first NFL start. That team is faded on the moneyline and the spread. */
const ROOKIE_QB_STARTS: { date: string; home: string; away: string; side: "home" | "away" }[] = [
  { date: "2026-11-15", home: "Las Vegas Raiders", away: "Seattle Seahawks", side: "home" },
  { date: "2026-11-15", home: "Arizona Cardinals", away: "Los Angeles Rams", side: "home" },
];

function formatSpread(line: number): string {
  const rounded = Math.round(line * 10) / 10;
  const text = Math.abs(rounded - Math.round(rounded)) < 1e-6 ? String(Math.round(rounded)) : rounded.toFixed(1);
  return rounded > 0 ? `+${text}` : text;
}

function debutSide(row: GradedRow): "home" | "away" | null {
  if (row.rookie_qb_start === "home" || row.rookie_qb_start === "away") return row.rookie_qb_start;
  const kick = String(row.kickoff_utc || "").slice(0, 10);
  const found = ROOKIE_QB_STARTS.find(
    (debut) => debut.date === kick && norm(debut.home) === norm(row.home_team) && norm(debut.away) === norm(row.away_team),
  );
  return found?.side ?? null;
}

/** Take the opponent when a rookie quarterback is making his first start. */
export function fadeRookieQuarterback<T extends GradedRow>(row: T): T {
  const debut = debutSide(row);
  if (debut !== "home" && debut !== "away") return row;
  const opponent = debut === "home" ? "away" : "home";
  const next: T = { ...row };
  const handicap = row.home_handicap != null && !Number.isNaN(row.home_handicap) ? row.home_handicap : null;
  if (row.pick_market !== "withheld" && row.favored_side === debut) {
    next.favored_side = opponent;
    next.pick_team = opponent === "home" ? row.home_team : row.away_team;
    next.pick_label = `${next.pick_team} to win`;
    next.pick_line = handicap == null ? null : opponent === "home" ? handicap : -handicap;
    next.pick_basis = "rookie_qb";
  }
  if (handicap == null || row.ats_side === opponent) return next;
  const team = opponent === "home" ? row.home_team : row.away_team;
  const line = opponent === "home" ? handicap : -handicap;
  next.ats_side = opponent;
  next.ats_team = team;
  next.ats_line = line;
  next.ats_pick_label = `${team} ${formatSpread(line)}`;
  next.ats_basis = "rookie_qb";
  next.ats_locked = false;
  return next;
}

const SHORT_NUMBER = 3.5;

/** A dog on a number of 3.5 or less lays the points with the favorite. */
export function layShortFavorite<T extends GradedRow>(row: T): T {
  if (row.ats_basis === "rookie_qb") return row;
  const handicap = row.home_handicap;
  if (handicap == null || Number.isNaN(handicap) || Math.abs(handicap) > SHORT_NUMBER) return row;
  if ((row.ats_side !== "home" && row.ats_side !== "away") || row.ats_line == null || row.ats_line < 0) return row;
  const opponent = row.ats_side === "home" ? "away" : "home";
  const team = opponent === "home" ? row.home_team : row.away_team;
  const laid = opponent === "home" ? handicap : -handicap;
  return {
    ...row,
    ats_side: opponent,
    ats_team: team,
    ats_line: laid,
    ats_pick_label: `${team} ${formatSpread(laid)}`,
    ats_basis: "short_number",
    ats_locked: false,
  };
}

function withSpreadRules<T extends GradedRow>(row: T): T {
  return layShortFavorite(fadeRookieQuarterback(row));
}

export type RecordedFinal = {
  home: string;
  away: string;
  home_score: number;
  away_score: number;
};

function norm(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9]/g, "");
}

function moneyline(home: number, away: number, side: string): Grade {
  const margin = home - away;
  if (Math.abs(margin) < 1e-9) return "push";
  const winner = margin > 0 ? "home" : "away";
  return winner === side ? "correct" : "miss";
}

function spread(home: number, away: number, homeHandicap: number, side: string): Grade {
  const edge = home - away + homeHandicap;
  if (Math.abs(edge) < 1e-9) return "push";
  const homeCover = edge > 0;
  if (side === "home") return homeCover ? "correct" : "miss";
  return homeCover ? "miss" : "correct";
}

function homeHandicap(row: GradedRow): number | null {
  if (row.home_handicap != null && !Number.isNaN(row.home_handicap)) return row.home_handicap;
  if (row.ats_line == null || (row.ats_side !== "home" && row.ats_side !== "away")) return null;
  return row.ats_side === "home" ? row.ats_line : -row.ats_line;
}

/** Write the final score onto a named moneyline and spread. An open game stays pending. */
export function gradeFinishedCall<T extends GradedRow>(row: T, finals: RecordedFinal[]): T {
  const recorded = finals.find(
    (game) => norm(game.home) === norm(row.home_team) && norm(game.away) === norm(row.away_team),
  );
  const alreadyFinal = String(row.game_status || "").toLowerCase() === "final";
  const home = recorded ? recorded.home_score : alreadyFinal ? row.home_score : null;
  const away = recorded ? recorded.away_score : alreadyFinal ? row.away_score : null;
  if (home == null || away == null) return withSpreadRules(row);
  const faded = withSpreadRules(row);
  const next: T = { ...faded, game_status: "final", home_score: home, away_score: away };
  if (
    next.pick_market !== "withheld" &&
    !next.projection_withheld &&
    (next.favored_side === "home" || next.favored_side === "away")
  ) {
    next.pick_result = moneyline(home, away, next.favored_side);
  }
  const line = homeHandicap(next);
  if (line != null && (next.ats_side === "home" || next.ats_side === "away")) {
    next.ats_pick_result = spread(home, away, line, next.ats_side);
  }
  return next;
}

function finalsFromScoreboard(body: unknown): RecordedFinal[] {
  const events = (body as { events?: unknown[] })?.events;
  if (!Array.isArray(events)) return [];
  const out: RecordedFinal[] = [];
  for (const event of events) {
    const competition = (event as { competitions?: unknown[] }).competitions?.[0] as
      | {
          status?: { type?: { name?: string; state?: string } };
          competitors?: { homeAway?: string; score?: string; team?: { displayName?: string } }[];
        }
      | undefined;
    const name = competition?.status?.type?.name || "";
    if (!name.startsWith("STATUS_FINAL")) continue;
    const home = competition?.competitors?.find((team) => team.homeAway === "home");
    const away = competition?.competitors?.find((team) => team.homeAway === "away");
    const homeScore = Number(home?.score);
    const awayScore = Number(away?.score);
    if (!home?.team?.displayName || !away?.team?.displayName || !Number.isFinite(homeScore) || !Number.isFinite(awayScore)) {
      continue;
    }
    out.push({
      home: home.team.displayName,
      away: away.team.displayName,
      home_score: homeScore,
      away_score: awayScore,
    });
  }
  return out;
}

async function readBoard(path: string, date: string): Promise<RecordedFinal[]> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 8000);
  try {
    const res = await fetch(
      `https://site.api.espn.com/apis/site/v2/sports/football/${path}/scoreboard?dates=${date.replaceAll("-", "")}&limit=400${path === "college-football" ? "&groups=80" : ""}`,
      { signal: ctrl.signal, headers: { "user-agent": "caliber-desk/1.0" }, cache: "no-store" },
    );
    if (!res.ok) return [];
    return finalsFromScoreboard(await res.json());
  } catch {
    return [];
  } finally {
    clearTimeout(timer);
  }
}

const finalsCache = new Map<string, { at: number; finals: RecordedFinal[] }>();
const finalsInflight = new Map<string, Promise<RecordedFinal[]>>();

function finalsFreshMs(date: string): number {
  return date < slateToday() ? 30 * 60_000 : 90_000;
}

function readFinals(date: string): { at: number; finals: RecordedFinal[] } | null {
  const stored = readJsonFile<{ savedAt: number; finals: RecordedFinal[] }>(cacheFile(`finals-${date}`));
  if (!stored || typeof stored.savedAt !== "number" || !Array.isArray(stored.finals)) return null;
  return { at: stored.savedAt, finals: stored.finals };
}

function loadFinals(date: string): Promise<RecordedFinal[]> {
  const existing = finalsInflight.get(date);
  if (existing) return existing;
  const job = Promise.all([readBoard("nfl", date), readBoard("college-football", date)])
    .then(([nfl, college]) => {
      const finals = [...nfl, ...college];
      const at = Date.now();
      finalsCache.set(date, { at, finals });
      writeJsonFile(cacheFile(`finals-${date}`), { savedAt: at, finals });
      return finals;
    })
    .finally(() => {
      finalsInflight.delete(date);
    });
  finalsInflight.set(date, job);
  return job;
}

/** Final NFL and FBS scores for one slate day. A saved card returns immediately. */
export async function finalsForDate(date: string): Promise<RecordedFinal[]> {
  let hit = finalsCache.get(date) ?? null;
  if (!hit) {
    const disk = readFinals(date);
    if (disk) {
      finalsCache.set(date, disk);
      hit = disk;
    }
  }
  if (hit && Date.now() - hit.at < finalsFreshMs(date)) return hit.finals;
  if (hit) {
    void loadFinals(date).catch(() => {
      // The scores already on the card stay up.
    });
    return hit.finals;
  }
  return loadFinals(date);
}
