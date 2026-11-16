import { mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "fs";
import path from "path";

/** Local JSON cache. A bad read or write falls through to the network. */
export function cacheFile(name: string): string {
  const safe = name.replace(/[^a-z0-9._-]/gi, "_");
  return path.join(process.cwd(), ".cache", "grades", `${safe}.json`);
}

export function readJsonFile<T>(file: string): T | null {
  try {
    return JSON.parse(readFileSync(file, "utf8")) as T;
  } catch {
    return null;
  }
}

export function writeJsonFile(file: string, value: unknown): void {
  try {
    mkdirSync(path.dirname(file), { recursive: true });
    const tmp = `${file}.tmp`;
    writeFileSync(tmp, JSON.stringify(value));
    rmSync(file, { force: true });
    renameSync(tmp, file);
  } catch {
    // The page still has the in-memory copy.
  }
}
