import { ticketName } from "@/lib/teams";

export type Grade = "correct" | "miss" | "push" | null | undefined;

export type CallFacts = {
  home_team: string;
  away_team: string;
  pick_label?: string | null;
  pick_probability?: number | null;
  pick_market?: string | null;
  pick_team?: string | null;
  favored_side?: string | null;
  ats_pick_label?: string | null;
  ats_side?: "home" | "away" | null;
  ats_team?: string | null;
  ats_line?: number | null;
  ats_cover_margin?: number | null;
  predicted_margin?: number | null;
  predicted_home_score?: number | null;
  predicted_away_score?: number | null;
  projection_withheld?: boolean;
  game_status?: string | null;
  pick_result?: Grade;
  ats_pick_result?: Grade;
};

/** A cover inside a point is the model agreeing with the posted number. */
export const QUIET_COVER = 1;

export function timesInHundred(p: number | null | undefined): string | null {
  if (p == null || Number.isNaN(p)) return null;
  const n = Math.round(p * 100);
  if (n <= 0) return "almost never in 100";
  if (n >= 100) return "almost always";
  return `about ${n} times in 100`;
}

export function pointsText(n: number): string {
  const rounded = Math.abs(Math.round(n * 10) / 10);
  return Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1);
}

export function spreadLineText(line: number | null | undefined): string | null {
  if (line == null || Number.isNaN(line)) return null;
  const rounded = Math.round(line * 10) / 10;
  if (Math.abs(rounded) < 0.05) return "PK";
  const body = pointsText(rounded);
  return rounded > 0 ? `+${body}` : `-${body}`;
}

export function chanceCount(p: number | null | undefined): string | null {
  if (p == null || Number.isNaN(p)) return null;
  const n = Math.round(p * 100);
  if (n <= 0) return "Rare";
  if (n >= 100) return "Near lock";
  return `${n} in 100`;
}

export type PickTicket = {
  kicker: string;
  title: string;
  detail: string;
  side: "home" | "away" | null;
  mark: string | null;
  result: Grade;
  empty: boolean;
};

export type PickTickets = {
  moneyline: PickTicket;
  spread: PickTicket;
  score: string | null;
  split: boolean;
  settled: boolean;
};

export function pickTickets(row: CallFacts): PickTickets {
  const settled = row.game_status === "final";
  const score =
    row.projection_withheld || row.predicted_away_score == null || row.predicted_home_score == null
      ? null
      : `Projected ${Math.round(row.predicted_away_score)}–${Math.round(row.predicted_home_score)}`;

  if (row.projection_withheld || row.pick_market === "withheld") {
    const blank = (kicker: string): PickTicket => ({
      kicker,
      title: "No pick",
      detail: "Uneven history",
      side: null,
      mark: null,
      result: null,
      empty: true,
    });
    return { moneyline: blank("Moneyline"), spread: blank("Spread"), score: null, split: false, settled };
  }

  const mlSide = row.favored_side === "home" || row.favored_side === "away" ? row.favored_side : null;
  const mlTeam = (row.pick_team || (row.pick_label || "").replace(/ to win$/i, "")).trim();
  const mlName = ticketName(mlTeam);
  const moneyline: PickTicket = {
    kicker: "Moneyline",
    title: mlName || "No pick",
    detail: settled ? resultWord(row.pick_result, "Won", "Lost") : chanceCount(row.pick_probability) || "Chance unpublished",
    side: mlName ? mlSide : null,
    mark: mlName ? "ML" : null,
    result: settled ? row.pick_result : null,
    empty: !mlName,
  };

  const line = spreadLineText(row.ats_line);
  const spreadTeam = (row.ats_team || "").trim();
  const spreadName = ticketName(spreadTeam);
  const spreadTitle = spreadName && line ? `${spreadName} ${line}` : row.ats_pick_label || "";
  const gap = row.ats_cover_margin;
  const quiet = gap == null || Number.isNaN(gap) || Math.abs(gap) < QUIET_COVER;
  const spread: PickTicket = spreadTitle
    ? {
        kicker: "Spread",
        title: spreadTitle,
        detail: settled
          ? resultWord(row.ats_pick_result, "Covered", "Missed")
          : quiet
            ? "On the number"
            : `Cover by ${pointsText(gap)}`,
        side: row.ats_side === "home" || row.ats_side === "away" ? row.ats_side : null,
        mark: line,
        result: settled ? row.ats_pick_result : null,
        empty: false,
      }
    : {
        kicker: "Spread",
        title: "No number",
        detail: "Spread not posted",
        side: null,
        mark: null,
        result: null,
        empty: true,
      };

  return {
    moneyline,
    spread,
    score: settled ? null : score,
    split: moneyline.side != null && spread.side != null && moneyline.side !== spread.side,
    settled,
  };
}

