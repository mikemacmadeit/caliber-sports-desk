"""Who is likely to score in a college game tonight.

The stored college forecast is a team prior, and the research file that was
supposed to replace it is not on disk. Recent carries, targets, and scores
are. A player with no recent work is not given a chance.

This does not rewrite a saved forecast or turn on Official Play.
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from spe.models.player_td.current_role import SKILL, defense_factor
from spe.models.player_td.tonight import (
    DECAY,
    LOOKBACK,
    SHRINK_GAMES,
    FormLine,
    TonightChance,
    _summary,
    _weather_split,
    _write,
    score_players,
)

_CACHE: dict[str, Any] = {"built": 0.0, "form": None}
_CACHE_TTL_SEC = 15 * 60


def chances_from_logs(logs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Build one offense card per team from recent player-game rows.

    Each log has player_id, team_id, pos, game_rank (1 = most recent for that
    team), carries, targets, rush_td, rec_td, and opponent_id.
    """
    by_team: dict[str, list[dict[str, Any]]] = {}
    for row in logs:
        by_team.setdefault(str(row["team_id"]), []).append(row)

    offenses: dict[str, dict[str, Any]] = {}
    allowed: dict[str, list[float]] = {}
    for team_id, rows in by_team.items():
        card = _offense(team_id, rows)
        if card is not None:
            offenses[team_id] = card
        for opponent, rush, recv in _allowed(rows):
            bucket = allowed.setdefault(opponent, [0.0, 0.0])
            bucket[0] += rush
            bucket[1] += recv
    shrink_rates(offenses)
    league_rush, league_pass = _league_allowed(allowed)
    defense = {
        team: {
            "rush": pair[0],
            "pass": pair[1],
            "league_rush": league_rush,
            "league_pass": league_pass,
        }
        for team, pair in allowed.items()
    }
    return {"offenses": offenses, "defense": defense}


def apply_fbs_form(
    session: Session,
    payload: dict[str, Any],
    *,
    player_id: str | None,
    team_id: str | None,
    opponent_id: str | None,
    position: str | None,
    weather: dict | None = None,
) -> dict[str, Any] | None:
    """Replace the stored chance with recent college form. None hides the row."""
    if not player_id or not team_id:
        return None
    pos = str(position or "").upper()
    if pos == "FB":
        pos = "RB"
    status = str(payload.get("availability_status") or "").lower()
    if status in {"out", "doubtful"}:
        if status == "doubtful":
            label = "Doubtful tonight, so he is off the card."
        else:
            label = "Ruled out tonight."
        return _write(payload, TonightChance(0.0, None, 0.0, 0.0, 0.0, 0.0, label))
    form = load_form(session)
    offense = (form.get("offenses") or {}).get(str(team_id))
    player = (offense or {}).get("by_player", {}).get(str(player_id)) if offense else None
    if offense is None or player is None or pos not in SKILL:
        return None
    defense = (form.get("defense") or {}).get(str(opponent_id or "")) or {}
    rush_factor = defense_factor("RB", defense.get("rush"), defense.get("league_rush"))
    pass_factor = defense_factor("WR", defense.get("pass"), defense.get("league_pass"))
    rush_weather, pass_weather = _weather_split(weather)
    chances = score_players(
        offense["players"],
        team_rush_rate=offense["rush_rate"],
        team_rec_rate=offense["rec_rate"],
        rush_factor=rush_factor,
        pass_factor=pass_factor,
        rush_weather=rush_weather,
        pass_weather=pass_weather,
    )
    p = float(chances.get(str(player_id), 0.0))
    if p <= 0:
        return None
    if status in {"questionable", "uncertain"}:
        p *= 0.7
    line = player["line"]
    rank = int(player["rank"])
    kind = "rush" if pos in {"RB", "QB"} else "pass"
    factor = rush_factor if kind == "rush" else pass_factor
    scored = TonightChance(
        p=p,
        depth_rank=rank,
        carries_per_game=_per(line.carries, line.games),
        targets_per_game=_per(line.targets, line.games),
        recent_tds=line.rush_tds + line.rec_tds,
        games=line.games,
        summary=_summary(pos, rank, line, p, kind, factor, weather),
    )
    if status in {"questionable", "uncertain"}:
        scored = TonightChance(
            p=p,
            depth_rank=scored.depth_rank,
            carries_per_game=scored.carries_per_game,
            targets_per_game=scored.targets_per_game,
            recent_tds=scored.recent_tds,
            games=scored.games,
            summary=scored.summary + " Listed questionable, so the chance is cut.",
        )
    out = _write(payload, scored)
    out["model_version"] = "fbs-tonight-usage"
    out["data_tier"] = "research"
    out["research_only"] = True
    out["validated_play"] = False
    out["play_enabled"] = False
    out["recommendation_status"] = "no_bet"
    return out


