"""Flatten CFBD /games/players box scores. Do not invent involvement from play text."""

from __future__ import annotations

from typing import Any


_OFFENSE = {"rushing", "receiving", "passing"}


def _num(value: Any, default: float | int | None = 0) -> float | int | None:
    if value is None or value == "":
        return default
    text = str(value).strip().replace(",", "")
    if "/" in text and text.replace("/", "").replace(".", "").isdigit() is False:
        pass
    try:
        number = float(text)
    except (TypeError, ValueError):
        return default
    if float(number).is_integer():
        return int(number)
    return float(number)


def parse_comp_att(value: Any) -> tuple[int, int]:
    text = str(value or "").strip()
    if "/" not in text:
        return 0, int(_num(text, 0) or 0)
    left, right = text.split("/", 1)
    return int(_num(left, 0) or 0), int(_num(right, 0) or 0)


def flatten_game_players(raw_game: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per athlete who recorded rushing, receiving, or passing stats."""
    game_ext = str(raw_game.get("id") or "")
    acc: dict[str, dict[str, Any]] = {}
    for team in raw_game.get("teams") or []:
        team_name = str(team.get("team") or "")
        for cat in team.get("categories") or []:
            cname = str(cat.get("name") or "")
            if cname not in _OFFENSE:
                continue
            by_id: dict[str, dict[str, Any]] = {}
            for typ in cat.get("types") or []:
                tname = str(typ.get("name") or "")
                for athlete in typ.get("athletes") or []:
                    aid = str(athlete.get("id") or "")
                    if not aid:
                        continue
                    row = by_id.setdefault(
                        aid,
                        {"athlete_id": aid, "name": str(athlete.get("name") or ""), "team": team_name},
                    )
                    row[tname] = athlete.get("stat")
            for aid, stats in by_id.items():
                dest = acc.setdefault(
                    aid,
                    {
                        "athlete_id": aid,
                        "name": stats.get("name") or "",
                        "team": team_name,
                        "game_ext": game_ext,
                        "attempts": 0,
                        "carries": 0,
                        "receptions": 0,
                        "targets": 0,
                        "rush_yards": 0.0,
                        "rec_yards": 0.0,
                        "rush_td": 0,
                        "rec_td": 0,
                        "pass_td": 0,
                    },
                )
                if stats.get("name"):
                    dest["name"] = stats["name"]
                dest["team"] = team_name
                if cname == "rushing":
                    dest["carries"] = int(_num(stats.get("CAR"), 0) or 0)
                    dest["rush_yards"] = float(_num(stats.get("YDS"), 0) or 0)
                    dest["rush_td"] = int(_num(stats.get("TD"), 0) or 0)
                elif cname == "receiving":
                    dest["receptions"] = int(_num(stats.get("REC"), 0) or 0)
                    dest["rec_yards"] = float(_num(stats.get("YDS"), 0) or 0)
                    dest["rec_td"] = int(_num(stats.get("TD"), 0) or 0)
                    dest["targets"] = dest["receptions"]
                elif cname == "passing":
                    _comp, att = parse_comp_att(stats.get("C/ATT"))
                    dest["attempts"] = att
                    dest["pass_td"] = int(_num(stats.get("TD"), 0) or 0)
    return [row for row in acc.values() if row["athlete_id"] and row["game_ext"]]
