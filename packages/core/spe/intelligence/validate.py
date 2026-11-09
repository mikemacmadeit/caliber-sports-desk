"""Reject model text that invents rows, contradicts itself, or rewrites a call."""

from __future__ import annotations

import re

from spe.intelligence.schema import PROP_MARKETS, ExplanationOutput, ResearchOutput

_DIRECTION = re.compile(
    r"(improved|increased|decreased|declined|dropped).{0,80}?\(([-\d.]+)\).{0,60}?\(([-\d.]+)\)",
    re.I,
)
_EDGE_CLAIM = re.compile(r"\b(edge|ev|\+ev|expected value)\b.{0,40}?\d", re.I)

OUT_DESIGNATIONS = {"out", "ir", "injured reserve", "pup", "suspended"}
HEALTHY_STANCES = {"healthy", "active", "available", "full"}
OUT_STANCES = {"out", "ir", "injured reserve", "pup", "suspended", "inactive"}


def validate_research(
    output: ResearchOutput,
    allowed_players: set[str],
    evidence: dict[str, set[str]],
    *,
    player_teams: dict[str, str | None] | None = None,
    game_teams: set[str] | None = None,
    packet_gaps: list[str] | None = None,
) -> list[str]:
    errors: list[str] = []
    seen: dict[str, str] = {}
    gaps = set(packet_gaps or [])
    for i, finding in enumerate(output.findings):
        errors.extend(
            _claim_errors(i, finding.claim_type, finding.evidence, finding.affected_player_ids, allowed_players, evidence)
        )
        errors.extend(_team_errors(i, finding.affected_player_ids, player_teams, game_teams, label="finding"))
        if finding.claim_type == "fact" and finding.topic in {"coaching", "tendency"} and "coaching_staff" in gaps:
            errors.append(f"finding {i} states coaching as fact while coaching_staff is a gap")
        for market_id in finding.markets_affected:
            if market_id not in PROP_MARKETS:
                errors.append(f"finding {i} names unknown market {market_id}")
        if finding.subject_key and finding.stance and finding.claim_type == "fact":
            stance = _normalize_stance(finding.stance)
            prior = seen.get(finding.subject_key)
            if prior and stance and _stances_conflict(prior, stance):
                errors.append(f"finding {i} contradicts {finding.subject_key}: {prior} vs {stance}")
            if stance:
                seen[finding.subject_key] = stance
    for i, signal in enumerate(output.signals):
        if signal.player_id not in allowed_players:
            errors.append(f"signal {i} names unknown player {signal.player_id}")
        errors.extend(
            _claim_errors(
                i,
                signal.claim_type,
                signal.evidence,
                [signal.player_id],
                allowed_players,
                evidence,
                label="signal",
            )
        )
        errors.extend(_team_errors(i, [signal.player_id], player_teams, game_teams, label="signal"))
    for i, opportunity in enumerate(output.opportunities):
        if opportunity.player_id not in allowed_players:
            errors.append(f"opportunity {i} names unknown player {opportunity.player_id}")
        if opportunity.market_id not in PROP_MARKETS:
            errors.append(f"opportunity {i} names unknown market {opportunity.market_id}")
        if not opportunity.explanation.strip():
            errors.append(f"opportunity {i} is missing an explanation")
        if not opportunity.recommended_analysis.strip():
            errors.append(f"opportunity {i} is missing recommended_analysis")
        if _EDGE_CLAIM.search(opportunity.explanation) or _EDGE_CLAIM.search(opportunity.recommended_analysis):
            errors.append(f"opportunity {i} invents a numeric edge without a priced forecast")
        errors.extend(
            _claim_errors(
                i,
                opportunity.claim_type,
                opportunity.evidence,
                [opportunity.player_id],
                allowed_players,
                evidence,
                label="opportunity",
            )
        )
        errors.extend(_team_errors(i, [opportunity.player_id], player_teams, game_teams, label="opportunity"))
    return errors


def validate_explanation(output: ExplanationOutput, expected: list[dict]) -> list[str]:
    errors: list[str] = []
    got = {row.forecast_id: row for row in output.forecasts}
    for row in expected:
        echo = got.get(row["forecast_id"])
        if echo is None:
            errors.append(f"missing forecast echo {row['forecast_id']}")
            continue
        if echo.recommendation_status != row["recommendation_status"]:
            errors.append(
                f"{row['forecast_id']} status {echo.recommendation_status} != {row['recommendation_status']}"
            )
        if list(echo.reason_codes) != list(row["reason_codes"]):
            errors.append(f"{row['forecast_id']} reason codes were rewritten")
    extra = set(got) - {row["forecast_id"] for row in expected}
    for forecast_id in extra:
        errors.append(f"unexpected forecast {forecast_id}")
    return errors


def misstates_direction(statement: str) -> bool:
    match = _DIRECTION.search(statement)
    if not match:
        return False
    word = match.group(1).lower()
    earlier, later = float(match.group(2)), float(match.group(3))
    if word in {"improved", "increased"} and later < earlier:
        return True
    return word in {"decreased", "declined", "dropped"} and later > earlier


