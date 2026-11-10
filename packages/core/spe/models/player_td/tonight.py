"""Who is actually likely to score tonight.

The chance comes from the current depth chart, recent carries and targets,
recent scores, how many touchdowns the offense has been producing, the
defense they face, and the weather. A career rate is not the chance.
A player who is not on the depth chart is not a scorer.

This does not rewrite a saved forecast or turn on Official Play.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from math import exp

import pandas as pd

from spe.models.player_td.current_role import (
    SKILL,
    _cache_dir,
    _person,
    _team,
    defense_factor,
    load_current_role_tables,
)

DECAY = 0.5
LOOKBACK = 4
RANK_WEIGHT = {1: 1.25, 2: 0.9, 3: 0.65, 4: 0.4}
SHRINK_GAMES = 2.0


@dataclass(frozen=True)
class FormLine:
    carries: float
    targets: float
    rush_tds: float
    rec_tds: float
    games: float


@dataclass(frozen=True)
class TonightChance:
    p: float
    depth_rank: int | None
    carries_per_game: float
    targets_per_game: float
    recent_tds: float
    games: float
    summary: str


def tables_ready() -> bool:
    root = _cache_dir()
    depth = root / "depth_charts_2026.parquet"
    stats = root / "stats_player_week_2026.parquet"
    return depth.exists() and stats.exists()


def score_players(
    players: list[dict],
    *,
    team_rush_rate: float,
    team_rec_rate: float,
    rush_factor: float = 1.0,
    pass_factor: float = 1.0,
    rush_weather: float = 1.0,
    pass_weather: float = 1.0,
) -> dict[str, float]:
    """Map person key → anytime chance for one offense."""
    weights: list[tuple[float, float]] = []
    for player in players:
        weights.append(_weights(player))
    rush_total = sum(pair[0] for pair in weights)
    rec_total = sum(pair[1] for pair in weights)
    out: dict[str, float] = {}
    for player, (rush_w, rec_w) in zip(players, weights, strict=True):
        rush_share = rush_w / rush_total if rush_total > 0 else 0.0
        rec_share = rec_w / rec_total if rec_total > 0 else 0.0
        lam = team_rush_rate * rush_share * rush_factor * rush_weather
        lam += team_rec_rate * rec_share * pass_factor * pass_weather
        games = float(player.get("games") or 0.0)
        own = float(player.get("rush_tds") or 0.0) + float(player.get("rec_tds") or 0.0)
        if games > 0:
            own_rate = own / games
            lam = (SHRINK_GAMES * lam + games * own_rate) / (SHRINK_GAMES + games)
        out[str(player["person"])] = _poisson(lam)
    return out


def _poisson(lam: float) -> float:
    if lam <= 0:
        return 0.0
    # 1 - exp(-λ), capped so one hot month cannot read as a certainty.
    return min(0.85, max(0.0, 1.0 - exp(-lam)))


def _weights(player: dict) -> tuple[float, float]:
    pos = str(player.get("pos") or "").upper()
    rank = int(player.get("rank") or 9)
    role = RANK_WEIGHT.get(rank, 0.25)
    carries = float(player.get("carries") or 0.0)
    targets = float(player.get("targets") or 0.0)
    rush_tds = float(player.get("rush_tds") or 0.0)
    rec_tds = float(player.get("rec_tds") or 0.0)
    if pos == "QB":
        return carries * 0.45 * role + 5.0 * rush_tds, 0.0
    if pos in {"RB", "FB"}:
        return carries * role + 2.0 * rush_tds, targets * 0.55 * role + 2.0 * rec_tds
    if pos in {"WR", "TE"}:
        return carries * 0.2 * role, targets * role + 2.5 * rec_tds
    return 0.0, 0.0


def apply_tonight_chance(
    payload: dict,
    *,
    player_name: str | None,
    team_abbr: str | None,
    position: str | None,
    opponent: str | None = None,
    weather: dict | None = None,
    availability: str | None = None,
    through_week: int | None = None,
) -> dict:
    """Replace a career rate with tonight's chance. Missing chart → no chance."""
    pos = str(position or "").upper()
    out = dict(payload)
    if pos not in SKILL or not tables_ready():
        return out
    status = str(availability or out.get("availability_status") or "").lower()
    if status in {"out", "doubtful"}:
        if status == "doubtful":
            label = "Doubtful tonight, so he is off the card."
        else:
            label = "Ruled out tonight."
        return _write(out, TonightChance(0.0, None, 0.0, 0.0, 0.0, 0.0, label))
    scored = _chance_for(
        player_name=player_name,
        team_abbr=team_abbr,
        position=pos,
        opponent=opponent,
        weather=weather,
        through_week=through_week,
    )
    if status in {"questionable", "uncertain"}:
        p = scored.p * 0.7
        summary = scored.summary.replace(
            f"{round(scored.p * 100)}% to score tonight.",
            f"{round(p * 100)}% to score tonight.",
        )
        scored = TonightChance(
            p=p,
            depth_rank=scored.depth_rank,
            carries_per_game=scored.carries_per_game,
            targets_per_game=scored.targets_per_game,
            recent_tds=scored.recent_tds,
            games=scored.games,
            summary=summary + " Listed questionable, so the chance is cut.",
        )
    return _write(out, scored)


