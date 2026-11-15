"""Historical play-calling and protection features; no claimed future game plan."""
METRICS = ["leading_pass_rate", "trailing_pass_rate", "close_pass_rate", "redzone_pass_rate",
           "goal_line_pass_rate", "early_down_pass_rate", "late_lead_pass_rate", "sack_rate", "short_yardage_success"]
SITUATIONAL_FEATURES = [f"{side}_{key}" for side in ("offense", "defense") for key in METRICS]
SITUATIONAL_FEATURES += ["offense_situation_known", "defense_situation_known", "historical_qb_change", "primary_passer_share"]


def features(offense, defense):
    result = {k: None for k in SITUATIONAL_FEATURES}
    for side, rows in (("offense", offense), ("defense", defense)):
        result[side+"_situation_known"] = float(len(rows) >= 2)
        if len(rows) < 2:
            continue
        for key in METRICS:
            pairs = [(0.8**i, r.get(key+"_n", 0), r.get(key+"_d", 0)) for i,r in enumerate(reversed(rows))]
            # Small situational samples stay missing, rather than extreme 0/1 estimates.
            if sum(d for _,_,d in pairs) >= (10 if key != "goal_line_pass_rate" else 5):
                result[side+"_"+key] = sum(w*n for w,n,_ in pairs)/sum(w*d for w,_,d in pairs)
    if len(offense) >= 2:
        last, previous = offense[-1], offense[-2]
        if all(r.get("passer_coverage", 0) >= .95 and r.get("primary_passer") for r in (last,previous)):
            result["historical_qb_change"] = float(last["primary_passer"] != previous["primary_passer"])
            result["primary_passer_share"] = last["primary_passer_share"]
    return result


def explanations(f):
    result = []
    lead, trail = f.get("offense_leading_pass_rate"), f.get("offense_trailing_pass_rate")
    if lead is not None and trail is not None:
        result.append(f"Historical play-calling: {lead:.0%} pass plays when ahead by 8+ points, {trail:.0%} when behind by 8+. This is a tendency, not a confirmed game plan.")
    goal = f.get("offense_goal_line_pass_rate")
    if goal is not None:
        result.append(f"Inside the five, this offense called pass plays on {goal:.0%} of measured opportunities.")
    if f.get("historical_qb_change") == 1:
        result.append("The primary passer changed between the last two measured games; this week's starter still requires confirmation.")
    return result