def load_form(session: Session) -> dict[str, Any]:
    now = time.time()
    cached = _CACHE.get("form")
    if cached is not None and now - float(_CACHE.get("built") or 0.0) < _CACHE_TTL_SEC:
        return cached
    logs = _logs(session)
    form = chances_from_logs(logs)
    _CACHE["built"] = now
    _CACHE["form"] = form
    return form


def _logs(session: Session) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            """
            SELECT s.player_id, s.team_id, upper(coalesce(p.position_group, '')) AS pos,
                   g.game_id, g.kickoff_utc, g.home_team_id, g.away_team_id,
                   coalesce(s.carries, 0) AS carries,
                   coalesce(s.targets, 0) AS targets,
                   coalesce(s.rush_td, 0) AS rush_td,
                   coalesce(s.rec_td, 0) AS rec_td
            FROM player_game_stats s
            JOIN games g ON g.game_id = s.game_id
            JOIN players p ON p.player_id = s.player_id
            WHERE g.league_id = 'fbs'
              AND lower(g.status) = 'final'
              AND g.kickoff_utc >= '2026-08-01'
            """
        )
    ).mappings().all()
    by_team_game: dict[str, dict[str, str]] = {}
    for row in rows:
        team_id = str(row["team_id"] or "")
        if not team_id:
            continue
        by_team_game.setdefault(team_id, {})[str(row["game_id"])] = str(row["kickoff_utc"] or "")
    keep: dict[tuple[str, str], int] = {}
    for team_id, games in by_team_game.items():
        ordered = sorted(games.items(), key=lambda item: item[1], reverse=True)[:LOOKBACK]
        for rank, (game_id, _kick) in enumerate(ordered, start=1):
            keep[(team_id, game_id)] = rank
    logs: list[dict[str, Any]] = []
    for row in rows:
        team_id = str(row["team_id"] or "")
        rank = keep.get((team_id, str(row["game_id"])))
        if rank is None:
            continue
        home = str(row["home_team_id"] or "")
        away = str(row["away_team_id"] or "")
        opponent = away if team_id == home else home
        logs.append(
            {
                "player_id": str(row["player_id"]),
                "team_id": team_id,
                "pos": str(row["pos"] or ""),
                "game_rank": rank,
                "carries": float(row["carries"] or 0),
                "targets": float(row["targets"] or 0),
                "rush_td": float(row["rush_td"] or 0),
                "rec_td": float(row["rec_td"] or 0),
                "opponent_id": opponent,
            }
        )
    return logs