def _write(payload: dict, scored: TonightChance) -> dict:
    out = dict(payload)
    career = out.get("p_estimate")
    if isinstance(career, (int, float)):
        out["p_career"] = float(career)
    out["p_estimate"] = float(scored.p)
    out["confidence_interval"] = (max(0.0, scored.p - 0.08), min(0.95, scored.p + 0.08))
    reasons = [code for code in list(out.get("reason_codes") or []) if code != "TONIGHT_FORM"]
    reasons.append("TONIGHT_FORM")
    out["reason_codes"] = reasons
    out["tonight"] = {
        "depth_rank": scored.depth_rank,
        "carries_per_game": round(scored.carries_per_game, 2),
        "targets_per_game": round(scored.targets_per_game, 2),
        "recent_tds": round(scored.recent_tds, 2),
        "games": round(scored.games, 2),
        "summary": scored.summary,
    }
    return out


def _chance_for(
    *,
    player_name: str | None,
    team_abbr: str | None,
    position: str,
    opponent: str | None,
    weather: dict | None,
    through_week: int | None = None,
) -> TonightChance:
    depth, form, team_rates, defense = load_tonight_tables(through_week)
    team = _team(team_abbr)
    pos = "RB" if position == "FB" else position
    person = _person(player_name)
    rank = depth.get((team, pos, person))
    line = form.get((team, person), FormLine(0.0, 0.0, 0.0, 0.0, 0.0))
    if rank is None:
        return TonightChance(
            0.0,
            None,
            _per(line.carries, line.games),
            _per(line.targets, line.games),
            line.rush_tds + line.rec_tds,
            line.games,
            "Not on the current depth chart, so he is not in the scoring mix tonight.",
        )
    mates = []
    for (club, group, name), mate_rank in depth.items():
        if club != team:
            continue
        mate = form.get((club, name), FormLine(0.0, 0.0, 0.0, 0.0, 0.0))
        mates.append(
            {
                "person": name,
                "pos": group,
                "rank": mate_rank,
                "carries": mate.carries,
                "targets": mate.targets,
                "rush_tds": mate.rush_tds,
                "rec_tds": mate.rec_tds,
                "games": mate.games,
            }
        )
    if not any(row["person"] == person and row["pos"] == pos for row in mates):
        mates.append(
            {
                "person": person,
                "pos": pos,
                "rank": rank,
                "carries": line.carries,
                "targets": line.targets,
                "rush_tds": line.rush_tds,
                "rec_tds": line.rec_tds,
                "games": line.games,
            }
        )
    rush_rate, rec_rate = team_rates.get(team, team_rates.get("_league", (0.9, 1.3)))
    rush_factor, pass_factor = _defense_factors(defense, opponent)
    rush_weather, pass_weather = _weather_split(weather)
    chances = score_players(
        mates,
        team_rush_rate=rush_rate,
        team_rec_rate=rec_rate,
        rush_factor=rush_factor,
        pass_factor=pass_factor,
        rush_weather=rush_weather,
        pass_weather=pass_weather,
    )
    # Same person can sit on two chart spots. Keep the spot we were asked about.
    p = float(chances.get(person, 0.0))
    kind = "rush" if pos in {"RB", "QB", "FB"} else "pass"
    factor = rush_factor if kind == "rush" else pass_factor
    return TonightChance(
        p=p,
        depth_rank=rank,
        carries_per_game=_per(line.carries, line.games),
        targets_per_game=_per(line.targets, line.games),
        recent_tds=line.rush_tds + line.rec_tds,
        games=line.games,
        summary=_summary(pos, rank, line, p, kind, factor, weather),
    )


