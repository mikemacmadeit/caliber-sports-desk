import { cacheFile, readJsonFile, writeJsonFile } from "@/lib/diskJson";
import { slateToday } from "@/lib/days";

const COUNTING = new Set(["receptions", "receiving_yards", "rushing_yards"]);
const LIVE_FRESH_MS = 90_000;
const FINAL_FRESH_MS = 30 * 60_000;

type Entry = { actual: number; team: string };
type StatIndex = Map<string, Entry[]>;
type SlateStats = { final: StatIndex; live: StatIndex; played: Set<string>; livePlayed: Set<string> };

const cache = new Map<string, { at: number; stats: SlateStats }>();
const inflight = new Map<string, Promise<SlateStats>>();

type StoredStats = {
  savedAt: number;
  final: [string, Entry[]][];
  live: [string, Entry[]][];
  played: string[];
  livePlayed: string[];
};

function norm(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9]/g, "");
}

function grade(side: string, line: number, actual: number): "win" | "loss" | "push" {
  if (Math.abs(actual - line) < 1e-9) return "push";
  const over = actual > line;
  if (side === "over") return over ? "win" : "loss";
  return over ? "loss" : "win";
}

function dateOf(path: string, body: unknown): string {
  const query = new URLSearchParams(path.split("?")[1] || "");
  const named = query.get("date");
  if (named && /^\d{4}-\d{2}-\d{2}$/.test(named)) return named;
  if (body && typeof body === "object" && typeof (body as { date?: unknown }).date === "string") {
    const date = (body as { date: string }).date;
    if (/^\d{4}-\d{2}-\d{2}$/.test(date)) return date;
  }
  return slateToday();
}

function remember(index: StatIndex, name: string, team: string, market: string, actual: number) {
  const key = `${norm(name)}|${market}`;
  const list = index.get(key) ?? [];
  list.push({ actual, team: team.toUpperCase() });
  index.set(key, list);
}

function lookup(index: StatIndex, name: string, team: string, market: string): number | null {
  const list = index.get(`${norm(name)}|${market}`);
  if (!list?.length) return null;
  const club = team.toUpperCase();
  const sameTeam = club ? list.filter((entry) => entry.team === club) : [];
  const chosen = sameTeam.length ? sameTeam : list;
  if (chosen.every((entry) => entry.actual === chosen[0].actual)) return chosen[0].actual;
  return null;
}

function absorb(payload: unknown, index: StatIndex, played: Set<string>) {
  const teams = (payload as { boxscore?: { players?: unknown[] } })?.boxscore?.players;
  if (!Array.isArray(teams)) return;
  for (const team of teams) {
    const club = String((team as { team?: { abbreviation?: string } }).team?.abbreviation || "");
    for (const group of (team as { statistics?: unknown[] }).statistics || []) {
      const kind = String((group as { name?: string }).name || "").toLowerCase();
      if (kind !== "receiving" && kind !== "rushing") continue;
      const labels = ((group as { labels?: unknown[] }).labels || []).map((label) => String(label).toUpperCase());
      for (const athlete of (group as { athletes?: unknown[] }).athletes || []) {
        const person = athlete as { athlete?: { displayName?: string }; stats?: unknown[] };
        const name = person.athlete?.displayName;
        if (!name) continue;
        played.add(`${norm(name)}|${club}`);
        const values = new Map(labels.map((label, i) => [label, person.stats?.[i]]));
        const read = (label: string) => {
          const value = Number(values.get(label));
          return Number.isFinite(value) ? value : null;
        };
        if (kind === "rushing") {
          const yards = read("YDS");
          if (yards != null) remember(index, name, club, "rushing_yards", yards);
        }
        if (kind === "receiving") {
          const yards = read("YDS");
          const catches = read("REC");
          if (yards != null) remember(index, name, club, "receiving_yards", yards);
          if (catches != null) remember(index, name, club, "receptions", catches);
        }
      }
    }
  }
}

