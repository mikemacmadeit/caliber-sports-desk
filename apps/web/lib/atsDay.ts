import type { AtsRow } from "@/components/AtsBoard";
import { kickoffDate } from "@/lib/format";

/** The API owns the selection, its lock and its grade. Preserve its sort order. */
export async function callsForDate(live: AtsRow[], date: string, league: string): Promise<AtsRow[]> {
  return live.filter(row => kickoffDate(row.kickoff_utc) === date &&
    (league === "all" || row.league_id === league));
}