def _per(total: float, games: float) -> float:
    if games <= 0:
        return 0.0
    return total / games


def _defense_factors(
    defense: dict[str, dict[str, float]],
    opponent: str | None,
) -> tuple[float, float]:
    row = defense.get(_team(opponent)) or {}
    return (
        defense_factor("RB", row.get("rush"), row.get("league_rush")),
        defense_factor("WR", row.get("pass"), row.get("league_pass")),
    )


def _weather_split(weather: dict | None) -> tuple[float, float]:
    if not weather or bool(weather.get("indoors")):
        return 1.0, 1.0
    wind = weather.get("wind_mps")
    precip = weather.get("precip_probability")
    rush, recv = 1.0, 1.0
    if isinstance(wind, (int, float)) and wind >= 8:
        recv *= 0.92
    if isinstance(precip, (int, float)) and precip >= 0.6:
        rush *= 0.96
        recv *= 0.96
    return rush, recv


def _summary(
    pos: str,
    rank: int,
    line: FormLine,
    p: float,
    kind: str,
    factor: float,
    weather: dict | None,
) -> str:
    role = _role_name(pos, rank)
    bits = [role + "."]
    work = []
    carries = _per(line.carries, line.games)
    targets = _per(line.targets, line.games)
    if carries >= 0.8:
        work.append(f"{_num(carries)} carries")
    if targets >= 0.8:
        work.append(f"{_num(targets)} targets")
    if work:
        bits.append(f"Lately that is {' and '.join(work)} a game.")
    scores = line.rush_tds + line.rec_tds
    if scores >= 1.0:
        bits.append("He has been scoring in this stretch.")
    elif scores >= 0.45:
        bits.append("He has a recent score in this stretch.")
    elif line.games >= 0.8 and (carries >= 0.8 or targets >= 0.8):
        bits.append("No score in this stretch. Tonight follows the carries and targets.")
    if factor >= 1.04:
        bits.append(f"This defense has allowed more {kind}ing scores than usual.")
    elif factor <= 0.96:
        bits.append(f"This defense has allowed fewer {kind}ing scores than usual.")
    if weather and not weather.get("indoors"):
        wind = weather.get("wind_mps")
        if isinstance(wind, (int, float)) and wind >= 8 and pos in {"WR", "TE"}:
            bits.append("Wind cuts the passing chance.")
    bits.append(f"{round(p * 100)}% to score tonight.")
    return " ".join(bits)


def _role_name(pos: str, rank: int) -> str:
    if pos == "QB":
        return "Starting quarterback" if rank == 1 else "Backup quarterback"
    if pos in {"RB", "FB"}:
        return {1: "Lead back", 2: "Backup back"}.get(rank, "Deep back")
    if pos == "WR":
        return {1: "Top receiver", 2: "Second receiver"}.get(rank, "Depth receiver")
    if pos == "TE":
        return "Starting tight end" if rank == 1 else "Backup tight end"
    return "Skill player"


def _num(value: float) -> str:
    rounded = round(value, 1)
    if abs(rounded - round(rounded)) < 0.05:
        return str(int(round(rounded)))
    return f"{rounded:.1f}"


