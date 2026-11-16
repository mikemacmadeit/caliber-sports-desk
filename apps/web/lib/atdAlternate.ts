import type { PropRow, TdCustomerExplanation, TdDecisionCandidate } from "@/lib/api";

/** A saved card keeps its graded alternate. An open game uses a real scoring-area role. */
export function withScoringRoleAlternate(row: PropRow): PropRow {
  const decision = row.decision;
  const current = decision?.value_candidate;
  if (!decision || !current || current.player_id === row.player_id) return row;
  const recorded = row.decision_results?.value_candidate?.result;
  const status = row.game_status || "";
  if (recorded === "scored" || recorded === "blank" || status === "final" || status === "in_progress") return row;
  if (current.football_case?.supported !== false) return row;
  const role = decision.alternatives
    .filter((candidate) => candidate.player_id !== row.player_id && candidate.football_case?.supported)
    .sort((a, b) =>
      (b.research_rank?.scoring_area_opportunities ?? 0) - (a.research_rank?.scoring_area_opportunities ?? 0)
      || b.probability - a.probability
      || a.player_id.localeCompare(b.player_id))[0];
  if (!role) return row;
  const value: TdDecisionCandidate = {
    ...role,
    research_selected: true,
    customer: role.customer ?? alternateCopy(role, decision.most_likely, current),
  };
  const report = row.desk_analysis?.scouting_report;
  const comparison = report?.customer?.comparison;
  const customer = comparison?.heading.includes(current.name) && report?.customer
    ? {
        ...report.customer,
        comparison: {
          heading: `Why ${row.player_name} over ${role.name}?`,
          text: `${role.name} is the alternate because he has a measured scoring-area role. ${current.name} stays off that spot: his chance does not come from goal-line or red-zone work.`,
        },
      }
    : report?.customer;
  return {
    ...row,
    decision: {
      ...decision,
      value_candidate: value,
      alternative_status: value.price_supported ? "qualified" : "research_only",
      alternative_note: "The alternate has a measured scoring-area role. A higher chance from ordinary touches is not used.",
    },
    desk_analysis: report ? { ...row.desk_analysis, scouting_report: { ...report, customer } } : row.desk_analysis,
  };
}

function alternateCopy(pick: TdDecisionCandidate, leader: TdDecisionCandidate, replaced: TdDecisionCandidate): TdCustomerExplanation {
  const reasons = pick.football_case?.reasons ?? [];
  const scoring = reasons.find((reason) => reason.kind === "scoring_area")?.text;
  const texts = reasons.map((reason) => reason.text).filter(Boolean);
  const chance = Math.round(pick.probability * 100);
  const price = pick.american != null && pick.break_even != null
    ? `The saved price needs ${Math.round(pick.break_even * 100)}% to break even; our estimate is ${chance}%. We have not established that this price is worth betting.`
    : "We do not have a usable saved price to judge whether this player is worth betting.";
  return {
    version: "customer_td_v1",
    label: "Alternate pick",
    chance: `We estimate a ${chance}% chance of a touchdown.`,
    why: scoring || texts[0] || "He has a measured role close to the end zone.",
    matchup: texts.find((text) => text !== scoring) || "The case is his scoring-area role, not his total touches.",
    caution: `${leader.name} remains the more likely scorer at ${Math.round(leader.probability * 100)}%. ${replaced.name} is not the alternate because that chance does not come from goal-line or red-zone work.`,
    price,
    evidence: texts,
    comparison: {
      heading: `Why ${pick.name} over ${replaced.name}?`,
      text: `${replaced.name} is estimated at ${Math.round(replaced.probability * 100)}%. ${pick.name} is the alternate because the red-zone and goal-line work is his.`,
    },
    research_note: "These estimates are still being tested; better results have not yet been demonstrated.",
  };
}