def _offense(team_id: str, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    decayed: dict[str, list[float]] = {}
    positions: dict[str, str] = {}
    game_weight: dict[int, float] = {}
    rush_tds = 0.0
    rec_tds = 0.0
    for row in rows:
        pos = str(row.get("pos") or "").upper()
        if pos == "FB":
            pos = "RB"
        if pos not in SKILL:
            continue
        rank = int(row["game_rank"])
        decay = DECAY ** (rank - 1)
        game_weight[rank] = decay
        rush_tds += decay * float(row.get("rush_td") or 0)
        rec_tds += decay * float(row.get("rec_td") or 0)
        player_id = str(row["player_id"])
        positions[player_id] = pos
        bucket = decayed.setdefault(player_id, [0.0, 0.0, 0.0, 0.0, 0.0])
        bucket[0] += decay * float(row.get("carries") or 0)
        bucket[1] += decay * float(row.get("targets") or 0)
        bucket[2] += decay * float(row.get("rush_td") or 0)
        bucket[3] += decay * float(row.get("rec_td") or 0)
        bucket[4] += decay
    if not decayed:
        return None
    grouped: dict[str, list[tuple[str, float]]] = {}
    for player_id, values in decayed.items():
        work = values[0] + values[1]
        if work <= 0 and values[2] + values[3] <= 0:
            continue
        grouped.setdefault(positions[player_id], []).append((player_id, work))
    ranks: dict[str, int] = {}
    for group in grouped.values():
        ordered = sorted(group, key=lambda item: item[1], reverse=True)
        for index, (player_id, _work) in enumerate(ordered, start=1):
            ranks[player_id] = index
    players: list[dict[str, Any]] = []
    by_player: dict[str, dict[str, Any]] = {}
    for player_id, values in decayed.items():
        if player_id not in ranks:
            continue
        line = FormLine(values[0], values[1], values[2], values[3], values[4])
        entry = {
            "person": player_id,
            "pos": positions[player_id],
            "rank": ranks[player_id],
            "carries": line.carries,
            "targets": line.targets,
            "rush_tds": line.rush_tds,
            "rec_tds": line.rec_tds,
            "games": line.games,
            "line": line,
        }
        players.append(entry)
        by_player[player_id] = entry
    games = sum(game_weight.values())
    if games <= 0 or not players:
        return None
    return {
        "team_id": team_id,
        "players": players,
        "by_player": by_player,
        "rush_rate": rush_tds / games,
        "rec_rate": rec_tds / games,
        "games": games,
    }


def _allowed(rows: list[dict[str, Any]]) -> list[tuple[str, float, float]]:
    """Rush and receiving scores this offense put on each opponent, last three games."""
    by_game: dict[tuple[str, int], list[float]] = {}
    for row in rows:
        rank = int(row["game_rank"])
        if rank > 3:
            continue
        opponent = str(row.get("opponent_id") or "")
        if not opponent:
            continue
        bucket = by_game.setdefault((opponent, rank), [0.0, 0.0])
        bucket[0] += float(row.get("rush_td") or 0)
        bucket[1] += float(row.get("rec_td") or 0)
    totals: dict[str, list[float]] = {}
    for (opponent, _rank), bucket in by_game.items():
        total = totals.setdefault(opponent, [0.0, 0.0])
        total[0] += bucket[0]
        total[1] += bucket[1]
    return [(opponent, pair[0], pair[1]) for opponent, pair in totals.items()]


def _league_allowed(allowed: dict[str, list[float]]) -> tuple[float, float]:
    if not allowed:
        return 0.0, 0.0
    count = float(len(allowed))
    return (
        sum(pair[0] for pair in allowed.values()) / count,
        sum(pair[1] for pair in allowed.values()) / count,
    )


def shrink_rates(offenses: dict[str, dict[str, Any]]) -> None:
    """Pull a short sample toward the college average. Mutates rush_rate and rec_rate."""
    if not offenses:
        return
    weights = [float(card["games"]) for card in offenses.values()]
    total = sum(weights) or 1.0
    league_rush = sum(card["rush_rate"] * card["games"] for card in offenses.values()) / total
    league_rec = sum(card["rec_rate"] * card["games"] for card in offenses.values()) / total
    for card in offenses.values():
        games = float(card["games"])
        card["rush_rate"] = (SHRINK_GAMES * league_rush + games * card["rush_rate"]) / (
            SHRINK_GAMES + games
        )
        card["rec_rate"] = (SHRINK_GAMES * league_rec + games * card["rec_rate"]) / (
            SHRINK_GAMES + games
        )


def _per(total: float, games: float) -> float:
    if games <= 0:
        return 0.0
    return total / games
