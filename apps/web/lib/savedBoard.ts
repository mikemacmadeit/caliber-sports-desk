import { createHash } from "crypto";
import { mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "fs";
import path from "path";
import { canonicalBoardPath, isSavedBoardPath, savedBoardIsPublic } from "@/lib/savedBoardPaths";

export { isSavedBoardPath };

/** Serve a saved board instantly. Refresh it in the background after this age. */
const FRESH_MS = 2 * 60 * 1000;
const MAX_AGE_MS = 2 * 60 * 1000;

const LIST_KEYS = ["rows", "calls", "picks", "day", "games", "dates", "tickets", "spreads", "prices"] as const;

type Meta = { savedAt: number; count: number };
export type SavedBoard = { body: string; fresh: boolean };

const refreshing = new Set<string>();

function filesFor(requestPath: string): { meta: string; body: string } {
  const digest = createHash("sha256").update(canonicalBoardPath(requestPath)).digest("hex").slice(0, 24);
  const dir = path.join(process.cwd(), ".cache", "boards");
  return { meta: path.join(dir, `${digest}.meta.json`), body: path.join(dir, `${digest}.body.json`) };
}

function contentCount(data: Record<string, unknown>): number {
  let count = 0;
  for (const key of LIST_KEYS) {
    const value = data[key];
    if (Array.isArray(value)) count += value.length;
  }
  const score = data.atd_scorecard;
  if (score && typeof score === "object") {
    const card = score as { rows?: unknown; open_and_settled_takes?: unknown };
    if (Array.isArray(card.rows)) count += card.rows.length;
    if (Array.isArray(card.open_and_settled_takes)) count += card.open_and_settled_takes.length;
  }
  if (data.game && typeof data.game === "object") count += 1;
  return count;
}

function saveableCount(body: string): number | null {
  try {
    const data = JSON.parse(body) as unknown;
    if (!data || typeof data !== "object" || Array.isArray(data)) return null;
    const record = data as Record<string, unknown>;
    const count = contentCount(record);
    if (count > 0) return count;
    const shaped =
      LIST_KEYS.some((key) => Array.isArray(record[key])) || Boolean(record.game) || Boolean(record.atd_scorecard);
    return shaped ? 0 : null;
  } catch {
    return null;
  }
}

function readMeta(file: string): Meta | null {
  try {
    const parsed = JSON.parse(readFileSync(file, "utf8")) as Meta;
    if (!parsed || typeof parsed.savedAt !== "number") return null;
    return parsed;
  } catch {
    return null;
  }
}

export function readSavedBoard(requestPath: string): SavedBoard | null {
  if (!isSavedBoardPath(requestPath)) return null;
  try {
    const files = filesFor(requestPath);
    const meta = readMeta(files.meta);
    if (!meta) return null;
    const age = Date.now() - meta.savedAt;
    if (age < 0 || age > MAX_AGE_MS) return null;
    const body = readFileSync(files.body, "utf8");
    if (!savedBoardIsPublic(requestPath, body)) return null;
    return { body, fresh: age <= FRESH_MS };
  } catch {
    return null;
  }
}

export function writeSavedBoard(requestPath: string, body: string): void {
  if (!isSavedBoardPath(requestPath) || !savedBoardIsPublic(requestPath, body)) return;
  const count = saveableCount(body);
  if (count == null) return;
  try {
    const files = filesFor(requestPath);
    mkdirSync(path.dirname(files.body), { recursive: true });
    const tmp = `${files.body}.tmp`;
    writeFileSync(tmp, body);
    rmSync(files.body, { force: true });
    renameSync(tmp, files.body);
    writeFileSync(files.meta, JSON.stringify({ savedAt: Date.now(), count } satisfies Meta));
  } catch {
    // A cache write must not turn a good board into a failed page.
  }
}

export function claimRefresh(requestPath: string): boolean {
  const key = canonicalBoardPath(requestPath);
  if (refreshing.has(key)) return false;
  refreshing.add(key);
  return true;
}

export function finishRefresh(requestPath: string): void {
  refreshing.delete(canonicalBoardPath(requestPath));
}
