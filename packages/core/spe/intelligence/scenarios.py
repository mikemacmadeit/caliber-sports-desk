"""Convert AI findings into validated research-only simulation inputs.

Official model inputs stay untouched unless a designation is Python-validated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from sqlalchemy.orm import Session

from spe.db.models import Game, ModelVersion, Player, SimulationRun
from spe.intelligence.validate import availability_override
from spe.simulate.engine import PlayerRole, simulate_game

ScenarioKind = Literal[
    "availability",
    "workload_targets",
    "workload_carries",
    "wind_mps",
    "pace_sec_per_play",
    "defensive_starter_out",
]


@dataclass
class ScenarioChange:
    kind: ScenarioKind
    value: float
    claim_type: str
    player_id: str | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    source_signal: str | None = None


@dataclass
class ScenarioPlan:
    game_id: str
    changes: list[ScenarioChange]
    research_only: bool
    validated_changes: list[ScenarioChange]
    hypothetical_changes: list[ScenarioChange]
    rejection_reasons: list[str] = field(default_factory=list)


def plan_from_signals(packet: dict[str, Any], signals: list[Any]) -> ScenarioPlan:
    """Build a scenario plan from AI signals + verified packet rows."""
    injuries = {row["ref"]: row for row in packet.get("injuries", [])}
    weather = packet.get("weather") or {}
    game_id = packet["game"]["game_id"]
    allowed = {row["player_id"] for row in packet.get("players", [])}
    changes: list[ScenarioChange] = []
    validated: list[ScenarioChange] = []
    hypothetical: list[ScenarioChange] = []
    errors: list[str] = []

    for signal in signals:
        player_id = _get(signal, "player_id")
        metric = _get(signal, "metric")
        direction = _get(signal, "direction")
        magnitude = _get(signal, "magnitude")
        claim_type = _get(signal, "claim_type")
        evidence = _get(signal, "evidence") or []
        if player_id not in allowed:
            errors.append(f"scenario player unknown: {player_id}")
            continue
        change = _signal_change(player_id, metric, direction, magnitude, claim_type, evidence, injuries, weather)
        if change is None:
            continue
        reasons = validate_change(change, packet)
        if reasons:
            errors.extend(reasons)
            continue
        changes.append(change)
        if change.claim_type == "fact" and _is_verified(change, injuries, weather):
            validated.append(change)
        else:
            hypothetical.append(change)

    return ScenarioPlan(
        game_id=game_id,
        changes=changes,
        research_only=True,
        validated_changes=validated,
        hypothetical_changes=hypothetical,
        rejection_reasons=errors,
    )


def validate_change(change: ScenarioChange, packet: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    allowed = {row["player_id"] for row in packet.get("players", [])}
    if change.player_id and change.player_id not in allowed:
        errors.append(f"{change.kind} names unknown player {change.player_id}")
    if change.kind == "availability" and change.value not in (0.0, 1.0) and change.claim_type == "fact":
        errors.append("availability facts must be 0 or 1 from a verified designation")
    if change.kind == "wind_mps" and change.value < 0:
        errors.append("wind cannot be negative")
    if change.kind == "pace_sec_per_play" and not (20 <= change.value <= 45):
        errors.append("pace_sec_per_play outside verified range")
    if change.kind in {"workload_targets", "workload_carries"} and change.value < 0:
        errors.append(f"{change.kind} cannot be negative")
    return errors


def run_research_simulation(
    session: Session,
    game: Game,
    plan: ScenarioPlan,
    packet: dict[str, Any],
    *,
    as_of: datetime,
    include_hypothetical: bool = True,
) -> str | None:
    """Execute a research-only simulation. Never writes official forecasts."""
    selected = list(plan.validated_changes)
    if include_hypothetical:
        selected.extend(plan.hypothetical_changes)
    if not selected:
        return None
    version = session.query(ModelVersion).filter(ModelVersion.league_id == game.league_id).first()
    if version is None:
        return None

    availability: dict[str, float] = {}
    workload: dict[str, dict[str, float]] = {}
    env: dict[str, float] = {}
    for change in selected:
        if change.kind == "availability" and change.player_id:
            availability[change.player_id] = change.value
        elif change.kind in {"workload_targets", "workload_carries"} and change.player_id:
            workload.setdefault(change.player_id, {})[change.kind] = change.value
        elif change.kind in {"wind_mps", "pace_sec_per_play"}:
            env[change.kind] = change.value
        elif change.kind == "defensive_starter_out" and change.player_id:
            availability[change.player_id] = 0.0

    roles: list[PlayerRole] = []
    for player_id in set(availability) | set(workload):
        player = session.get(Player, player_id)
        if player is None:
            continue
        team_id = _player_team(packet, game, player_id)
        load = workload.get(player_id, {})
        rush_mu = 10.0
        if "workload_carries" in load:
            rush_mu = float(load["workload_carries"]) * 4.0
        roles.append(
            PlayerRole(
                player_id=player_id,
                team_id=team_id,
                position_group=player.position_group or "WR",
                availability=availability.get(player_id, 1.0),
                snap_share=0.55 if load else 0.5,
                td_weight=0.15,
                rec_lambda=float(load.get("workload_targets", 3.0)),
                rec_yards_mu=40.0,
                rush_yards_mu=rush_mu,
            )
        )
    if not roles and not env:
        return None
    if not roles:
        roles = [
            PlayerRole(
                player_id="env",
                team_id=game.home_team_id,
                position_group="WR",
                availability=1.0,
                snap_share=0.01,
                td_weight=0.01,
                rec_lambda=0.1,
                rec_yards_mu=1.0,
                rush_yards_mu=1.0,
            )
        ]

    overrides = {
        "availability": availability,
        "workload": workload,
        "environment": env,
        "source": "ai_scenario_v1",
        "validated_only": [_change_dict(row) for row in plan.validated_changes],
        "hypothetical": [_change_dict(row) for row in plan.hypothetical_changes],
        "official": False,
        "play_enabled": False,
    }
    result = simulate_game(
        game.league_id,
        game.home_team_id,
        game.away_team_id,
        roles,
        draw_count=2000,
        seed=42,
        overrides=overrides,
    )
    sim = SimulationRun(
        game_id=game.game_id,
        as_of=as_of,
        seed=result.seed,
        draw_count=result.draw_count,
        assumptions={"engine": "drive_dirichlet_v0", "scenario": "ai_research"},
        scenario="research",
        overrides=result.overrides,
        model_version_id=version.model_version_id,
        summary={"player_td_prob": result.player_td_prob, "official": False, "play_enabled": False},
        convergence=result.convergence,
        research_only=True,
    )
    session.add(sim)
    session.flush()
    return sim.run_id


def _player_team(packet: dict[str, Any], game: Game, player_id: str) -> str:
    for row in packet.get("players", []):
        if row.get("player_id") == player_id and row.get("team_id") in {game.home_team_id, game.away_team_id}:
            return row["team_id"]
    return game.home_team_id


def _get(obj: Any, key: str) -> Any:
    if hasattr(obj, key):
        return getattr(obj, key)
    return obj.get(key)


def _change_dict(change: ScenarioChange) -> dict[str, Any]:
    return {
        "kind": change.kind,
        "value": change.value,
        "claim_type": change.claim_type,
        "player_id": change.player_id,
        "evidence": change.evidence,
        "source_signal": change.source_signal,
    }


def _signal_change(player_id, metric, direction, magnitude, claim_type, evidence, injuries, weather):
    ev_rows = []
    for ev in evidence:
        if hasattr(ev, "model_dump"):
            ev_rows.append(ev.model_dump())
        else:
            ev_rows.append(dict(ev))

    for ev in ev_rows:
        if ev.get("source") != "injury_events":
            continue
        row = injuries.get(ev.get("ref"))
        if not row or row["player_id"] != player_id:
            continue
        avail = availability_override(row["designation"])
        if avail is not None:
            return ScenarioChange(
                kind="availability",
                player_id=player_id,
                value=avail,
                claim_type="fact",
                evidence=ev_rows,
                source_signal=metric,
            )

    if metric in {"targets", "receptions"} and direction in {"up", "down"} and magnitude == "material":
        return ScenarioChange(
            kind="workload_targets",
            player_id=player_id,
            value=6.0 if direction == "up" else 2.0,
            claim_type=claim_type or "inference",
            evidence=ev_rows,
            source_signal=metric,
        )
    if metric in {"carries", "rushing_yards"} and direction in {"up", "down"} and magnitude == "material":
        return ScenarioChange(
            kind="workload_carries",
            player_id=player_id,
            value=18.0 if direction == "up" else 6.0,
            claim_type=claim_type or "inference",
            evidence=ev_rows,
            source_signal=metric,
        )
    if metric == "yards" and weather.get("wind_mps") is not None and direction == "down":
        return ScenarioChange(
            kind="wind_mps",
            player_id=None,
            value=float(weather["wind_mps"]),
            claim_type="fact" if weather.get("ref") else (claim_type or "inference"),
            evidence=[{"source": "weather_snapshots", "ref": weather.get("ref")}] if weather.get("ref") else ev_rows,
            source_signal=metric,
        )
    return None


def _is_verified(change: ScenarioChange, injuries: dict, weather: dict) -> bool:
    if change.kind == "availability":
        for ev in change.evidence:
            row = injuries.get(ev.get("ref")) if ev.get("source") == "injury_events" else None
            if row and availability_override(row["designation"]) is not None:
                return True
        return False
    if change.kind == "wind_mps":
        return bool(weather.get("ref")) and weather.get("wind_mps") is not None
    return False