function resultWord(result: Grade, hit: string, miss: string): string {
  if (result === "correct") return hit;
  if (result === "miss") return miss;
  if (result === "push") return "Push";
  return "Pending";
}

export function gradeWord(result: Grade): string {
  if (result === "correct") return "right";
  if (result === "miss") return "wrong";
  if (result === "push") return "push";
  return "pending";
}

export function callCopy(row: CallFacts): {
  winSentence: string;
  coverSentence: string | null;
  loud: boolean;
  score: string | null;
} {
  if (row.projection_withheld) {
    return {
      winSentence: "No score is published. One club is missing the seasons the other one has.",
      coverSentence: null,
      loud: false,
      score: null,
    };
  }

  const times = timesInHundred(row.pick_probability);
  const winner = (row.pick_label || "").replace(/ to win$/i, "");
  const winSentence =
    row.pick_market === "straight_up" && winner && times
      ? `${winner} to win, ${times}.`
      : row.pick_label
        ? times
          ? `${row.pick_label}, ${times}.`
          : `${row.pick_label}.`
        : "No winner published yet.";

  const score =
    row.predicted_away_score == null || row.predicted_home_score == null
      ? null
      : `Projected ${Math.round(row.predicted_away_score)}–${Math.round(row.predicted_home_score)}`;

  if (!row.ats_pick_label) {
    return { winSentence, coverSentence: null, loud: false, score };
  }

  const gap = row.ats_cover_margin;
  const quiet = gap == null || Number.isNaN(gap) || Math.abs(gap) < QUIET_COVER;
  const coverSentence = quiet
    ? `${row.ats_pick_label} is essentially the model's score.`
    : `${row.ats_pick_label}, projected to cover by ${pointsText(gap)}.`;
  const winSide = row.predicted_margin == null ? null : row.predicted_margin >= 0 ? "home" : "away";
  const split = winSide != null && row.ats_side != null && winSide !== row.ats_side;
  return {
    winSentence,
    coverSentence: split ? `${coverSentence} The win and the number name different teams.` : coverSentence,
    loud: !quiet,
    score,
  };
}

export function ledgerLines(row: CallFacts & { pick_result?: Grade; ats_pick_result?: Grade }): {
  win: string;
  cover: string | null;
} {
  const winLabel = row.projection_withheld ? "No projection" : row.pick_label || "To win";
  return {
    win: `${winLabel}: ${gradeWord(row.pick_result)}.`,
    cover: row.ats_pick_label ? `${row.ats_pick_label}: ${gradeWord(row.ats_pick_result)}.` : null,
  };
}

/** One line a reader can say out loud: the chance and what the ticket pays. */
export function ticketLine(p: number, priceAmerican: number | null): string {
  const oneIn = p > 0.02 && p < 0.5 ? Math.max(2, Math.round(1 / p)) : null;
  const chance = oneIn != null && oneIn >= 3 ? `about 1 in ${oneIn}` : timesInHundred(p) || "the chance";
  const lead = chance.charAt(0).toUpperCase() + chance.slice(1);
  if (priceAmerican == null) return `${lead}.`;
  const price = priceAmerican > 0 ? `+${priceAmerican}` : String(priceAmerican);
  return `${lead}, pays ${price}.`;
}
