"""Show the current job, not an old scoring rate.

The stored model can still remember how often a player used to score.
On the live board, a backup does not keep that rate. His shown chance is
the model chance times his recent touches divided by the starter's recent
touches. It cannot go up. The starter's chance stays as the model wrote it.

This does not rewrite a saved forecast or turn on Official Play.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pandas as pd

from spe.config import settings

SKILL = {"QB", "RB", "WR", "TE", "FB"}
TEAM_ALIAS = {"WSH": "WAS", "LAR": "LA", "JAC": "JAX"}


_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def _norm(name: str | None) -> str:
    text = "".join(ch.lower() if ch.isalnum() or ch.isspace() else " " for ch in str(name or ""))
    return " ".join(text.split())


def _person(name: str | None) -> str:
    """Same player across 'Aaron Jones' and 'Aaron Jones Sr.'"""
    parts = _norm(name).split()
    while parts and parts[-1] in _SUFFIXES:
        parts.pop()
    collapsed: list[str] = []
    index = 0
    while index < len(parts):
        if len(parts[index]) == 1:
            letters = parts[index]
            index += 1
            while index < len(parts) and len(parts[index]) == 1:
                letters += parts[index]
                index += 1
            collapsed.append(letters)
        else:
            collapsed.append(parts[index])
            index += 1
    return " ".join(collapsed)


def _team(abbr: str | None) -> str:
    code = str(abbr or "").upper()
    return TEAM_ALIAS.get(code, code)


def backup_touch_ratio(player_touches: float, starter_touches: float) -> float:
    """Share of the starter's recent work. Never increases a chance."""
    if starter_touches <= 0:
        return 1.0
    return float(min(1.0, max(0.0, player_touches) / starter_touches))


def defense_factor(position: str | None, allowed: float | None, league: float | None) -> float:
    """How many scores this opponent has allowed lately, versus a normal defense.

    The move is capped. A soft defense cannot double a chance, and a good one
    cannot wipe it out.
    """
    pos = str(position or "").upper()
    if pos not in SKILL or allowed is None or league is None or league <= 0:
        return 1.0
    return float(min(1.12, max(0.88, allowed / league)))


def weather_factor(position: str | None, wind_mps: float | None, precip_probability: float | None, indoors: bool) -> float:
    """Wind and a likely rain cut the passing chance. A dome does not."""
    if indoors:
        return 1.0
    pos = str(position or "").upper()
    factor = 1.0
    if wind_mps is not None and wind_mps >= 8 and pos in {"WR", "TE", "QB"}:
        factor *= 0.92
    if precip_probability is not None and precip_probability >= 0.6:
        factor *= 0.96
    return factor


def apply_current_role(
    payload: dict,
    *,
    player_name: str | None,
    team_abbr: str | None,
    position: str | None,
    depth: dict[tuple[str, str, str], int],
    touches: dict[tuple[str, str, str], float],
    defense: dict[str, dict[str, float]] | None = None,
    weather: dict[str, float | bool | None] | None = None,
    opponent: str | None = None,
) -> dict:
    pos = str(position or "").upper()
    if pos not in SKILL:
        return payload
    key = (_team(team_abbr), pos if pos != "FB" else "RB", _person(player_name))
    rank = depth.get(key)
    out = dict(payload)
    if rank is not None and rank > 1:
        starter_touches = 0.0
        for (team, group, _name), count in touches.items():
            if team == key[0] and group == key[1] and depth.get((team, group, _name), 99) == 1:
                starter_touches = max(starter_touches, count)
        ratio = backup_touch_ratio(touches.get(key, 0.0), starter_touches)
        if ratio < 0.999:
            value = out.get("p_estimate")
            if isinstance(value, (int, float)):
                out["p_estimate"] = float(value) * ratio
            interval = out.get("confidence_interval")
            if isinstance(interval, (list, tuple)) and len(interval) == 2:
                out["confidence_interval"] = (float(interval[0]) * ratio, float(interval[1]) * ratio)
            reasons = list(out.get("reason_codes") or [])
            if "CURRENT_ROLE" not in reasons:
                reasons.append("CURRENT_ROLE")
            out["reason_codes"] = reasons
            out["current_role"] = {"depth_rank": rank, "touch_ratio": ratio}
    out = _apply_game_context(out, position=pos, opponent=opponent, defense=defense, weather=weather)
    # Role calibration (rushing QB / high-carry RB underconfidence).
    try:
        from spe.shadow.atd_role_calibration import apply_role_calibration

        value = out.get("p_estimate")
        if isinstance(value, (int, float)):
            touches_n = float(touches.get(key, 0.0) or 0.0)
            fixed = apply_role_calibration(
                float(value),
                position_group=pos,
                prior_carries=touches_n,
            )
            if abs(float(fixed) - float(value)) > 1e-9:
                out = dict(out)
                out["p_estimate_pre_role_calib"] = float(value)
                out["p_estimate"] = float(fixed)
                interval = out.get("confidence_interval")
                if isinstance(interval, (list, tuple)) and len(interval) == 2 and float(value) > 1e-9:
                    scale = float(fixed) / float(value)
                    out["confidence_interval"] = (float(interval[0]) * scale, float(interval[1]) * scale)
                reasons = list(out.get("reason_codes") or [])
                if "ROLE_CALIBRATION_V1" not in reasons:
                    reasons.append("ROLE_CALIBRATION_V1")
                out["reason_codes"] = reasons
    except Exception:  # noqa: BLE001
        pass
    return out


