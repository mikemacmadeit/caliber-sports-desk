/** Plain-language labels for desk status codes shown to operators. */

const REASON_LABELS: Record<string, string> = {
  RESEARCH_ONLY: "Research only — not a live play",
  PLAY_DISABLED: "Plays are turned off for this slate",
  INJURY_COVERAGE_INCOMPLETE: "Injury coverage is incomplete",
  NEVER_ASSUME_HEALTHY: "Missing injury data does not mean healthy",
  NO_BOOK_PRICE: "No book price on file",
  LOW_EV: "Edge after haircut is too thin",
  DEMO_ONLY: "Demo data — not for production calls",
  DATA_TIER_RESEARCH: "Data tier is research, not validated",
  WEATHER_MISSING: "Weather is missing",
  THIN_SAMPLE: "Sample is too thin",
};

const VALIDATION_LABELS: Record<string, string> = {
  unverified: "Not verified",
  verified: "Verified",
  rejected: "Rejected",
  pending: "Pending review",
};

const SCENARIO_LABELS: Record<string, string> = {
  speculative: "Speculative",
  confirmed: "Confirmed",
  rejected: "Rejected",
};

const CLAIM_LABELS: Record<string, string> = {
  fact: "Fact",
  inference: "Inference",
  opinion: "Opinion",
};

const TOPIC_LABELS: Record<string, string> = {
  weather: "Weather",
  injury: "Injury",
  usage: "Usage",
  matchup: "Matchup",
  market: "Market",
  lineup: "Lineup",
};

export function reasonLabel(code: string): string {
  return REASON_LABELS[code] || code.replaceAll("_", " ").toLowerCase();
}

export function validationLabel(status: string): string {
  return VALIDATION_LABELS[status] || status.replaceAll("_", " ");
}

export function scenarioLabel(status: string): string {
  return SCENARIO_LABELS[status] || status.replaceAll("_", " ");
}

export function claimLabel(type: string): string {
  return CLAIM_LABELS[type] || type.replaceAll("_", " ");
}

export function topicLabel(topic: string): string {
  return TOPIC_LABELS[topic] || topic.replaceAll("_", " ");
}

export function recommendationCopy(status: string | null | undefined): {
  title: string;
  tone: "play" | "hold" | "demo";
} {
  if (status === "play") return { title: "Play", tone: "play" };
  if (status === "demo_play") return { title: "Demo only", tone: "demo" };
  return { title: "No bet", tone: "hold" };
}
