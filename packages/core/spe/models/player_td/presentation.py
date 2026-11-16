"""Trim internal feature matrices from customer cards without changing saved predictions."""
def card_payload(payload):
    result = dict(payload)
    result["rows"] = []
    for original in payload.get("rows", []):
        row = dict(original)
        analysis = original.get("desk_analysis") or {}
        row["desk_analysis"] = {k: analysis[k] for k in (
            "scouting_report", "reasons", "risks", "sources", "evidence_timing",
            "combination_status", "situational_features_active",
        ) if k in analysis}
        report = analysis.get("scouting_report") or {}
        if report:
            row["desk_analysis"]["scouting_report"] = {"customer": report.get("customer")}
        context = analysis.get("current_context") or {}
        if context.get("weather"):
            row["desk_analysis"]["current_context"] = {"weather": context["weather"]}
        row["decision"] = {k: v for k, v in (original.get("decision") or {}).items()
                           if k not in {"alternatives", "selected"}}
        decision = original.get("decision") or {}
        for lane in ("most_likely", "value_candidate"):
            candidate = decision.get(lane)
            if candidate:
                row["decision"][lane] = {k:v for k,v in candidate.items() if k not in {"eligibility_inputs", "features", "scoring_paths", "research_rank", "football_case"}}
                research = candidate.get("research") or {}
                row["decision"][lane]["research"] = {k:research[k] for k in ("opponent_games", "quarterback", "opponent") if k in research}
        row["decision"]["alternatives"] = [{**{k: c.get(k) for k in ("player_id", "name", "team", "position", "probability", "american")},
            "research_rank": {"advantages": (c.get("research_rank") or {}).get("advantages", [])}}
            for c in decision.get("alternatives", [])]
        result["rows"].append(row)
    return result