def _scale(payload: dict, factor: float, reason: str) -> dict:
    if abs(factor - 1.0) < 0.005:
        return payload
    out = dict(payload)
    value = out.get("p_estimate")
    if isinstance(value, (int, float)):
        out["p_estimate"] = float(value) * factor
    interval = out.get("confidence_interval")
    if isinstance(interval, (list, tuple)) and len(interval) == 2:
        out["confidence_interval"] = (float(interval[0]) * factor, float(interval[1]) * factor)
    reasons = list(out.get("reason_codes") or [])
    if reason not in reasons:
        reasons.append(reason)
    out["reason_codes"] = reasons
    return out


def _apply_game_context(
    payload: dict,
    *,
    position: str,
    opponent: str | None,
    defense: dict[str, dict[str, float]] | None,
    weather: dict[str, float | bool | None] | None,
) -> dict:
    out = payload
    if defense and opponent:
        row = defense.get(_team(opponent)) or {}
        kind = "rush" if str(position).upper() in {"RB", "QB", "FB"} else "pass"
        factor = defense_factor(position, row.get(kind), row.get(f"league_{kind}"))
        out = _scale(out, factor, "MATCHUP")
        out["matchup"] = {"kind": kind, "factor": factor}
    if weather:
        indoors = bool(weather.get("indoors"))
        wind = weather.get("wind_mps")
        precip = weather.get("precip_probability")
        factor = weather_factor(
            position,
            float(wind) if isinstance(wind, (int, float)) else None,
            float(precip) if isinstance(precip, (int, float)) else None,
            indoors,
        )
        out = _scale(out, factor, "WEATHER")
    return out


def _cache_dir() -> Path:
    return Path(settings.parquet_dir).parent / "cache" / "nflverse"


@lru_cache(maxsize=1)
def load_current_role_tables() -> tuple[
    dict[tuple[str, str, str], int],
    dict[tuple[str, str, str], float],
    dict[str, dict[str, float]],
]:
    root = _cache_dir()
    depth_path = root / "depth_charts_2026.parquet"
    stats_path = root / "stats_player_week_2026.parquet"
    depth: dict[tuple[str, str, str], int] = {}
    touches: dict[tuple[str, str, str], float] = {}
    defense: dict[str, dict[str, float]] = {}
    if depth_path.exists():
        frame = pd.read_parquet(depth_path, columns=["dt", "team", "player_name", "pos_abb", "pos_rank"])
        frame["dt"] = pd.to_datetime(frame["dt"], utc=True, errors="coerce")
        latest = frame["dt"].max()
        current = frame[frame["dt"].eq(latest)]
        for row in current.itertuples(index=False):
            pos = str(row.pos_abb or "").upper()
            if pos == "FB":
                pos = "RB"
            if pos not in SKILL:
                continue
            key = (_team(row.team), pos, _person(row.player_name))
            rank = int(row.pos_rank) if pd.notna(row.pos_rank) else 99
            depth[key] = min(depth.get(key, 99), rank)
    if stats_path.exists():
        stats = pd.read_parquet(
            stats_path,
            columns=["player_display_name", "position", "team", "opponent_team", "week", "carries", "targets", "rushing_tds", "receiving_tds"],
        )
        latest_week = int(pd.to_numeric(stats["week"], errors="coerce").max())
        recent = stats[pd.to_numeric(stats["week"], errors="coerce").ge(latest_week - 2)]
        grouped = recent.groupby(["team", "position", "player_display_name"], dropna=False)[["carries", "targets"]].sum()
        for (team, pos, name), row in grouped.iterrows():
            group = "RB" if str(pos).upper() == "FB" else str(pos).upper()
            if group not in SKILL:
                continue
            touches[(_team(team), group, _person(name))] = float(row["carries"] or 0) + float(row["targets"] or 0)
        scored = recent.groupby(["team", "opponent_team"], dropna=False)[["rushing_tds", "receiving_tds"]].sum()
        allowed: dict[str, list[float]] = {}
        for (_offense, defense_team), row in scored.iterrows():
            bucket = allowed.setdefault(_team(defense_team), [0.0, 0.0])
            bucket[0] += float(row["rushing_tds"] or 0)
            bucket[1] += float(row["receiving_tds"] or 0)
        if allowed:
            league_rush = sum(pair[0] for pair in allowed.values()) / len(allowed)
            league_pass = sum(pair[1] for pair in allowed.values()) / len(allowed)
            for team, pair in allowed.items():
                defense[team] = {
                    "rush": pair[0],
                    "pass": pair[1],
                    "league_rush": league_rush,
                    "league_pass": league_pass,
                }
    return depth, touches, defense
