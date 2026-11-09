"""AI matchup research for ATS — explanations only; never overrides calibrated probs.

All factual claims must cite verified packet evidence. Numerical cover probabilities
remain owned by the ATS statistical model.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from spe.db.models import Game, InjuryEvent, Team, WeatherSnapshot
from spe.intelligence.client import IntelligenceError, LanguageModel, OpenAIResponses


ATS_RESEARCH_INSTRUCTIONS = """
You are Caliber's Against-the-Spread research desk.

Use only the JSON packet. Facts require evidence shaped as {"source": "<bucket>", "ref": "<id>"}.
Do not invent statistics, injuries, weather, coaching changes, or roster facts.
Do not produce or override cover probabilities, EV, or Play/No-bet calls.
The numerical model owns predicted_margin and p_cover; you may only explain them.

Focus on verified matchup factors:
- Key injuries (from injury_events only)
- Offensive vs defensive efficiency edges already present in packet.factors
- Weather when weather_available is true
- Rest / home-field flags present in the packet
Label each claim fact or inference. Keep NFL and FBS separate.
""".strip()


class AtsExplanation(BaseModel):
    headline: str
    bullets: list[str] = Field(default_factory=list)
    key_injuries: list[str] = Field(default_factory=list)
    weather_note: str | None = None
    uncertainty: list[str] = Field(default_factory=list)
    claim_types: list[str] = Field(default_factory=list)


def build_ats_research_packet(session: Session, prediction: dict[str, Any]) -> dict[str, Any]:
    game = session.get(Game, prediction["game_id"])
    home = session.get(Team, prediction["home_team_id"])
    away = session.get(Team, prediction["away_team_id"])
    injuries = (
        session.query(InjuryEvent)
        .filter(InjuryEvent.game_id == prediction["game_id"])
        .all()
    )
    weather = (
        session.query(WeatherSnapshot)
        .filter(WeatherSnapshot.game_id == prediction["game_id"])
        .order_by(WeatherSnapshot.issued_at.desc())
        .first()
    )
    injury_rows = [
        {
            "id": i.injury_event_id,
            "player_id": i.player_id,
            "designation": i.designation,
            "coverage_complete": i.coverage_complete,
            "source": i.source,
            "reported_at": i.reported_at.isoformat() if i.reported_at else None,
        }
        for i in injuries
    ]
    weather_row = None
    if weather is not None:
        weather_row = {
            "id": weather.id,
            "temperature_c": weather.temperature_c,
            "wind_mps": weather.wind_mps,
            "precip_mm": weather.precip_mm,
            "summary": weather.summary,
            "roof_status": weather.roof_status,
            "issued_at": weather.issued_at.isoformat() if weather.issued_at else None,
        }
    return {
        "kind": "ats_matchup_research",
        "as_of": datetime.now(UTC).isoformat(),
        "league_id": prediction["league_id"],
        "game": {
            "game_id": prediction["game_id"],
            "kickoff_utc": prediction.get("kickoff_utc"),
            "home": {"team_id": home.team_id if home else None, "name": home.name if home else None},
            "away": {"team_id": away.team_id if away else None, "name": away.name if away else None},
            "neutral_site": bool(game.neutral_site) if game else False,
        },
        "model": {
            "predicted_margin": prediction.get("predicted_margin"),
            "predicted_home_score": prediction.get("predicted_home_score"),
            "predicted_away_score": prediction.get("predicted_away_score"),
            "p_home_cover": prediction.get("p_home_cover"),
            "p_away_cover": prediction.get("p_away_cover"),
            "spread_line": prediction.get("spread_line"),
            "model_vs_market": prediction.get("model_vs_market"),
            "model_version_id": prediction.get("model_version_id"),
            "research_only": True,
            "play_enabled": False,
        },
        "factors": prediction.get("factors") or {},
        "warnings": prediction.get("warnings") or [],
        "evidence": {
            "injury_events": [{"id": r["id"]} for r in injury_rows],
            "weather_snapshots": [{"id": weather_row["id"]}] if weather_row else [],
        },
        "injury_events": injury_rows,
        "weather": weather_row,
        "gaps": _gaps(injury_rows, weather_row, prediction),
    }


def _gaps(injuries: list[dict[str, Any]], weather: dict[str, Any] | None, prediction: dict[str, Any]) -> list[str]:
    gaps = []
    if not injuries:
        gaps.append("injury_report_missing_or_empty")
    if weather is None:
        gaps.append("weather_missing")
    if prediction.get("spread_line") is None:
        gaps.append("sportsbook_spread_missing")
    gaps.append("coaching_changes_not_verified_in_packet")
    gaps.append("returning_starters_not_verified_in_packet")
    return gaps


def explain_ats_matchup(
    session: Session,
    prediction: dict[str, Any],
    client: LanguageModel | None = None,
    *,
    use_ai: bool = False,
) -> dict[str, Any]:
    packet = build_ats_research_packet(session, prediction)
    explanation: dict[str, Any]
    if use_ai:
        try:
            client = client or OpenAIResponses()
            parsed, _usage = client.complete(
                instructions=ATS_RESEARCH_INSTRUCTIONS,
                user=json.dumps(packet, default=str),
                schema=AtsExplanation,
            )
            explanation = parsed.model_dump()
            status = "ok"
        except (IntelligenceError, Exception) as exc:  # noqa: BLE001
            explanation = _fallback_explanation(packet)
            explanation["ai_error"] = str(exc)[:240]
            status = "fallback"
    else:
        explanation = _fallback_explanation(packet)
        status = "deterministic"
    return {
        "status": status,
        "packet_gaps": packet["gaps"],
        "explanation": explanation,
        "overrides_probability": False,
        "model_probability_unchanged": {
            "p_home_cover": prediction.get("p_home_cover"),
            "p_away_cover": prediction.get("p_away_cover"),
            "predicted_margin": prediction.get("predicted_margin"),
        },
        "research_only": True,
        "play_enabled": False,
    }


def _fallback_explanation(packet: dict[str, Any]) -> dict[str, Any]:
    factors = packet.get("factors") or {}
    bits: list[str] = []
    epa = float(factors.get("epa_edge") or 0)
    if abs(epa) > 0.05:
        bits.append(
            f"Verified EPA/PPA edge in packet favors {'home' if epa > 0 else 'away'} "
            f"(epa_edge={epa:.3f})."
        )
    if float(factors.get("home_field") or 0) > 0:
        bits.append("Home-field flag is present for a non-neutral site.")
    if float(factors.get("rest_edge") or 0) >= 2:
        bits.append("Home side has a rest-days edge in the feature packet.")
    elif float(factors.get("rest_edge") or 0) <= -2:
        bits.append("Away side has a rest-days edge in the feature packet.")
    wind = float(factors.get("weather_wind_mps") or 0)
    weather_note = None
    if packet.get("weather") and wind >= 7:
        weather_note = f"Elevated wind in weather snapshot ({wind:.1f} m/s)."
        bits.append(weather_note)
    injuries = packet.get("injury_events") or []
    key_injuries = [
        f"{row['player_id']}:{row['designation']}"
        for row in injuries
        if str(row.get("designation", "")).lower() in {"out", "doubtful", "suspended"}
    ][:8]
    if key_injuries:
        bits.append(f"{len(key_injuries)} verified out/doubtful/suspended designations on the injury list.")
    if not bits:
        bits.append("Insufficient verified narrative factors beyond the numerical model.")
    return {
        "headline": "Model-driven ATS research note",
        "bullets": bits,
        "key_injuries": key_injuries,
        "weather_note": weather_note,
        "uncertainty": list(packet.get("gaps") or []),
        "claim_types": ["fact", "inference"],
        "source": "deterministic_fallback",
    }