def _normalize_stance(stance: str) -> str:
    text = stance.strip().lower()
    if text in OUT_STANCES or any(token == text for token in OUT_STANCES):
        return "out" if text in OUT_STANCES else text
    if text in HEALTHY_STANCES:
        return "healthy"
    for token in OUT_STANCES:
        if text == token or text.startswith(token + " ") or f" {token} " in f" {text} ":
            if token in HEALTHY_STANCES:
                return "healthy"
            return "out"
    for token in HEALTHY_STANCES:
        if text == token:
            return "healthy"
    # Free-text workload notes are not availability contradictions.
    return ""


def _stances_conflict(left: str, right: str) -> bool:
    return {left, right} == {"out", "healthy"}


def availability_override(designation: str) -> float | None:
    if designation.strip().lower() in OUT_DESIGNATIONS:
        return 0.0
    return None


def repair_research_evidence(output: ResearchOutput, evidence: dict[str, set[str]]) -> ResearchOutput:
    """Fix common model citation mistakes without inventing new facts."""
    by_ref: dict[str, str] = {}
    for source, refs in evidence.items():
        for ref in refs:
            # Prefer the first source if a ref collides across tables.
            by_ref.setdefault(ref, source)

    def fix_rows(rows):
        fixed = []
        for row in rows:
            source = row.source
            ref = row.ref
            if source in evidence and ref in evidence[source]:
                fixed.append(row)
                continue
            if ref in evidence and source in evidence.get(ref, set()):
                fixed.append(row.model_copy(update={"source": ref, "ref": source}))
                continue
            if ref in evidence and source not in evidence:
                refs = evidence[ref]
                chosen = source if source in refs else (next(iter(refs)) if len(refs) == 1 else None)
                if chosen:
                    fixed.append(row.model_copy(update={"source": ref, "ref": chosen}))
                    continue
            if source in by_ref and (not ref or ref == source):
                fixed.append(row.model_copy(update={"source": by_ref[source], "ref": source}))
                continue
            if ref in by_ref and source not in evidence:
                fixed.append(row.model_copy(update={"source": by_ref[ref], "ref": ref}))
                continue
            if source in by_ref:
                fixed.append(row.model_copy(update={"source": by_ref[source], "ref": source}))
                continue
            fixed.append(row)
        return fixed

    findings = [
        finding.model_copy(update={"evidence": fix_rows(finding.evidence)}) for finding in output.findings
    ]
    signals = [signal.model_copy(update={"evidence": fix_rows(signal.evidence)}) for signal in output.signals]
    opportunities = [
        opportunity.model_copy(update={"evidence": fix_rows(opportunity.evidence)})
        for opportunity in output.opportunities
    ]
    return output.model_copy(update={"findings": findings, "signals": signals, "opportunities": opportunities})


def stamp_evidence(evidence_rows, packet: dict) -> list[dict]:
    """Attach verified timestamps from the packet onto evidence refs."""
    lookup = _timestamp_lookup(packet)
    stamped = []
    for row in evidence_rows:
        payload = row.model_dump() if hasattr(row, "model_dump") else dict(row)
        key = (payload.get("source"), payload.get("ref"))
        if not payload.get("observed_at") and key in lookup:
            payload["observed_at"] = lookup[key]
        stamped.append(payload)
    return stamped


def _timestamp_lookup(packet: dict) -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    for row in packet.get("injuries", []):
        if row.get("reported_at"):
            out[("injury_events", row["ref"])] = row["reported_at"]
    weather = packet.get("weather")
    if weather and weather.get("issued_at"):
        out[("weather_snapshots", weather["ref"])] = weather["issued_at"]
    for row in packet.get("prices", []):
        if row.get("captured_at"):
            out[("odds_ticks", row["ref"])] = row["captured_at"]
    for row in packet.get("news", []):
        if row.get("seen_at"):
            out[("news_articles", row["ref"])] = row["seen_at"]
    for row in packet.get("depth_chart", []):
        if row.get("snapshot_at"):
            out[("depth_charts", row["ref"])] = row["snapshot_at"]
    return out


def _team_errors(index, player_ids, player_teams, game_teams, label="finding"):
    if not player_teams or not game_teams:
        return []
    errors = []
    for player_id in player_ids:
        team_id = player_teams.get(player_id)
        if team_id and team_id not in game_teams:
            errors.append(f"{label} {index} player {player_id} is not on either team in this game")
    return errors


def _claim_errors(index, claim_type, evidence_rows, player_ids, allowed_players, catalog, label="finding"):
    errors = []
    for player_id in player_ids:
        if player_id not in allowed_players:
            errors.append(f"{label} {index} names unknown player {player_id}")
    if claim_type == "fact" and not evidence_rows:
        errors.append(f"{label} {index} is a fact with no source")
    for ev in evidence_rows:
        source = ev.source if hasattr(ev, "source") else ev.get("source")
        ref = ev.ref if hasattr(ev, "ref") else ev.get("ref")
        refs = catalog.get(source)
        if refs is None or ref not in refs:
            errors.append(f"{label} {index} cites missing {source}:{ref}")
    return errors
