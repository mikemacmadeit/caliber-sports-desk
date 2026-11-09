export type Grade = "correct" | "miss" | "push" | null | undefined;

export type CallFacts = {
  home_team: string;
  away_team: string;
  pick_label?: string | null;
  pick_probability?: number | null;
  pick_market?: string | null;
  ats_pick_label?: string | null;
  ats_side?: "home" | "away" | null;
  ats_cover_margin?: number | null;
  predicted_margin?: number | null;
  predicted_home_score?: number | null;
  predicted_away_score?: number | null;
  projection_withheld?: boolean;
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
