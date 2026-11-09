"""Score upcoming games with Phase 3 player Anytime TD research forecasts.

Does not write over M3/M4 forecasts. Does not enable Validated Play.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from spe.db.base import as_utc
from spe.db.models import Game, InjuryEvent, Player, Team
from spe.identity.roster import eligible_players_for_game
from spe.integrity.assignments import linked_team_ids
from spe.models.player_td.model import predict_frame
from spe.models.player_td.shares import ShareWeights, attach_shares, default_weights
from spe.models.player_td.train import load_player_td_model, report_path
from spe.models.team_td.dataset import (
    build_team_td_frame,
    features_for_upcoming_game,
    injury_skill_out,
    weather_by_game,
)
from spe.models.team_td.model import predict_team_row
from spe.models.team_td.train import load_team_td_model
from spe.ops.availability import availability_for_player

SKILL = {"QB", "RB", "WR", "TE"}
VERIFY_HINTS = (
    ("cincinnati", "atlanta"),
    ("denver", "carolina"),
)


def _scoreboard_team_id(team_id: str, game: Game, teams: dict[str, Team]) -> str | None:
    """Map roster/alias team ids onto the game's home/away scoreboard id.

    FBS games often store ``uga`` on the schedule while rosters and CFBD stats use
    ``fbs:61``. Without this bridge, starters on the linked id are eligible but
    never scored — and their sportsbook quotes never reach the research board.
    """
    if team_id in (game.home_team_id, game.away_team_id):
        return team_id
    for side in (game.home_team_id, game.away_team_id):
        linked = linked_team_ids(teams.get(side)) | {side}
        if team_id in linked:
            return side
    return None


def score_upcoming_player_td(
    session: Session,
    *,
    game_ids: list[str] | None = None,
    horizon_hours: int = 24 * 14,
    now: datetime | None = None,
    hist_cache: dict[str, pd.DataFrame] | None = None,
    research_features: bool = False,
) -> dict[str, Any]:
    moment = as_utc(now) or datetime.now(UTC)
    out: dict[str, Any] = {
        "status": "ok",
        "as_of": moment.isoformat(),
        "play_enabled": False,
        "promoted": False,
        "research_only": True,
        "validated_play": False,
        "research_features": research_features,
        "examples": [],
        "games": [],
        "leakage_checks": [],
        "missing_info_summary": [],
    }

    targets = _select_games(session, game_ids=game_ids, horizon_hours=horizon_hours, now=moment)
    if not targets:
        out["status"] = "no_games"
        return out

    # Load history once per league for player priors.
    league_hist: dict[str, pd.DataFrame] = {}
    team_hist: dict[str, pd.DataFrame] = hist_cache or {}

    for game in targets:
        model = load_player_td_model(game.league_id)
        team_model = load_team_td_model(game.league_id)
        if model is None or team_model is None:
            out["games"].append({"game_id": game.game_id, "status": "missing_artifact"})
            continue

        if game.league_id not in league_hist:
            league_hist[game.league_id] = _player_history(session, game.league_id)
        if game.league_id not in team_hist:
            print(f"[player_td] team history {game.league_id}…", flush=True)
            team_hist[game.league_id] = build_team_td_frame(session, game.league_id)

        hist = league_hist[game.league_id]
        thist = team_hist[game.league_id]
        weights = ShareWeights.from_dict(model.share_weights) if model.share_weights else default_weights(game.league_id)

        teams = {
            t.team_id: t
            for t in session.query(Team).filter(Team.team_id.in_([game.home_team_id, game.away_team_id])).all()
        }
        # Pull alias twins (uga ↔ fbs:61) so scoreboard mapping and names resolve.
        extras: set[str] = set()
        for tid in list(teams):
            extras |= linked_team_ids(teams[tid])
        if extras - set(teams):
            for t in session.query(Team).filter(Team.team_id.in_(extras - set(teams))).all():
                teams[t.team_id] = t
        weather = weather_by_game(session, game.league_id).get(game.game_id)
        injuries_map = injury_skill_out(session, game.league_id).get(game.game_id, {})

        # Team λ for both sides.
        team_lambdas: dict[str, dict[str, Any]] = {}
        for team_id, is_home in ((game.home_team_id, True), (game.away_team_id, False)):
            opp = game.away_team_id if is_home else game.home_team_id
            feats = features_for_upcoming_game(
                thist,
                game_id=game.game_id,
                team_id=team_id,
                opp_id=opp,
                kickoff_utc=game.kickoff_utc,
                is_home=is_home,
                neutral_site=bool(game.neutral_site),
                league_id=game.league_id,
                weather=weather,
                injury=injuries_map.get(team_id),
            )
            as_of = datetime.fromisoformat(feats["as_of"])
            if as_of.tzinfo is None:
                as_of = as_of.replace(tzinfo=UTC)
            if as_of >= as_utc(game.kickoff_utc):
                out["leakage_checks"].append({"game_id": game.game_id, "issue": "team_as_of_not_before_kick"})
            team_lambdas[team_id] = predict_team_row(team_model, feats)

        eligible = eligible_players_for_game(session, game)
        skill_eligible = []
        for e in eligible:
            if e.position_group not in SKILL:
                continue
            board_team = _scoreboard_team_id(e.team_id, game, teams)
            if board_team is None:
                continue
            skill_eligible.append((e, board_team))
        complete_report = _game_injury_complete(session, game)
        from spe.ops.availability import games_with_inactive_list

        inactive_lists = games_with_inactive_list(session, [game.game_id])

        rows: list[dict[str, Any]] = []
        matchup_cache: dict[str, dict[str, Any]] = {}
        for ep, board_team in skill_eligible:
            player = session.get(Player, ep.player_id)
            if player is None:
                continue
            if board_team not in team_lambdas:
                continue
            inj = (
                session.query(InjuryEvent)
                .filter(InjuryEvent.game_id == game.game_id, InjuryEvent.player_id == ep.player_id)
                .order_by(InjuryEvent.reported_at.desc())
                .first()
            )
            avail = availability_for_player(
                league_id=game.league_id,
                kickoff=game.kickoff_utc,
                player_injury=inj,
                game_has_complete_report=complete_report,
                now=moment,
                inactive_list_received=game.game_id in inactive_lists,
            )
            # Confirmed unavailable still listed with p≈0 for transparency.
            # Pass current team so recent/season windows exclude prior-org blowups (transfers).
            # Prefer roster team id; also accept scoreboard alias for hist that used either form.
            priors = _player_priors_from_hist(
                hist,
                ep.player_id,
                game.kickoff_utc,
                game.league_id,
                current_team_id=ep.team_id,
                role=ep.role,
            )
            missing = list(priors.get("missing") or [])
            if avail["availability_status"] == "unknown":
                missing.append("availability_unknown_never_assumed_healthy")
            if game.league_id == "fbs":
                missing.extend(["fbs_rz_targets_not_in_source", "fbs_gl_carries_not_in_source"])
            for flag in priors.get("scope_flags") or []:
                if flag not in missing:
                    missing.append(flag)

            tl = team_lambdas[board_team]
            opp_id = game.away_team_id if board_team == game.home_team_id else game.home_team_id
            if opp_id not in matchup_cache:
                matchup_cache[opp_id] = _opponent_matchup_priors(
                    session, hist, opp_id, game.kickoff_utc, game.league_id
                )
            matchup = matchup_cache[opp_id]
            if matchup.get("opp_def_z") is None:
                missing.append("opp_def_z_unavailable")
            # Prefer matchup defense (this opponent) over any stale player-row opponent feature.
            opp_def_z = matchup.get("opp_def_z")
            opp_adj_def = matchup.get("opp_adj_def")
            if opp_adj_def is None:
                opp_adj_def = priors.get("opp_adj_def")
            usage = priors.get("usage_x_defense")
            share = priors.get("prior_target_share")
            if share is None:
                share = priors.get("prior_carry_share")
            if usage is None and share is not None and opp_def_z is not None:
                usage = float(share) * float(opp_def_z)
            rows.append(
                {
                    "game_id": game.game_id,
                    "league_id": game.league_id,
                    "kickoff_utc": as_utc(game.kickoff_utc).isoformat(),
                    "player_id": ep.player_id,
                    "player_name": player.name,
                    "team_id": board_team,
                    "roster_team_id": ep.team_id,
                    "team_name": teams[board_team].name if teams.get(board_team) else board_team,
                    "opp_id": opp_id,
                    "position_group": ep.position_group,
                    "eligibility": ep.eligibility,
                    "roster_status": ep.status,
                    "availability_status": avail["availability_status"],
                    "availability_prob": avail["availability_prob"],
                    "availability_coverage": avail.get("coverage"),
                    "team_lambda_off_td": tl["lambda_off_td"],
                    "team_lambda_pass_td": tl["lambda_pass_td"],
                    "team_lambda_rush_td": tl["lambda_rush_td"],
                    "team_exp_possessions": tl.get("exp_possessions"),
                    "prior_carries": priors.get("prior_carries"),
                    "prior_targets": priors.get("prior_targets"),
                    "prior_receptions": priors.get("prior_receptions"),
                    "prior_rz_targets": priors.get("prior_rz_targets"),
                    "prior_gl_carries": priors.get("prior_gl_carries"),
                    "prior_td_rate": priors.get("prior_td_rate"),
                    "roll3_td_rate": priors.get("roll3_td_rate"),
                    "prior_rush_td": priors.get("prior_rush_td"),
                    "prior_rec_td": priors.get("prior_rec_td"),
                    "prior_carry_share": priors.get("prior_carry_share"),
                    "prior_target_share": priors.get("prior_target_share"),
                    "workload_delta": priors.get("workload_delta"),
                    "snap_roll3": priors.get("snap_roll3"),
                    "own_off_epa": priors.get("own_off_epa"),
                    "opp_def_z": opp_def_z,
                    "opp_adj_def": opp_adj_def,
                    "team_rz_td": priors.get("team_rz_td"),
                    "team_points_per_opp": priors.get("team_points_per_opp"),
                    "usage_x_defense": usage,
                    "is_home": 1.0 if board_team == game.home_team_id else 0.0,
                    "sample_size": priors.get("sample_size", 0),
                    "season_td_rate": priors.get("season_td_rate"),
                    "recent8_td_rate": priors.get("recent8_td_rate"),
                    "recent8_td_rate_current_team": priors.get("recent8_td_rate_current_team"),
                    "season_td_rate_current_team": priors.get("season_td_rate_current_team"),
                    "prior_td_rate_all_teams": priors.get("prior_td_rate_all_teams"),
                    "prior_atd_game_rate": priors.get("prior_atd_game_rate"),
                    "fbs_receptions_usage_proxy": priors.get("fbs_receptions_usage_proxy"),
                    "season_sample_size": priors.get("season_sample_size"),
                    "recent8_sample_size": priors.get("recent8_sample_size"),
                    "current_team_sample_size": priors.get("current_team_sample_size"),
                    "missing_information": missing,
                    "opp_def_source": matchup.get("source"),
                }
            )

        if not rows:
            out["games"].append({"game_id": game.game_id, "status": "no_eligible_skill"})
            continue

        frame = pd.DataFrame(rows)
        frame = attach_shares(frame, weights)
        frame["lambda_rush_td"] = frame["team_lambda_rush_td"] * frame["rush_td_share"]
        frame["lambda_rec_td"] = frame["team_lambda_pass_td"] * frame["rec_td_share"]
        frame["lambda_player_td"] = frame["lambda_rush_td"] + frame["lambda_rec_td"]
        # Never silently replace missing opponent defense with 0 (league-average disguise).
        scored = predict_frame(model, frame)
        scored = scored.sort_values("p_atd", ascending=False)

        game_blob = {
            "game_id": game.game_id,
            "league_id": game.league_id,
            "kickoff_utc": as_utc(game.kickoff_utc).isoformat(),
            "home": teams.get(game.home_team_id).name if teams.get(game.home_team_id) else game.home_team_id,
            "away": teams.get(game.away_team_id).name if teams.get(game.away_team_id) else game.away_team_id,
            "n_eligible_skill": len(skill_eligible),
            "team_expectations": {
                tid: {
                    "team_name": teams[tid].name if teams.get(tid) else tid,
                    "lambda_off_td": team_lambdas[tid]["lambda_off_td"],
                    "lambda_pass_td": team_lambdas[tid]["lambda_pass_td"],
                    "lambda_rush_td": team_lambdas[tid]["lambda_rush_td"],
                    "p_off_td": team_lambdas[tid]["p_off_td"],
                }
                for tid in team_lambdas
            },
            "players": [
                _export_player_row(r, game.league_id, research_features=research_features)
                for r in scored.itertuples(index=False)
            ],
        }
        out["games"].append(game_blob)
        # Top examples for report
        for r in scored.head(8).itertuples(index=False):
            if len(out["examples"]) < 24:
                out["examples"].append(
                    {
                        "game_id": game.game_id,
                        "matchup": f"{game_blob['away']} @ {game_blob['home']}",
                        "player_name": r.player_name,
                        "position_group": r.position_group,
                        "team_lambda_off_td": float(r.team_lambda_off_td),
                        "rush_td_share": float(r.rush_td_share),
                        "rec_td_share": float(r.rec_td_share),
                        "p_atd": float(r.p_atd),
                        "p_atd_low": float(r.p_atd_low),
                        "p_atd_high": float(r.p_atd_high),
                        "availability_status": r.availability_status,
                        "missing_information": r.missing_information,
                    }
                )
        print(
            f"[player_td] scored {game.game_id}: {len(scored)} skill players",
            flush=True,
        )

    # Persist
    live_path = report_path().parent / "phase3_player_td_live.json"
    live_path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    out["live_path"] = str(live_path)
    if report_path().exists():
        try:
            report = json.loads(report_path().read_text(encoding="utf-8"))
        except Exception:
            report = {}
        report["live_scoring"] = {
            "as_of": out["as_of"],
            "n_games": len(out["games"]),
            "leakage_checks": out["leakage_checks"],
            "example_games": [
                {
                    "game_id": g.get("game_id"),
                    "matchup": f"{g.get('away')} @ {g.get('home')}",
                    "n_players": len(g.get("players") or []),
                    "team_expectations": g.get("team_expectations"),
                    "top_players": (g.get("players") or [])[:6],
                }
                for g in out["games"]
                if g.get("status") is None or g.get("players")
            ],
        }
        report["live_scoring_path"] = str(live_path)
        report_path().write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        out["report_path"] = str(report_path())
    return out


def _select_games(
    session: Session,
    *,
    game_ids: list[str] | None,
    horizon_hours: int,
    now: datetime,
) -> list[Game]:
    """All upcoming NFL/FBS games in horizon (data_tier=upcoming).

    VERIFY_HINTS are sorted first for stable demos but no longer *exclude*
    the rest of the slate — that was blocking player_td / HistGBM freezes.
    """
    if game_ids:
        return session.query(Game).filter(Game.game_id.in_(game_ids)).all()
    upcoming = (
        session.query(Game)
        .filter(
            Game.data_tier == "upcoming",
            Game.status.in_(("scheduled", "preview", "pregame")),
            Game.kickoff_utc >= now,
            Game.kickoff_utc <= now + timedelta(hours=horizon_hours),
            Game.league_id.in_(("nfl", "fbs")),
        )
        .order_by(Game.kickoff_utc)
        .all()
    )
    hinted: list[Game] = []
    rest: list[Game] = []
    for game in upcoming:
        teams = {
            t.team_id: t
            for t in session.query(Team).filter(Team.team_id.in_([game.home_team_id, game.away_team_id])).all()
        }
        home = (teams.get(game.home_team_id).name if teams.get(game.home_team_id) else "").lower()
        away = (teams.get(game.away_team_id).name if teams.get(game.away_team_id) else "").lower()
        blob = f"{away} {home} {game.game_id}".lower()
        is_hint = any(a in blob and b in blob for a, b in VERIFY_HINTS)
        (hinted if is_hint else rest).append(game)
    return hinted + rest


def _game_injury_complete(session: Session, game: Game) -> bool:
    row = session.execute(
        text(
            """
            SELECT MAX(CASE WHEN coverage_complete THEN 1 ELSE 0 END) AS ok
            FROM injury_events
            WHERE game_id = :gid AND reported_at < :kick
            """
        ),
        {"gid": game.game_id, "kick": game.kickoff_utc},
    ).mappings().first()
    return bool(row and row["ok"])


def _player_history(session: Session, league_id: str) -> pd.DataFrame:
    """Compact prior history from player_game_stats for live priors."""
    from spe.models.advanced import load_model_frame
    from spe.models.rolling_td import _add_td_recency

    frame = load_model_frame(session, league_id, year_max=2026, universe="involved")
    if frame.empty:
        return frame
    frame = frame[frame["position_group"].isin(SKILL)].copy()
    frame = _add_td_recency(frame)
    # Component priors
    frame = frame.sort_values(["player_id", "kickoff_utc"])
    g = frame.groupby("player_id", sort=False)
    for src, dest in (("rush_td", "prior_rush_td"), ("rec_td", "prior_rec_td")):
        frame[dest] = g[src].transform(lambda s: s.shift(1).expanding(min_periods=1).mean())
    if league_id == "fbs":
        for col in ("prior_rz_targets", "prior_gl_carries", "prior_target_share"):
            if col in frame.columns:
                frame[col] = np.nan
    return frame


def _export_player_row(r: Any, league_id: str, *, research_features: bool) -> dict[str, Any]:
    """Serialize a scored player row; optionally keep research feature columns for unified forecasting."""

    def _f(name: str) -> float | None:
        val = getattr(r, name, None)
        if val is None or (isinstance(val, float) and not np.isfinite(val)):
            return None
        try:
            if pd.isna(val):
                return None
        except (TypeError, ValueError):
            pass
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    base: dict[str, Any] = {
        "player_id": r.player_id,
        "player_name": r.player_name,
        "team_id": getattr(r, "team_id", None),
        "team_name": r.team_name,
        "position_group": r.position_group,
        "availability_status": r.availability_status,
        "availability_prob": r.availability_prob,
        "team_lambda_off_td": float(r.team_lambda_off_td),
        "exp_carries": _f("prior_carries"),
        "exp_targets": _f("prior_targets"),
        "exp_receptions": _f("prior_receptions"),
        "exp_rz_targets": None if league_id == "fbs" else _f("prior_rz_targets"),
        "exp_gl_carries": None if league_id == "fbs" else _f("prior_gl_carries"),
        "rush_td_share": float(r.rush_td_share),
        "rec_td_share": float(r.rec_td_share),
        "lambda_player_td": float(r.lambda_player_td_adj),
        "opp_def_z": _f("opp_def_z"),
        "is_home": _f("is_home"),
        "p_atd": float(r.p_atd),
        "p_atd_low": float(r.p_atd_low),
        "p_atd_high": float(r.p_atd_high),
        "missing_information": list(r.missing_information)
        if isinstance(r.missing_information, list)
        else r.missing_information,
    }
    if research_features:
        for col in (
            "prior_td_rate",
            "roll3_td_rate",
            "roll5_td_rate",
            "workload_delta",
            "prior_carry_share",
            "prior_target_share",
            "prior_rz_targets",
            "prior_gl_carries",
            "prior_carries",
            "prior_targets",
            "prior_receptions",
            "own_off_epa",
            "opp_adj_def",
            "team_rz_td",
            "team_points_per_opp",
            "usage_x_defense",
            "snap_roll3",
            "sample_size",
            "season_td_rate",
            "recent8_td_rate",
            "recent8_td_rate_current_team",
            "season_td_rate_current_team",
            "prior_td_rate_all_teams",
            "prior_atd_game_rate",
            "fbs_receptions_usage_proxy",
            "season_sample_size",
            "recent8_sample_size",
            "current_team_sample_size",
        ):
            base[col] = _f(col)
        src = getattr(r, "opp_def_source", None)
        if src is not None:
            base["opp_def_source"] = src
    return base


def _shrink_role_workload(out: dict[str, Any], role: str | None, scope_flags: list[str]) -> None:
    """Shrink prior-school workload for a new transfer, and reserve touch shares."""
    from spe.identity.roles import ROLE_NEW_TRANSFER, ROLE_RESERVE, workload_factor

    factor = workload_factor(role)
    if factor == 1.0 or role not in {ROLE_NEW_TRANSFER, ROLE_RESERVE}:
        return
    games_here = int(out.get("current_team_sample_size") or 0)
    if role == ROLE_NEW_TRANSFER and games_here >= 2:
        return
    if role == ROLE_RESERVE and games_here < 2:
        return
    for key in (
        "prior_carries",
        "prior_receptions",
        "prior_rush_td",
        "prior_rec_td",
        "prior_carry_share",
        "fbs_receptions_usage_proxy",
        "recent8_td_rate",
        "recent8_td_rate_current_team",
        "roll3_td_rate",
    ):
        value = out.get(key)
        if isinstance(value, (int, float)):
            out[key] = float(value) * factor
    if role == ROLE_NEW_TRANSFER and out.get("recent8_td_rate_current_team") is None:
        base = out.get("recent8_td_rate")
        if isinstance(base, (int, float)):
            out["recent8_td_rate_current_team"] = float(base)
    scope_flags.append(
        "new_transfer_prior_org_usage_shrunk" if role == ROLE_NEW_TRANSFER else "reserve_role_usage_shrunk"
    )
    out["role"] = role
    out["role_workload_factor"] = factor


def _player_priors_from_hist(
    hist: pd.DataFrame,
    player_id: str,
    kickoff: datetime,
    league_id: str,
    *,
    current_team_id: str | None = None,
    role: str | None = None,
) -> dict[str, Any]:
    if hist.empty:
        return {"missing": ["no_player_history"], "sample_size": 0}
    kick = pd.Timestamp(kickoff)
    if kick.tzinfo is None:
        kick = kick.tz_localize("UTC")
    as_of = kick - pd.Timedelta(seconds=1)
    part = hist.loc[
        (hist["player_id"] == player_id) & (pd.to_datetime(hist["kickoff_utc"], utc=True) < as_of)
    ].sort_values("kickoff_utc")
    missing: list[str] = []
    scope_flags: list[str] = []
    if part.empty:
        return {"missing": ["player_no_prior_games"], "sample_size": 0, "scope_flags": []}
    last = part.iloc[-1]

    def get(col: str) -> float | None:
        if col not in part.columns:
            missing.append(col)
            return None
        # Prefer the already-shifted prior on the last historical row (which is prior to that game);
        # for live, use expanding mean of raw outcomes excluding nothing further.
        if col.startswith("prior_"):
            val = last.get(col)
            if pd.isna(val):
                missing.append(col)
                return None
            return float(val)
        val = pd.to_numeric(part[col], errors="coerce").mean()
        if pd.isna(val):
            missing.append(col)
            return None
        return float(val)

    # Canonical priors match training: expanding means over ALL completed games before kickoff.
    # Recent/season windows are separate research fields — never silently replace prior_td_rate.
    def mean_window(col: str, source: pd.DataFrame) -> float | None:
        if col not in source.columns or source.empty:
            return None
        val = pd.to_numeric(source[col], errors="coerce").mean()
        return None if pd.isna(val) else float(val)

    season_year = None
    season_part = part.iloc[0:0]
    if "season_year" in part.columns and part["season_year"].notna().any():
        season_year = int(part["season_year"].iloc[-1])
        season_part = part.loc[part["season_year"] == season_year]
    recent8 = part.tail(8)

    # Current-team slice: transfers must not inherit prior-org TD blowups into C_recent windows.
    # Also accept null team_id rows when the scoreboard/roster alias matches (common FBS gap).
    team_part = part.iloc[0:0]
    if current_team_id and "team_id" in part.columns:
        tid = str(current_team_id)
        mask = part["team_id"].astype(str) == tid
        # Null team_id: include only when this player has no conflicting other-team rows
        # (avoids swallowing prior-org history into the current-team window).
        null_mask = part["team_id"].isna() | (part["team_id"].astype(str).isin(["None", "nan", ""]))
        non_null = part.loc[~null_mask]
        if len(non_null) == 0:
            mask = mask | null_mask
        elif (non_null["team_id"].astype(str) == tid).all():
            mask = mask | null_mask
        team_part = part.loc[mask]
        if len(team_part) and len(team_part) < len(part):
            scope_flags.append("prior_org_games_excluded_from_current_team_windows")
        elif team_part.empty and len(part):
            scope_flags.append("no_current_team_box_history_yet")

    team_season_part = team_part.iloc[0:0]
    if len(team_part) and season_year is not None and "season_year" in team_part.columns:
        team_season_part = team_part.loc[team_part["season_year"] == season_year]
    team_recent8 = team_part.tail(8) if len(team_part) else team_part

    # Prior-only roll3 TD rate from completed games before kickoff (mean of TD counts).
    roll3 = None
    roll_src = team_part if len(team_part) else part
    if "anytime_tds" in roll_src.columns and len(roll_src):
        tail = pd.to_numeric(roll_src["anytime_tds"], errors="coerce").tail(3)
        if tail.notna().any():
            roll3 = float(tail.mean())

    atd = pd.to_numeric(part["anytime_tds"], errors="coerce") if "anytime_tds" in part.columns else pd.Series(dtype=float)
    expanding_td = mean_window("anytime_tds", part)  # mean TD count / game (training definition)
    atd_game_rate = float((atd.fillna(0) > 0).mean()) if len(atd) else None  # % games with ≥1 ATD

    # Workload features for live FBS: prefer current-team means when available.
    carries_src = team_part if len(team_part) else part
    receptions_src = team_part if len(team_part) else part

    out = {
        # Expanding means over full pre-kickoff history — matches add_player_priors / M5 training.
        "prior_carries": mean_window("carries", carries_src),
        "prior_targets": None if league_id == "fbs" else mean_window("targets", part),
        "prior_receptions": mean_window("receptions", receptions_src),
        "prior_rz_targets": None if league_id == "fbs" else mean_window("rz_targets", part),
        "prior_gl_carries": None if league_id == "fbs" else mean_window("gl_carries", part),
        "prior_td_rate": expanding_td,
        "prior_td_rate_all_teams": expanding_td,
        "roll3_td_rate": roll3 if roll3 is not None else get("roll3_td_rate"),
        "prior_rush_td": mean_window("rush_td", carries_src),
        "prior_rec_td": mean_window("rec_td", receptions_src),
        # Expanding mean of observed shares over completed games (matches training shift+expanding).
        "prior_carry_share": mean_window("carry_share", carries_src)
        if "carry_share" in part.columns
        else get("prior_carry_share"),
        "prior_target_share": None
        if league_id == "fbs"
        else (
            mean_window("target_share", part)
            if "target_share" in part.columns
            else get("prior_target_share")
        ),
        "workload_delta": get("workload_delta"),
        "snap_roll3": None if league_id == "fbs" else get("snap_roll3"),
        "own_off_epa": get("own_off_epa"),
        # Opponent defense is matchup-specific — filled by _opponent_matchup_priors, never zero-filled here.
        "opp_def_z": None,
        "opp_adj_def": get("opp_adj_def"),
        "team_rz_td": get("team_rz_td"),
        "team_points_per_opp": get("team_points_per_opp"),
        "usage_x_defense": None,  # recomputed after matchup join; do not reuse stale hist cell
        "sample_size": int(len(part)),
        "current_team_sample_size": int(len(team_part)),
        # Explicit alternate windows for research variants (not substitutes for prior_*).
        # All-team windows kept for audit; current-team windows drive C_recent live scoring.
        "season_td_rate": mean_window("anytime_tds", season_part) if len(season_part) else None,
        "recent8_td_rate": mean_window("anytime_tds", recent8),
        "season_td_rate_current_team": (
            mean_window("anytime_tds", team_season_part) if len(team_season_part) else None
        ),
        "recent8_td_rate_current_team": (
            mean_window("anytime_tds", team_recent8) if len(team_recent8) else None
        ),
        "season_carries": mean_window("carries", season_part) if len(season_part) else None,
        "recent8_carries": mean_window("carries", recent8),
        "season_receptions": mean_window("receptions", season_part) if len(season_part) else None,
        "recent8_receptions": mean_window("receptions", recent8),
        "season_sample_size": int(len(season_part)),
        "recent8_sample_size": int(len(recent8)),
        "prior_atd_game_rate": atd_game_rate,
        "season_year": season_year,
        # FBS: receptions are an explicitly labeled usage proxy — never written into prior_targets.
        "fbs_receptions_usage_proxy": mean_window("receptions", receptions_src) if league_id == "fbs" else None,
        "missing": missing,
        "scope_flags": scope_flags,
    }
    if league_id == "fbs":
        missing.append("fbs_true_targets_unavailable_receptions_proxy_separate")
    _shrink_role_workload(out, role, scope_flags)
    return out


def _opponent_matchup_priors(
    session: Session,
    hist: pd.DataFrame,
    opp_id: str,
    kickoff: datetime,
    league_id: str,
) -> dict[str, Any]:
    """Point-in-time opponent defensive quality (matches training ``_attach_team_environment``).

    Definition: opponent's expanding prior ``def_epa_per_play`` (PPA/EPA allowed) from
    completed games only, then z-scored against the hist-frame ``opp_def`` distribution.
    Positive z ⇒ opponent has allowed more EPA than average (softer defense).
    Both teams in a game can have positive z if both face soft defenses — signs are
    independent, not forced opposite.

    Unavailable → None (never disguised as 0).
    """
    kick = pd.Timestamp(as_utc(kickoff) or kickoff)
    if kick.tzinfo is None:
        kick = kick.tz_localize("UTC")
    as_of = kick - pd.Timedelta(seconds=1)

    rows = session.execute(
        text(
            """
            SELECT g.kickoff_utc,
                   json_extract(t.metrics, '$.def_epa_per_play') AS def_epa,
                   json_extract(t.metrics, '$.adj_def_epa') AS adj_def
            FROM team_analytics t
            JOIN games g ON g.game_id = t.game_id
            WHERE t.team_id = :opp AND t.league_id = :lg AND t.grain = 'game'
              AND g.status = 'final' AND g.kickoff_utc < :as_of
            ORDER BY g.kickoff_utc
            """
        ),
        {"opp": opp_id, "lg": league_id, "as_of": as_of.to_pydatetime()},
    ).mappings().all()
    if not rows:
        return {"opp_def_z": None, "opp_adj_def": None, "opp_def_raw": None, "source": "unavailable"}

    def_epas = pd.to_numeric(pd.Series([r["def_epa"] for r in rows]), errors="coerce")
    adj = pd.to_numeric(pd.Series([r["adj_def"] for r in rows]), errors="coerce")
    # Expanding prior through each game, then take the prior entering the next (live) game:
    # mean of all completed def_epa rows before as_of (equiv. to shift(1).expanding on next row).
    prior_def = float(def_epas.dropna().mean()) if def_epas.notna().any() else None
    prior_adj = float(adj.dropna().mean()) if adj.notna().any() else None
    if prior_def is None:
        return {"opp_def_z": None, "opp_adj_def": None, "opp_def_raw": None, "source": "unavailable"}

    if not hist.empty and "opp_def" in hist.columns:
        league = pd.to_numeric(hist["opp_def"], errors="coerce").dropna()
        mu = float(league.mean()) if len(league) else 0.0
        sd = float(league.std(ddof=0)) if len(league) else 1.0
        if sd < 1e-8:
            sd = 1.0
        z = (prior_def - mu) / sd
        source = "team_analytics_prior_z_vs_hist"
    else:
        z = None
        source = "team_analytics_prior_unzscored"
    return {
        "opp_def_z": float(z) if z is not None and np.isfinite(z) else None,
        "opp_adj_def": prior_adj,
        "opp_def_raw": prior_def,
        "source": source,
    }