async function readJson(url: string): Promise<unknown | null> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 8000);
  try {
    const res = await fetch(url, { signal: ctrl.signal, headers: { "user-agent": "caliber-desk/1.0" }, cache: "no-store" });
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

function eventIds(board: unknown, want: (status: string) => boolean): { league: "nfl" | "college-football"; id: string }[] {
  const events = (board as { events?: unknown[] })?.events;
  if (!Array.isArray(events)) return [];
  const ids: { league: "nfl" | "college-football"; id: string }[] = [];
  for (const event of events) {
    const item = event as { id?: string; competitions?: { status?: { type?: { name?: string } } }[] };
    const name = item.competitions?.[0]?.status?.type?.name || "";
    if (!item.id || !want(name)) continue;
    ids.push({ league: "nfl", id: String(item.id) });
  }
  return ids;
}

function isFinalStatus(status: string): boolean {
  return status.startsWith("STATUS_FINAL");
}

function isLiveStatus(status: string): boolean {
  return status === "STATUS_IN_PROGRESS" || status === "STATUS_HALFTIME" || status.startsWith("STATUS_END");
}

async function loadIndex(
  boards: unknown[],
  leagueFor: (index: number) => "nfl" | "college-football",
  want: (status: string) => boolean,
  played?: Set<string>,
): Promise<StatIndex> {
  const events = boards.flatMap((board, index) =>
    eventIds(board, want).map((event) => ({ ...event, league: leagueFor(index) })),
  );
  const boxes = await Promise.all(
    events.map((event) =>
      readJson(`https://site.api.espn.com/apis/site/v2/sports/football/${event.league}/summary?event=${event.id}`),
    ),
  );
  const stats: StatIndex = new Map();
  for (const box of boxes) if (box) absorb(box, stats, played ?? new Set());
  return stats;
}

function freshWindow(date: string): number {
  return date < slateToday() ? FINAL_FRESH_MS : LIVE_FRESH_MS;
}

function unpack(stored: StoredStats | null): { at: number; stats: SlateStats } | null {
  if (!stored || typeof stored.savedAt !== "number" || !Array.isArray(stored.final) || !Array.isArray(stored.live)) return null;
  return {
    at: stored.savedAt,
    stats: {
      final: new Map(stored.final),
      live: new Map(stored.live),
      played: new Set(stored.played || []),
      livePlayed: new Set(stored.livePlayed || []),
    },
  };
}

function readSnapshot(date: string): { at: number; stats: SlateStats } | null {
  return unpack(readJsonFile<StoredStats>(cacheFile(`props-${date}`)));
}

function writeSnapshot(date: string, stats: SlateStats, savedAt: number): void {
  const stored: StoredStats = {
    savedAt,
    final: [...stats.final],
    live: [...stats.live],
    played: [...stats.played],
    livePlayed: [...stats.livePlayed],
  };
  writeJsonFile(cacheFile(`props-${date}`), stored);
}

async function fetchStats(date: string): Promise<SlateStats> {
  const stamp = date.replaceAll("-", "");
  const boards = await Promise.all([
    readJson(`https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?dates=${stamp}&limit=50`),
    readJson(`https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard?dates=${stamp}&limit=400&groups=80`),
  ]);
  const leagueFor = (index: number) => (index === 0 ? "nfl" : "college-football") as "nfl" | "college-football";
  const played = new Set<string>();
  const livePlayed = new Set<string>();
  const [final, live] = await Promise.all([
    loadIndex(boards, leagueFor, isFinalStatus, played),
    loadIndex(boards, leagueFor, isLiveStatus, livePlayed),
  ]);
  return { final, live, played, livePlayed };
}

function loadFresh(date: string): Promise<SlateStats> {
  const existing = inflight.get(date);
  if (existing) return existing;
  const job = fetchStats(date)
    .then((stats) => {
      if (stats.final.size === 0 && stats.live.size === 0 && stats.played.size === 0) return stats;
      const at = Date.now();
      cache.set(date, { at, stats });
      writeSnapshot(date, stats, at);
      return stats;
    })
    .finally(() => {
      inflight.delete(date);
    });
  inflight.set(date, job);
  return job;
}

function statsReady(date: string): SlateStats | null {
  const freshMs = freshWindow(date);
  let hit = cache.get(date) ?? null;
  if (!hit) {
    const disk = readSnapshot(date);
    if (disk) {
      cache.set(date, disk);
      hit = disk;
    }
  }
  if (!hit) {
    void loadFresh(date).catch(() => {
      // The board paints now. Grades fill in on the next open.
    });
    return null;
  }
  if (Date.now() - hit.at >= freshMs) {
    void loadFresh(date).catch(() => {
      // The saved box stays on screen.
    });
  }
  return hit.stats;
}

async function statsForDate(date: string): Promise<SlateStats> {
  const freshMs = freshWindow(date);
  let hit = cache.get(date) ?? null;
  if (!hit) {
    const disk = readSnapshot(date);
    if (disk) {
      cache.set(date, disk);
      hit = disk;
    }
  }
  if (hit && Date.now() - hit.at < freshMs) return hit.stats;
  if (hit) {
    void loadFresh(date).catch(() => {
      // The saved box stays on screen. The next click tries again.
    });
    return hit.stats;
  }
  return loadFresh(date);
}

function seen(played: Set<string>, name: string, team: string): boolean {
  const club = team.toUpperCase();
  const key = `${norm(name)}|${club}`;
  if (played.has(key)) return true;
  const hits = [...played].filter((item) => item.startsWith(`${norm(name)}|`));
  return hits.length === 1;
}

function pricedUnits(price: number, result: string): number | undefined {
  if (result === "loss") return -1;
  if (result === "push") return 0;
  if (result === "win") return price < 0 ? 100 / Math.abs(price) : price / 100;
  return undefined;
}

function applyCounting(row: Record<string, unknown>, stats: SlateStats): boolean {
  const market = String(row.market_id || "");
  if (!COUNTING.has(market)) return false;
  const side = String(row.side || "");
  const line = row.line;
  const name = String(row.player_name || "");
  if ((side !== "over" && side !== "under") || typeof line !== "number" || !name) return false;
  const team = String(row.team_abbr || row.team || "");
  const actual = lookup(stats.final, name, team, market);
  const finished = row.game_status === "final" || row.game_status === "post";
  if (actual == null && finished && seen(stats.played, name, team)) {
    row.actual = 0;
    row.result = grade(side, line, 0);
    row.game_status = "final";
    return true;
  }
  if (actual == null) {
    const liveActual = lookup(stats.live, name, team, market);
    const onField = liveActual != null || seen(stats.livePlayed, name, team);
    if (onField) {
      const current = liveActual ?? 0;
      row.actual = current;
      // Receptions and yards only rise, so a line already cleared is decided.
      row.result = current > line ? grade(side, line, current) : "pending";
      return true;
    }
    // A finished box lists everyone who played. A stored zero for a name that
    // never appears is a player who did not play, not a real stat line.
    if (finished && stats.played.size > 0 && row.actual === 0) {
      row.result = "void";
      row.actual = null;
      return true;
    }
    return false;
  }
  row.actual = actual;
  row.result = grade(side, line, actual);
  row.game_status = "final";
  return true;
}

function ticketResult(legs: Record<string, unknown>[]): "win" | "loss" | "push" | "pending" {
  const results = legs.map((leg) => String(leg.result || "pending"));
  if (!results.length) return "pending";
  if (results.includes("loss")) return "loss";
  if (results.some((result) => result !== "win" && result !== "push")) return "pending";
  if (results.includes("win")) return "win";
  return "push";
}

function correctBody(body: Record<string, unknown>, stats: SlateStats, swap: boolean) {
  for (const key of ["calls", "rows", "picks", "day", "prices"] as const) {
    const rows = body[key];
    if (!Array.isArray(rows)) continue;
    for (const row of rows) {
      if (!row || typeof row !== "object") continue;
      const call = row as Record<string, unknown>;
      if (applyCounting(call, stats) && typeof call.book_price === "number" && typeof call.result === "string") {
        const units = pricedUnits(call.book_price, call.result);
        if (units != null && "units" in call) call.units = units;
        if (typeof call.detail === "string") {
          const actual = typeof call.actual === "number" ? `Actual ${call.actual}` : "";
          const parts = call.detail.split(" · ").filter((part) => part && !part.startsWith("Actual "));
          if (actual) parts.push(actual);
          call.detail = parts.join(" · ");
        }
      }
    }
  }
  const tickets = body.tickets;
  if (!Array.isArray(tickets)) return;
  for (const ticket of tickets) {
    if (!ticket || typeof ticket !== "object") continue;
    const legs = (ticket as { legs?: unknown[] }).legs;
    if (!Array.isArray(legs)) continue;
    let changed = false;
    for (const leg of legs) {
      if (leg && typeof leg === "object") {
        const call = leg as Record<string, unknown>;
        if (applyCounting(call, stats)) changed = true;
      }
    }
    if (changed) (ticket as { result?: string }).result = ticketResult(legs as Record<string, unknown>[]);
  }
}

function wantsOfficialGrade(path: string): boolean {
  const pathname = path.split("?")[0];
  if (pathname === "/v1/props" && new URLSearchParams(path.split("?")[1] || "").get("market") === "anytime_td") {
    return false;
  }
  return (
    pathname === "/v1/props" ||
    pathname === "/v1/props/desk" ||
    pathname === "/v1/props/picks" ||
    pathname === "/v1/parlays/card" ||
    pathname === "/v1/best-bets" ||
    pathname === "/v1/published/day"
  );
}

function heavyBoard(path: string): boolean {
  return path.split("?")[0] === "/v1/props";
}

/** Replace a frozen live number with the final box for counting props. */
export async function correctPropPayload<T>(path: string, value: T): Promise<T> {
  if (!wantsOfficialGrade(path) || !value || typeof value !== "object") return value;
  try {
    const date = dateOf(path, value);
    // The full price list is large. Grade it only when the box is already saved.
    const stats = heavyBoard(path) ? statsReady(date) : await statsForDate(date);
    if (stats) correctBody(value as Record<string, unknown>, stats, !heavyBoard(path));
  } catch {
    return value;
  }
  return value;
}

export async function correctPropBody(path: string, body: string): Promise<string> {
  if (!wantsOfficialGrade(path)) return body;
  if (heavyBoard(path) && !statsReady(dateOf(path, null))) return body;
  try {
    const value = JSON.parse(body) as unknown;
    await correctPropPayload(path, value);
    return JSON.stringify(value);
  } catch {
    return body;
  }
}
