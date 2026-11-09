"""Score upcoming NFL/FBS games with the team-TD opportunity engine.

Research-only. Does not write player forecasts. Does not enable Validated Play.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from spe.db.base import as_utc
from spe.db.models import Game, Team
from spe.models.team_td.dataset import (
    build_team_td_frame,
    features_for_upcoming_game,
    injury_skill_out,
    weather_by_game,
)
from spe.models.team_td.model import predict_team_row
from spe.models.team_td.train import load_team_td_model, report_path


def score_upcoming_team_td(
    session: Session,
    *,
    leagues: tuple[str, ...] = ("nfl", "fbs"),
    horizon_hours: int = 24 * 14,
    now: datetime | None = None,
    hist_cache: dict[str, pd.DataFrame] | None = None,
    limit_per_league: int | None = 12,
) -> dict[str, Any]:
    moment = as_utc(now) or datetime.now(UTC)
    out: dict[str, Any] = {
        "status": "ok",
        "as_of": moment.isoformat(),
        "play_enabled": False,
        "promoted": False,
        "research_only": True,
        "validated_play": False,
        "leakage_checks": [],
        "leagues": {},
        "examples": [],
    }
    for league_id in leagues:
        model = load_team_td_model(league_id)
        if model is None:
            out["leagues"][league_id] = {"status": "missing_artifact"}
            continue
        if hist_cache is not None and league_id in hist_cache:
            hist = hist_cache[league_id]
        else:
            print(f"[team_td] loading history for live score {league_id}…", flush=True)
            hist = build_team_td_frame(session, league_id)
        if hist.empty:
            out["leagues"][league_id] = {"status": "no_history"}
            continue

        games = (
            session.query(Game)
            .filter(
                Game.league_id == league_id,
                Game.data_tier == "upcoming",
                Game.status.in_(("scheduled", "preview", "pregame")),
                Game.kickoff_utc >= moment,
                Game.kickoff_utc <= moment + timedelta(hours=horizon_hours),
            )
            .order_by(Game.kickoff_utc)
            .all()
        )
        if limit_per_league is not None:
            games = games[:limit_per_league]

        weather = weather_by_game(session, league_id)
        injuries = injury_skill_out(session, league_id)
        scored: list[dict[str, Any]] = []
        for game in games:
            teams = {
                t.team_id: t
                for t in session.query(Team)
                .filter(Team.team_id.in_([game.home_team_id, game.away_team_id]))
                .all()
            }
            for team_id, is_home in ((game.home_team_id, True), (game.away_team_id, False)):
                opp_id = game.away_team_id if is_home else game.home_team_id
                feats = features_for_upcoming_game(
                    hist,
                    game_id=game.game_id,
                    team_id=team_id,
                    opp_id=opp_id,
                    kickoff_utc=game.kickoff_utc,
                    is_home=is_home,
                    neutral_site=bool(game.neutral_site),
                    league_id=league_id,
                    weather=weather.get(game.game_id),
                    injury=injuries.get(game.game_id, {}).get(team_id),
                )
                # Leakage: as_of must be before kickoff; sample_size from prior games only.
                as_of = datetime.fromisoformat(feats["as_of"])
                if as_of.tzinfo is None:
                    as_of = as_of.replace(tzinfo=UTC)
                kick = as_utc(game.kickoff_utc)
                if as_of >= kick:
                    out["leakage_checks"].append(
                        {"game_id": game.game_id, "team_id": team_id, "issue": "as_of_not_before_kickoff"}
                    )
                pred = predict_team_row(model, feats)
                team = teams.get(team_id)
                row = {
                    "game_id": game.game_id,
                    "league_id": league_id,
                    "kickoff_utc": kick.isoformat() if kick else None,
                    "team_id": team_id,
                    "team_name": team.name if team else team_id,
                    "opp_id": opp_id,
                    "opp_name": teams[opp_id].name if teams.get(opp_id) else opp_id,
                    "is_home": is_home,
                    "sample_size": feats["sample_size"],
                    "weather_available": feats["weather_available"],
                    "injury_report_complete": feats["injury_report_complete"],
                    "features_snapshot": {
                        "prior_off_td": feats["prior_off_td"],
                        "opp_prior_off_td_allowed": feats["opp_prior_off_td_allowed"],
                        "epa_edge": feats["epa_edge"],
                        "exp_possessions": feats["exp_possessions"],
                        "home_field": feats["home_field"],
                    },
                    **pred,
                }
                scored.append(row)
                if len(out["examples"]) < 8:
                    out["examples"].append(row)

        out["leagues"][league_id] = {
            "status": "ok",
            "n_games": len(games),
            "n_team_rows": len(scored),
            "train_end": model.train_end,
            "features": model.feature_names,
            "rows": scored,
        }
        print(f"[team_td] scored {league_id}: {len(games)} games / {len(scored)} team rows", flush=True)

    # Merge into report if present.
    path = report_path()
    if path.exists():
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            report = {}
        report["live_scoring"] = {
            "as_of": out["as_of"],
            "examples": out["examples"],
            "leakage_checks": out["leakage_checks"],
            "summary": {
                lg: {
                    "status": body.get("status"),
                    "n_games": body.get("n_games"),
                    "n_team_rows": body.get("n_team_rows"),
                }
                for lg, body in out["leagues"].items()
            },
        }
        # Keep full rows in a sibling file to avoid huge report.
        live_path = path.parent / "phase2_team_td_live.json"
        live_path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
        report["live_scoring_path"] = str(live_path)
        path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        out["report_path"] = str(path)
        out["live_path"] = str(live_path)

    return out
