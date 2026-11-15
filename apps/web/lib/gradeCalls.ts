import type { Grade } from "@/lib/calls";

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
};

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
  if (home == null || away == null) return row;
  const next: T = { ...row, game_status: "final", home_score: home, away_score: away };
  if (
    row.pick_market !== "withheld" &&
    !row.projection_withheld &&
    (row.favored_side === "home" || row.favored_side === "away")
  ) {
    next.pick_result = moneyline(home, away, row.favored_side);
  }
  const line = homeHandicap(row);
  if (line != null && (row.ats_side === "home" || row.ats_side === "away")) {
    next.ats_pick_result = spread(home, away, line, row.ats_side);
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

/** Final NFL and FBS scores for one slate day. A miss leaves the card ungraded. */
export async function finalsForDate(date: string): Promise<RecordedFinal[]> {
  const [nfl, college] = await Promise.all([readBoard("nfl", date), readBoard("college-football", date)]);
  return [...nfl, ...college];
}