@lru_cache(maxsize=4)
def load_tonight_tables(through_week: int | None = None) -> tuple[
    dict[tuple[str, str, str], int],
    dict[tuple[str, str], FormLine],
    dict[str, tuple[float, float]],
    dict[str, dict[str, float]],
]:
    depth, _touches, _defense = load_current_role_tables()
    stats = pd.read_parquet(
        _cache_dir() / "stats_player_week_2026.parquet",
        columns=[
            "player_display_name",
            "position",
            "team",
            "opponent_team",
            "week",
            "carries",
            "targets",
            "rushing_tds",
            "receiving_tds",
        ],
    )
    week = pd.to_numeric(stats["week"], errors="coerce")
    latest = int(week.max())
    if through_week is not None:
        latest = min(latest, int(through_week))
    defense = _defense_from(stats, week, latest)
    recent = stats[week.ge(latest - LOOKBACK + 1) & week.le(latest)].copy()
    recent["week"] = pd.to_numeric(recent["week"], errors="coerce")
    recent["decay"] = DECAY ** (latest - recent["week"])
    form: dict[tuple[str, str], list[float]] = {}
    team_games: dict[str, float] = {}
    team_rush: dict[str, float] = {}
    team_rec: dict[str, float] = {}
    seen_weeks: set[tuple[str, int]] = set()
    for row in recent.itertuples(index=False):
        group = str(row.position or "").upper()
        if group == "FB":
            group = "RB"
        if group not in SKILL:
            continue
        club = _team(row.team)
        person = _person(row.player_display_name)
        decay = float(row.decay)
        bucket = form.setdefault((club, person), [0.0, 0.0, 0.0, 0.0, 0.0])
        bucket[0] += decay * float(row.carries or 0)
        bucket[1] += decay * float(row.targets or 0)
        bucket[2] += decay * float(row.rushing_tds or 0)
        bucket[3] += decay * float(row.receiving_tds or 0)
        bucket[4] += decay
        week_no = int(row.week)
        if (club, week_no) not in seen_weeks:
            seen_weeks.add((club, week_no))
            team_games[club] = team_games.get(club, 0.0) + decay
        team_rush[club] = team_rush.get(club, 0.0) + decay * float(row.rushing_tds or 0)
        team_rec[club] = team_rec.get(club, 0.0) + decay * float(row.receiving_tds or 0)
    lines = {
        key: FormLine(values[0], values[1], values[2], values[3], values[4])
        for key, values in form.items()
    }
    league_games = sum(team_games.values()) or 1.0
    league_rush = sum(team_rush.values()) / league_games
    league_rec = sum(team_rec.values()) / league_games
    rates: dict[str, tuple[float, float]] = {"_league": (league_rush, league_rec)}
    for club, games in team_games.items():
        rush = team_rush.get(club, 0.0) / games
        rec = team_rec.get(club, 0.0) / games
        rates[club] = (
            (SHRINK_GAMES * league_rush + games * rush) / (SHRINK_GAMES + games),
            (SHRINK_GAMES * league_rec + games * rec) / (SHRINK_GAMES + games),
        )
    return depth, lines, rates, defense


def _defense_from(stats: pd.DataFrame, week: pd.Series, latest: int) -> dict[str, dict[str, float]]:
    """Scores allowed over the three weeks that existed when this call was made."""
    window = stats[week.ge(latest - 2) & week.le(latest)]
    scored = window.groupby(["team", "opponent_team"], dropna=False)[
        ["rushing_tds", "receiving_tds"]
    ].sum()
    allowed: dict[str, list[float]] = {}
    for (_offense, defense_team), row in scored.iterrows():
        bucket = allowed.setdefault(_team(defense_team), [0.0, 0.0])
        bucket[0] += float(row["rushing_tds"] or 0)
        bucket[1] += float(row["receiving_tds"] or 0)
    if not allowed:
        return {}
    league_rush = sum(pair[0] for pair in allowed.values()) / len(allowed)
    league_pass = sum(pair[1] for pair in allowed.values()) / len(allowed)
    return {
        team: {
            "rush": pair[0],
            "pass": pair[1],
            "league_rush": league_rush,
            "league_pass": league_pass,
        }
        for team, pair in allowed.items()
    }
