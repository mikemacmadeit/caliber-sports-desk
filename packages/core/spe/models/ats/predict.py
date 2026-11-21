"""Score upcoming NFL/FBS games with the corrected ATS research bundle."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from spe.market.odds import american_to_implied, ev_per_dollar
from spe.models.ats.guards import withhold_for_history_gap
from spe.models.ats.labels import grade_side, normal_cover_probs
from spe.models.ats.model import fit_ridge_margin, score_predictions
from spe.models.ats.ratings import build_team_strength_frame
from spe.models.ats.research_bundle import (
    CLEAN_PROSPECTIVE_COHORT,
    PIPELINE_VERSION,
    SUPERSEDED_CLAIM,
    load_ats_research_bundle,
)
from spe.models.ats.simulate import simulate_margins
from spe.models.ats.spread_contract import classify_spread_source


def _score_int(value: Any) -> int | None:
    number = _finite(value)
    if number is None:
        return None
    return int(number)


def _finite(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if np.isnan(number):
        return None
    return number


def format_spread_line(line: float) -> str:
    rounded = round(float(line), 1)
    if abs(rounded - round(rounded)) < 1e-6:
        text = str(int(round(rounded)))
    else:
        text = f"{rounded:.1f}"
    if rounded > 0:
        return f"+{text}"
    return text


# Saved margin model, scored on games after its training cutoff through October 2026.
# NFL model side covered 55.5% of 470 games when it was 1 to 7 points off the number.
# College model side covered 54.6% of 326 games when it was 1 to 3 points off the
# number. From 3 to 7 points it was 50.7% of 592, so those stay unpublished.
# A gap under a point is not a pick in either league.
# A gap of a touchdown or more names the number's side. The close bands name ours.
# The number's side is skipped when it is laying points with fewer wins than
# the opponent. That pick stays on our side.
FBS_GRADED_COVER_RATE = 0.524
SPREAD_MIN_DISAGREEMENT = 7.0
CLOSE_MIN_DISAGREEMENT = 1.0
FBS_CLOSE_MAX = 3.0
# The scoreboard fallback has no play-by-play, so its opinion is an
# opponent-adjusted margin. A club with fewer than three prior games is an
# unknown (an FCS opponent counts as average otherwise). A gap of a field
# goal or more is the band this prior has not earned, so those stay off the card.
SLATE_MIN_GAMES = 3
SLATE_HOME_FIELD = 2.5
SLATE_RIDGE = 10.0
FBS_GRADED_COVER_LABEL = "Covered 52% of recent college games"
# NFL holdout from 2024-09-01 with pre-kickoff Out/Doubtful counts: 53.7% of 681 games.
# The same window without injuries covered 51.8%.
NFL_GRADED_COVER_RATE = 0.537
NFL_GRADED_COVER_LABEL = "Covered 54% of recent NFL games"
# Years before 2024-08-15: an 85% weight on the posted number picked the winner
# more often than the score model alone. Later college games: 76% vs 69%.
# Later NFL games: 68% vs 61%.
MARKET_WINNER_WEIGHT = 0.85
# Finished games stay on the card for four days, long enough for a
# Thursday kickoff to still be here on Sunday.
# Keep a full prior week of finals on the card so weekend grades stay visible
# through the next slate (research board, not a season archive).
BOARD_RESULT_LOOKBACK = timedelta(days=10)


def blend_margin_toward_market(
    predicted_margin: float,
    market_margin: float | None,
    *,
    weight: float = MARKET_WINNER_WEIGHT,
) -> float:
    """Pull the score toward the posted number. The books pick the winner more often."""
    if market_margin is None:
        return float(predicted_margin)
    share = min(max(float(weight), 0.0), 1.0)
    return (1.0 - share) * float(predicted_margin) + share * float(market_margin)


def moneyline_record_side(
    *,
    blended_margin: float,
    raw_margin: float | None,
    market_margin: float | None,
    home_record: object = None,
    away_record: object = None,
) -> str:
    """Use our winner when the favorite has fewer wins and our guess already likes the dog.

    The blended margin otherwise stays. A missing record does not move the pick.
    """
    side = "home" if blended_margin >= 0 else "away"
    market = _finite(market_margin)
    raw = _finite(raw_margin)
    if market is None or market == 0 or raw is None or raw == 0:
        return side
    book = "home" if market > 0 else "away"
    our = "home" if raw > 0 else "away"
    if our == book:
        return side
    favorite_wins = record_wins(home_record if book == "home" else away_record)
    dog_wins = record_wins(away_record if book == "home" else home_record)
    if favorite_wins is None or dog_wins is None or favorite_wins >= dog_wins:
        return side
    return our


def choose_ats_pick(
    *,
    predicted_margin: float,
    home_handicap: float | None,
    p_home_cover: float | None,
    p_away_cover: float | None,
    home_team: str,
    away_team: str,
    raw_margin: float | None = None,
    market_margin: float | None = None,
    home_record: object = None,
    away_record: object = None,
) -> dict[str, Any]:
    """Name the team expected to win. The number can still be attached for context."""
    side = moneyline_record_side(
        blended_margin=predicted_margin,
        raw_margin=raw_margin,
        market_margin=market_margin,
        home_record=home_record,
        away_record=away_record,
    )
    team = home_team if side == "home" else away_team
    chance = p_home_cover if side == "home" else p_away_cover
    line = None
    if home_handicap is not None:
        line = float(home_handicap) if side == "home" else -float(home_handicap)
    return {
        "favored_side": side,
        "pick_team": team,
        "pick_line": line,
        "pick_market": "straight_up",
        "p_cover": chance,
        "pick_label": f"{team} to win",
        "pick_basis": "record" if raw_margin is not None and side != ("home" if predicted_margin >= 0 else "away") else "blended",
    }


def _spread_basis(gap: float, league_id: str | None) -> str | None:
    """score = our side of a close number. number = the posted side of a big gap."""
    size = abs(gap)
    league = str(league_id or "").lower()
    if league == "nfl" and CLOSE_MIN_DISAGREEMENT <= size < SPREAD_MIN_DISAGREEMENT:
        return "score"
    if league == "fbs" and CLOSE_MIN_DISAGREEMENT <= size < FBS_CLOSE_MAX:
        return "score"
    if size >= SPREAD_MIN_DISAGREEMENT:
        return "number"
    return None


def record_wins(record: object) -> int | None:
    """Wins from a record like ``1-9``. A missing record stays missing."""
    if not isinstance(record, str) or "-" not in record:
        return None
    head = record.split("-", 1)[0].strip()
    if not head.isdigit():
        return None
    return int(head)


# A quarterback's first NFL start. The team on `side` is faded on the moneyline and the spread.
ROOKIE_QB_STARTS = (
    {"date": "2026-11-15", "home": "Las Vegas Raiders", "away": "Seattle Seahawks", "side": "home"},
    {"date": "2026-11-15", "home": "Arizona Cardinals", "away": "Los Angeles Rams", "side": "home"},
)


def rookie_qb_side(row: dict[str, Any]) -> str | None:
    """The side making a first NFL start, if this game has one."""
    marked = row.get("rookie_qb_start")
    if marked in {"home", "away"}:
        return str(marked)
    kickoff = str(row.get("kickoff_utc") or "")[:10]
    home = str(row.get("home_team") or "")
    away = str(row.get("away_team") or "")
    for debut in ROOKIE_QB_STARTS:
        if debut["date"] == kickoff and debut["home"] == home and debut["away"] == away:
            return str(debut["side"])
    return None


def apply_rookie_qb_fade(row: dict[str, Any]) -> dict[str, Any]:
    """Take the opponent when a rookie quarterback is making his first start."""
    debut = rookie_qb_side(row)
    if debut not in {"home", "away"}:
        return row
    opponent = "away" if debut == "home" else "home"
    home = str(row.get("home_team") or "Home")
    away = str(row.get("away_team") or "Away")
    handicap = _finite(row.get("home_handicap"))
    if row.get("pick_market") != "withheld" and row.get("favored_side") == debut:
        row["favored_side"] = opponent
        row["pick_team"] = home if opponent == "home" else away
        row["pick_label"] = f"{row['pick_team']} to win"
        row["pick_line"] = None if handicap is None else (handicap if opponent == "home" else -handicap)
        row["pick_basis"] = "rookie_qb"
    if handicap is None or row.get("ats_side") == opponent:
        return row
    team = home if opponent == "home" else away
    line = handicap if opponent == "home" else -handicap
    row["ats_side"] = opponent
    row["ats_team"] = team
    row["ats_line"] = line
    row["ats_pick_label"] = f"{team} {format_spread_line(line)}"
    row["ats_basis"] = "rookie_qb"
    row["ats_locked"] = False
    return row


SHORT_NUMBER = 3.5


def apply_short_favorite(row: dict[str, Any]) -> dict[str, Any]:
    """A dog on a number of 3.5 or less lays the points with the favorite instead."""
    if row.get("ats_basis") == "rookie_qb":
        return row
    handicap = _finite(row.get("home_handicap"))
    line = _finite(row.get("ats_line"))
    side = row.get("ats_side")
    if handicap is None or abs(handicap) > SHORT_NUMBER or line is None or line < 0 or side not in {"home", "away"}:
        return row
    opponent = "away" if side == "home" else "home"
    home = str(row.get("home_team") or "Home")
    away = str(row.get("away_team") or "Away")
    team = home if opponent == "home" else away
    laid = handicap if opponent == "home" else -handicap
    row["ats_side"] = opponent
    row["ats_team"] = team
    row["ats_line"] = laid
    row["ats_pick_label"] = f"{team} {format_spread_line(laid)}"
    row["ats_basis"] = "short_number"
    row["ats_locked"] = False
    return row


def lays_points_with_fewer_wins(row: dict[str, Any]) -> bool:
    """The named side is laying points and has fewer wins than the opponent."""
    line = _finite(row.get("ats_line"))
    if line is None or line >= 0:
        return False
    team = row.get("ats_team")
    if team and team == row.get("home_team"):
        our_record, opp_record = row.get("home_record"), row.get("away_record")
    elif team and team == row.get("away_team"):
        our_record, opp_record = row.get("away_record"), row.get("home_record")
    else:
        return False
    our_wins = record_wins(our_record)
    opp_wins = record_wins(opp_record)
    return our_wins is not None and opp_wins is not None and our_wins < opp_wins


def choose_spread_pick(
    *,
    predicted_margin: float,
    home_handicap: float | None,
    margin_sigma: float | None,
    home_team: str,
    away_team: str,
    model_vs_market: float | None = None,
    league_id: str | None = None,
    home_record: str | None = None,
    away_record: str | None = None,
) -> dict[str, Any] | None:
    """Name a spread side from the raw disagreement with the number.

    Pass model_vs_market when the stored margin has already been pulled
    toward the number. The blended leftover is not the gap.
    """
    handicap = _finite(home_handicap)
    if handicap is None:
        return None
    gap = _finite(model_vs_market)
    if gap is None:
        margin = _finite(predicted_margin)
        if margin is None:
            return None
        gap = margin + handicap
    basis = _spread_basis(gap, league_id)
    if basis is None:
        return None
    side = "home" if gap > 0 else "away"
    if basis == "number":
        book_side = "away" if side == "home" else "home"
        book_line = handicap if book_side == "home" else -handicap
        book_team = home_team if book_side == "home" else away_team
        if lays_points_with_fewer_wins(
            {
                "ats_line": book_line,
                "ats_team": book_team,
                "home_team": home_team,
                "away_team": away_team,
                "home_record": home_record,
                "away_record": away_record,
            }
        ):
            basis = "record"
        else:
            side = book_side
    line = handicap if side == "home" else -handicap
    team = home_team if side == "home" else away_team
    sigma = _finite(margin_sigma) or 13.0
    raw_margin = gap - handicap
    probs = normal_cover_probs(raw_margin, handicap, sigma)
    return {
        "ats_side": side,
        "ats_team": team,
        "ats_line": line,
        "ats_pick_label": f"{team} {format_spread_line(line)}",
        "ats_cover_margin": abs(gap),
        "ats_basis": basis,
        "ats_probability": probs["p_home_cover"] if side == "home" else probs["p_away_cover"],
    }


def _apply_saved_moneyline_record(row: dict[str, Any]) -> None:
    """Flip a saved moneyline when the favorite has fewer wins and our guess likes the dog."""
    blended = _finite(row.get("predicted_margin"))
    market = _finite(row.get("market_home_margin"))
    gap = _finite(row.get("model_vs_market"))
    if blended is None or row.get("pick_market") == "withheld":
        return
    raw = None if market is None or gap is None else gap + market
    side = moneyline_record_side(
        blended_margin=blended,
        raw_margin=raw,
        market_margin=market,
        home_record=row.get("home_record"),
        away_record=row.get("away_record"),
    )
    if side == row.get("favored_side"):
        return
    home = str(row.get("home_team") or "Home")
    away = str(row.get("away_team") or "Away")
    handicap = _finite(row.get("home_handicap"))
    row["favored_side"] = side
    row["pick_team"] = home if side == "home" else away
    row["pick_label"] = f"{row['pick_team']} to win"
    row["pick_line"] = None if handicap is None else (handicap if side == "home" else -handicap)
    row["pick_basis"] = "record"


def apply_recorded_result(
    row: dict[str, Any],
    *,
    status: str | None,
    home_score: int | None,
    away_score: int | None,
) -> dict[str, Any]:
    """Grade both calls once the game has a recorded score."""
    _apply_saved_moneyline_record(row)
    apply_rookie_qb_fade(row)
    if status and home_score is not None and away_score is not None and status.lower() in {"final", "in_progress"}:
        row["game_status"] = status.lower()
        row["home_score"] = int(home_score)
        row["away_score"] = int(away_score)
        if row.get("pick_market") != "withheld" and row.get("favored_side") in {"home", "away"}:
            row["pick_result"] = grade_displayed_pick(
                status=row["game_status"],
                home_score=row["home_score"],
                away_score=row["away_score"],
                pick_market="straight_up",
                favored_side=row.get("favored_side"),
                home_handicap=row.get("home_handicap"),
            )
    return attach_spread_pick(row)


def _game_has_started(row: dict[str, Any]) -> bool:
    return str(row.get("game_status") or "").lower() in {"final", "in_progress"}


def _named_spread(row: dict[str, Any]) -> bool:
    return row.get("ats_side") in {"home", "away"} and bool(row.get("ats_pick_label"))


def _clear_spread_pick(row: dict[str, Any]) -> None:
    for key in (
        "ats_side",
        "ats_team",
        "ats_line",
        "ats_pick_label",
        "ats_cover_margin",
        "ats_probability",
        "ats_basis",
        "ats_pick_result",
        "ats_locked",
    ):
        row[key] = None


def attach_spread_pick(row: dict[str, Any]) -> dict[str, Any]:
    """Add the cover side to a board row. The winner pick stays as it is."""
    apply_rookie_qb_fade(row)
    apply_short_favorite(row)
    if row.get("ats_basis") in {"rookie_qb", "short_number"} and _named_spread(row):
        if _game_has_started(row):
            row["ats_locked"] = True
        row["ats_pick_result"] = grade_displayed_pick(
            status=row.get("game_status"),
            home_score=row.get("home_score"),
            away_score=row.get("away_score"),
            pick_market="spread",
            favored_side=row.get("ats_side"),
            home_handicap=row.get("home_handicap"),
        )
        return row
    if _game_has_started(row) and (_named_spread(row) or row.get("ats_locked") is True) and not (
        row.get("ats_basis") == "number" and lays_points_with_fewer_wins(row)
    ):
        row["ats_locked"] = True
        row["ats_pick_result"] = grade_displayed_pick(
            status=row.get("game_status"),
            home_score=row.get("home_score"),
            away_score=row.get("away_score"),
            pick_market="spread",
            favored_side=row.get("ats_side"),
            home_handicap=row.get("home_handicap"),
        )
        return row
    if row.get("projection_withheld") or row.get("pick_market") == "withheld":
        row["ats_side"] = None
        row["ats_team"] = None
        row["ats_line"] = None
        row["ats_pick_label"] = None
        row["ats_cover_margin"] = None
        row["ats_probability"] = None
        row["ats_basis"] = None
        row["ats_pick_result"] = None
        return row
    pick = choose_spread_pick(
        predicted_margin=row.get("predicted_margin"),
        home_handicap=row.get("home_handicap"),
        margin_sigma=row.get("margin_sigma"),
        home_team=str(row.get("home_team") or "Home"),
        away_team=str(row.get("away_team") or "Away"),
        model_vs_market=row.get("model_vs_market"),
        league_id=row.get("league_id") or row.get("league"),
        home_record=row.get("home_record"),
        away_record=row.get("away_record"),
    )
    if pick is None:
        row["ats_side"] = None
        row["ats_team"] = None
        row["ats_line"] = None
        row["ats_pick_label"] = None
        row["ats_cover_margin"] = None
        row["ats_probability"] = None
        row["ats_basis"] = None
        row["ats_pick_result"] = None
        return row
    row.update(pick)
    if _game_has_started(row):
        row["ats_locked"] = True
    row["ats_pick_result"] = grade_displayed_pick(
        status=row.get("game_status"),
        home_score=row.get("home_score"),
        away_score=row.get("away_score"),
        pick_market="spread",
        favored_side=pick["ats_side"],
        home_handicap=row.get("home_handicap"),
    )
    return row


def select_ats_board_frame(frame: pd.DataFrame, now: datetime) -> pd.DataFrame:
    """Upcoming games, plus ones that already kicked off inside the board window.

    A final stays on the card long enough to show whether that team won.
    Older finals stay off so the card is not the whole season.
    """
    if frame.empty:
        return frame
    now_ts = pd.Timestamp(now)
    if now_ts.tzinfo is None:
        now_ts = now_ts.tz_localize("UTC")
    else:
        now_ts = now_ts.tz_convert("UTC")
    kick = pd.to_datetime(frame["kickoff_utc"], utc=True)
    status = frame["status"].fillna("").astype(str).str.lower()
    future = kick > now_ts
    upcoming = frame.loc[future & status.isin(["scheduled", "preview", "pregame"])].copy()
    if "data_tier" in frame.columns:
        extra = frame.loc[future & (frame["data_tier"] == "upcoming")]
        upcoming = pd.concat([upcoming, extra], ignore_index=True).drop_duplicates(subset=["game_id"])
    if "margin" in upcoming.columns and not upcoming.empty:
        upcoming = upcoming[upcoming["margin"].isna() | (upcoming["status"].astype(str).str.lower() != "final")]
    recent = (kick > now_ts - pd.Timedelta(BOARD_RESULT_LOOKBACK)) & ~future
    started = frame.loc[recent & ~status.isin(["canceled", "cancelled", "postponed"])].copy()
    board = pd.concat([upcoming, started], ignore_index=True).drop_duplicates(subset=["game_id"])
    if board.empty:
        board = frame.loc[
            future & (kick < now_ts + pd.Timedelta(days=21)) & (status != "final")
        ].copy()
    return board


def grade_displayed_pick(
    *,
    status: str | None,
    home_score: float | None,
    away_score: float | None,
    pick_market: str | None,
    favored_side: str | None,
    home_handicap: float | None,
) -> str | None:
    """correct | miss | push once the game is final. Open games stay ungraded."""
    if str(status or "").lower() != "final":
        return None
    if home_score is None or away_score is None or favored_side not in {"home", "away"}:
        return None
    margin = float(home_score) - float(away_score)
    if pick_market == "spread" and home_handicap is not None:
        result = grade_side(margin, float(home_handicap), favored_side)
        return {"cover": "correct", "loss": "miss", "push": "push"}[result]
    if abs(margin) < 1e-9:
        return "push"
    winner = "home" if margin > 0 else "away"
    return "correct" if winner == favored_side else "miss"


def _apply_live_spreads(upcoming: pd.DataFrame, live_lines: pd.DataFrame, now: datetime) -> pd.DataFrame:
    """Prefer a live book number. After kickoff, keep the closing number if the live quote is gone."""
    if live_lines.empty:
        return upcoming
    closing_cols = [
        col
        for col in ("spread_line", "home_spread_price", "away_spread_price", "source")
        if col in upcoming.columns
    ]
    closing = upcoming[["game_id", *closing_cols]].copy() if closing_cols else None
    upcoming = upcoming.drop(
        columns=["spread_line", "home_spread_price", "away_spread_price", "source", "bookmaker_id"],
        errors="ignore",
    )
    upcoming = upcoming.merge(live_lines, on="game_id", how="left")
    if closing is None:
        return upcoming
    closing = closing.rename(columns={col: f"{col}_close" for col in closing_cols})
    upcoming = upcoming.merge(closing, on="game_id", how="left")
    kick = pd.to_datetime(upcoming["kickoff_utc"], utc=True)
    now_ts = pd.Timestamp(now)
    if now_ts.tzinfo is None:
        now_ts = now_ts.tz_localize("UTC")
    else:
        now_ts = now_ts.tz_convert("UTC")
    kicked = kick <= now_ts
    numeric = {"spread_line", "home_spread_price", "away_spread_price"}
    for col in closing_cols:
        close_col = f"{col}_close"
        if col in numeric:
            live_vals = pd.to_numeric(upcoming[col], errors="coerce")
            close_vals = pd.to_numeric(upcoming[close_col], errors="coerce")
            upcoming[col] = live_vals.where(~(kicked & live_vals.isna()), close_vals)
        else:
            live_vals = upcoming[col]
            missing = live_vals.isna() | live_vals.astype(str).str.lower().isin(["", "nan", "none"])
            upcoming[col] = live_vals.where(~(kicked & missing), upcoming[close_col])
    return upcoming.drop(columns=[f"{col}_close" for col in closing_cols])


def predict_upcoming_ats(
    session: Session,
    *,
    league_id: str | None = None,
    now: datetime | None = None,
    include_simulation: bool = False,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    leagues = [league_id] if league_id else ["nfl", "fbs"]
    rows: list[dict[str, Any]] = []
    summaries: dict[str, Any] = {}
    bundles: dict[str, Any] = {}
    for lg in leagues:
        frame = build_team_strength_frame(session, lg)
        if frame.empty:
            summaries[lg] = {"status": "no_features", "n": 0}
            continue
        frame["kickoff_utc"] = pd.to_datetime(frame["kickoff_utc"], utc=True)
        upcoming = select_ats_board_frame(frame, now)

        art, bundle = load_ats_research_bundle(lg)
        bundles[lg] = bundle.to_dict()
        if art is None:
            # Ad-hoc ridge only when production margin missing — cover probs stay suppressed.
            labeled = frame.dropna(subset=["margin"])
            labeled = labeled[labeled["kickoff_utc"] < pd.Timestamp(now)]
            if len(labeled) < 100:
                summaries[lg] = {"status": "no_model", "n": 0, "research_bundle": bundle.to_dict()}
                continue
            art = fit_ridge_margin(labeled, league_id=lg)
            art.model_version_id = f"ats-ridge-{lg}-adhoc:{CLEAN_PROSPECTIVE_COHORT}"
            art.platt_a, art.platt_b = 0.0, 1.0
            bundle.cover_probs_enabled = False
            bundle.calibrator_status = "missing"
            bundle.notes.append("Ad-hoc margin fit; cover probabilities suppressed without research calibrator")
            bundles[lg] = bundle.to_dict()

        live_lines = _live_spread_lines(session, lg)
        upcoming = _apply_live_spreads(upcoming, live_lines, now)

        scored = score_predictions(art, upcoming, apply_calibration=bundle.cover_probs_enabled)
        team_names = _team_names(session, lg)
        records = _live_records(session, lg, now)
        cover_enabled = bool(bundle.cover_probs_enabled)
        from spe.models.ats.close_check import apply_close_look, load_close_look

        close_look = load_close_look(session, lg, now)

        for row in scored.itertuples(index=False):
            spread = getattr(row, "spread_line", None)
            spread_f = None if spread is None or (isinstance(spread, float) and np.isnan(spread)) else float(spread)
            handicap = getattr(row, "home_handicap", None)
            handicap_f = (
                None
                if handicap is None or (isinstance(handicap, float) and np.isnan(handicap))
                else float(handicap)
            )
            market_margin = getattr(row, "market_home_margin", None)
            market_margin_f = (
                None
                if market_margin is None or (isinstance(market_margin, float) and np.isnan(market_margin))
                else float(market_margin)
            )
            source_kind = getattr(row, "spread_source_kind", None)
            home_price = getattr(row, "home_spread_price", None)
            away_price = getattr(row, "away_spread_price", None)
            book = getattr(row, "bookmaker_id", None)
            if book is None or (isinstance(book, float) and np.isnan(book)):
                src = getattr(row, "source", None)
                if src is not None and not (isinstance(src, float) and np.isnan(src)):
                    book = str(src)
                elif spread_f is not None and lg == "nfl":
                    book = "nflverse_consensus"
                else:
                    book = None
            warnings = _warnings(row)
            is_consensus = _is_consensus_source(book)
            if is_consensus and "consensus_not_executable" not in warnings:
                warnings.append("consensus_not_executable")
            if is_consensus and "quote_not_live_sportsbook" not in warnings:
                warnings.append("quote_not_live_sportsbook")

            home_id = str(row.home_team_id)
            away_id = str(row.away_team_id)
            home_name = team_names.get(home_id, home_id)
            away_name = team_names.get(away_id, away_id)
            raw_margin = float(row.predicted_margin)
            blended_margin = blend_margin_toward_market(raw_margin, market_margin_f)
            total_points = float(row.predicted_home_score) + float(row.predicted_away_score)
            blended_home = (total_points + blended_margin) / 2.0
            blended_away = (total_points - blended_margin) / 2.0
            win = normal_cover_probs(blended_margin, 0.0, float(row.margin_sigma))
            win_home = float(win["p_home_cover"])
            win_away = float(win["p_away_cover"])
            pick = choose_ats_pick(
                predicted_margin=blended_margin,
                home_handicap=handicap_f,
                p_home_cover=win_home,
                p_away_cover=win_away,
                home_team=home_name,
                away_team=away_name,
                raw_margin=raw_margin,
                market_margin=market_margin_f,
                home_record=records.get(home_id),
                away_record=records.get(away_id),
            )
            side = pick["favored_side"]
            side_line = pick["pick_line"]
            on_spread = pick["pick_market"] == "spread"
            uses_nfl_injuries = lg == "nfl" and "home_qb_out" in set(art.feature_cols)
            p_home = win_home
            p_away = win_away
            p_push = float(win["p_push"])
            p_cover = pick["p_cover"]
            cover_kind = "calibrated" if (on_spread and cover_enabled) else ("model" if on_spread else "straight_up")
            price = None
            research_ev = None
            breakeven = None
            actionable_ev = None
            sim = None
            if on_spread:
                raw_price = home_price if side == "home" else away_price
                price = None if raw_price is None or (isinstance(raw_price, float) and np.isnan(raw_price)) else int(raw_price)
                if include_simulation and handicap_f is not None:
                    sim = simulate_margins(
                        float(row.predicted_margin),
                        float(row.margin_sigma),
                        home_spread_line=handicap_f,
                        draw_count=3000,
                        seed=abs(hash(str(row.game_id))) % (2**32),
                    )
                # Dollar value stays on the calibrated number. The side is still a pick either way.
                if cover_enabled and price is not None and p_cover is not None:
                    research_ev = float(ev_per_dollar(p_cover, price))
                    breakeven = float(american_to_implied(price))
                    if not is_consensus:
                        actionable_ev = research_ev
            mvm = getattr(row, "model_vs_market", None)
            if mvm is None or (isinstance(mvm, float) and np.isnan(mvm)):
                mvm = None if market_margin_f is None else float(row.predicted_margin - market_margin_f)
            else:
                mvm = float(mvm)

            game_status = str(getattr(row, "status", "") or "").lower() or "scheduled"
            home_score = _score_int(getattr(row, "home_score", None))
            away_score = _score_int(getattr(row, "away_score", None))
            pick_result = grade_displayed_pick(
                status=game_status,
                home_score=home_score,
                away_score=away_score,
                pick_market=pick["pick_market"],
                favored_side=side,
                home_handicap=handicap_f if on_spread else None,
            )
            quote_class = "consensus" if is_consensus else ("live_book" if book else "none")
            # Clean prospective = live book + valid cover probs; consensus stays research-only category.
            eligibility = "clean_prospective" if (cover_enabled and quote_class == "live_book" and p_cover is not None) else (
                "consensus_research" if (cover_enabled and quote_class == "consensus" and p_cover is not None) else "ineligible"
            )
            home_prior = _finite(getattr(row, "home_games_prior", None))
            away_prior = _finite(getattr(row, "away_games_prior", None))
            projection_withheld = (
                home_prior is not None
                and away_prior is not None
                and withhold_for_history_gap(home_prior, away_prior)
            )
            if projection_withheld:
                warnings.append("uneven_history")
                pick = {
                    **pick,
                    "pick_label": "No projection",
                    "pick_team": None,
                    "pick_line": None,
                    "pick_market": "withheld",
                    "p_cover": None,
                }
                p_home = None
                p_away = None
                p_cover = None
                p_push = None
                on_spread = False
                pick_result = None
                eligibility = "ineligible"

            rows.append(
                {
                    "game_id": str(row.game_id),
                    "league_id": lg,
                    "kickoff_utc": pd.Timestamp(row.kickoff_utc).isoformat(),
                    "game_status": game_status,
                    "home_score": home_score,
                    "away_score": away_score,
                    "pick_result": pick_result,
                    "home_team_id": home_id,
                    "away_team_id": away_id,
                    "home_team": home_name,
                    "away_team": away_name,
                    "home_record": records.get(home_id),
                    "away_record": records.get(away_id),
                    "sportsbook": book,
                    "quote_source_class": quote_class,
                    "spread_line": spread_f,
                    "home_handicap": handicap_f,
                    "market_home_margin": market_margin_f,
                    "spread_source_kind": source_kind,
                    "home_spread_price": None if home_price is None or (isinstance(home_price, float) and np.isnan(home_price)) else int(home_price),
                    "away_spread_price": None if away_price is None or (isinstance(away_price, float) and np.isnan(away_price)) else int(away_price),
                    "predicted_margin": blended_margin,
                    "predicted_home_score": blended_home,
                    "predicted_away_score": blended_away,
                    "p_home_cover": p_home,
                    "p_away_cover": p_away,
                    "p_push": p_push,
                    "margin_sigma": float(row.margin_sigma),
                    "model_vs_market": mvm,
                    "favored_side": side,
                    "favored_side_line": side_line,
                    "pick_label": pick["pick_label"],
                    "pick_team": pick["pick_team"],
                    "pick_line": pick["pick_line"],
                    "pick_market": pick["pick_market"],
                    "pick_probability": (
                        FBS_GRADED_COVER_RATE
                        if lg == "fbs" and on_spread
                        else NFL_GRADED_COVER_RATE
                        if uses_nfl_injuries and on_spread
                        else pick["p_cover"]
                    ),
                    "pick_chance_label": (
                        FBS_GRADED_COVER_LABEL
                        if lg == "fbs" and on_spread
                        else NFL_GRADED_COVER_LABEL
                        if uses_nfl_injuries and on_spread
                        else None
                    ),
                    "pick_edge_points": None if mvm is None or not on_spread else float(mvm if side == "home" else -mvm),
                    "cover_prob_kind": cover_kind,
                    "p_cover": p_cover,
                    "price_american": price,
                    "break_even": breakeven,
                    "expected_value": actionable_ev,
                    "research_ev_vs_consensus_price": research_ev if is_consensus else None,
                    "confidence": float(np.clip(row.sample_size / 12.0, 0.15, 0.95)),
                    "sample_size": float(row.sample_size),
                    "projection_withheld": projection_withheld,
                    "warnings": warnings,
                    "model_version_id": art.model_version_id,
                    "pipeline_version": PIPELINE_VERSION,
                    "cohort_version": CLEAN_PROSPECTIVE_COHORT,
                    "eligibility": eligibility,
                    "cover_probs_enabled": cover_enabled,
                    "research_bundle": {
                        "calibrator_status": bundle.calibrator_status,
                        "margin_sha256": bundle.margin_artifact_sha256,
                        "calibrator_sha256": bundle.calibrator_sha256,
                        "spread_contract": bundle.spread_contract,
                        "training_cutoff_utc": bundle.training_cutoff_utc,
                    },
                    "research_only": True,
                    "play_enabled": False,
                    "data_tier": "research",
                    "simulation": sim,
                    "factors": {
                        "epa_edge": float(row.epa_edge),
                        "success_edge": float(row.success_edge),
                        "ppg_edge": float(row.ppg_edge),
                        "home_field": float(row.home_field),
                        "rest_edge": float(row.rest_edge),
                        "weather_wind_mps": float(row.weather_wind_mps),
                    },
                }
            )
            attach_spread_pick(rows[-1])
            if not _game_has_started(rows[-1]):
                apply_close_look(rows[-1], close_look)
        summaries[lg] = {
            "status": "ok",
            "n": int(len(upcoming)),
            "model_version_id": art.model_version_id,
            "cover_probs_enabled": cover_enabled,
            "calibrator_status": bundle.calibrator_status,
            "research_bundle": bundle.to_dict(),
        }
    rows = _add_open_slate(session, rows, leagues, now, summaries)
    rows.sort(key=lambda r: r["kickoff_utc"])
    return {
        "as_of": now.isoformat(),
        "research_only": True,
        "play_enabled": False,
        "pipeline_version": PIPELINE_VERSION,
        "cohort_version": CLEAN_PROSPECTIVE_COHORT,
        "disclaimer": (
            "Two calls per game. One is who we think wins. The other is the side of the number. "
            "Not a guarantee and not a sportsbook."
        ),
        "superseded_claims": [SUPERSEDED_CLAIM],
        "research_bundles": bundles,
        "summaries": summaries,
        "rows": rows,
    }


def _add_open_slate(
    session: Session,
    rows: list[dict[str, Any]],
    leagues: list[str],
    now: datetime,
    summaries: dict[str, Any],
) -> list[dict[str, Any]]:
    """Put today's games on the card when the play model has no row for them.

    The strength frame is empty when play-by-play is missing, and it drops a
    club whose history is stored under a different id. Finished games from
    today stay on the card so the close can be graded.
    """
    from zoneinfo import ZoneInfo

    from spe.db.base import as_utc
    from spe.db.models import Game

    seen = {row.get("game_id") for row in rows}
    horizon = now + timedelta(hours=36)
    day_start = now.astimezone(ZoneInfo("America/Chicago")).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    day_end = day_start + timedelta(days=1)
    window_end = horizon if horizon > day_end else day_end
    open_status = {"scheduled", "pre", "upcoming", "preview", "pregame"}
    started_status = {"final", "in_progress"}
    for league_id in leagues:
        games = (
            session.query(Game)
            .filter(
                Game.league_id == league_id,
                Game.data_tier.in_(["upcoming", "historical"]),
                Game.kickoff_utc >= day_start,
                Game.kickoff_utc < window_end,
            )
            .all()
        )
        pending = []
        for game in games:
            kick = as_utc(game.kickoff_utc)
            status = str(game.status or "").lower()
            if game.game_id in seen or kick is None:
                continue
            if status in started_status and day_start <= kick < day_end:
                pending.append(game)
                continue
            if kick <= now or kick > horizon or status not in open_status:
                continue
            pending.append(game)
        if not pending:
            continue
        lines = _live_spread_lines(session, league_id)
        by_game = {}
        if not lines.empty:
            by_game = {str(row.game_id): row for row in lines.itertuples(index=False)}
        names = _team_names(session, league_id)
        finals = _final_scores(session, league_id, now.year)
        strength_cache: dict[str, tuple[dict[str, str], dict[str, float], dict[str, int]]] = {}
        added = 0
        for game in pending:
            quote = by_game.get(game.game_id)
            handicap = None if quote is None else _finite(getattr(quote, "spread_line", None))
            home_price = None if quote is None else _score_int(getattr(quote, "home_spread_price", None))
            away_price = None if quote is None else _score_int(getattr(quote, "away_spread_price", None))
            book = None if quote is None else str(getattr(quote, "bookmaker_id", "") or "") or None
            kick = as_utc(game.kickoff_utc)
            if kick is None:
                continue
            cached = strength_cache.get(kick.isoformat())
            if cached is None:
                cached = _strength_before(finals, kick)
                strength_cache[kick.isoformat()] = cached
            records, ratings, counts = cached
            home_games = counts.get(game.home_team_id, 0)
            away_games = counts.get(game.away_team_id, 0)
            margin = ratings.get(game.home_team_id, 0.0) - ratings.get(game.away_team_id, 0.0)
            margin += 0.0 if game.neutral_site else SLATE_HOME_FIELD
            market_margin = None if handicap is None else -handicap
            home_name = names.get(game.home_team_id, game.home_team_id)
            away_name = names.get(game.away_team_id, game.away_team_id)
            total = 52.0 if league_id == "fbs" else 44.0
            win = normal_cover_probs(margin, 0.0, 14.0)
            pick = choose_ats_pick(
                predicted_margin=margin,
                home_handicap=handicap,
                p_home_cover=float(win["p_home_cover"]),
                p_away_cover=float(win["p_away_cover"]),
                home_team=home_name,
                away_team=away_name,
                raw_margin=margin,
                market_margin=market_margin,
                home_record=records.get(game.home_team_id),
                away_record=records.get(game.away_team_id),
            )
            row = {
                "game_id": game.game_id,
                "league_id": league_id,
                "kickoff_utc": kick.isoformat(),
                "game_status": str(game.status or "scheduled").lower(),
                "home_score": _score_int(game.home_score),
                "away_score": _score_int(game.away_score),
                "pick_result": None,
                "home_team_id": game.home_team_id,
                "away_team_id": game.away_team_id,
                "home_team": home_name,
                "away_team": away_name,
                "home_record": records.get(game.home_team_id),
                "away_record": records.get(game.away_team_id),
                "sportsbook": book,
                "quote_source_class": "live_book" if book else "none",
                "spread_line": handicap,
                "home_handicap": handicap,
                "market_home_margin": market_margin,
                "spread_source_kind": "sportsbook_handicap" if handicap is not None else None,
                "home_spread_price": home_price,
                "away_spread_price": away_price,
                "predicted_margin": margin,
                "predicted_home_score": (total + margin) / 2.0,
                "predicted_away_score": (total - margin) / 2.0,
                "p_home_cover": float(win["p_home_cover"]),
                "p_away_cover": float(win["p_away_cover"]),
                "p_push": float(win["p_push"]),
                "margin_sigma": 14.0,
                "model_vs_market": None if handicap is None else margin + handicap,
                "favored_side": pick["favored_side"],
                "favored_side_line": pick["pick_line"],
                "pick_label": pick["pick_label"],
                "pick_team": pick["pick_team"],
                "pick_line": pick["pick_line"],
                "pick_market": pick["pick_market"],
                "pick_probability": pick["p_cover"],
                "pick_chance_label": None,
                "pick_edge_points": None if handicap is None else margin + handicap,
                "cover_prob_kind": "straight_up",
                "p_cover": pick["p_cover"],
                "price_american": None,
                "break_even": None,
                "expected_value": None,
                "confidence": 0.35,
                "sample_size": float(min(home_games, away_games)),
                "projection_withheld": False,
                "warnings": ["slate_without_play_model", "opponent_adjusted", "research_unvalidated"],
                "model_version_id": f"ats-slate-sos-{league_id}",
                "pipeline_version": PIPELINE_VERSION,
                "cohort_version": CLEAN_PROSPECTIVE_COHORT,
                "eligibility": "ineligible",
                "cover_probs_enabled": False,
                "research_only": True,
                "play_enabled": False,
                "data_tier": "research",
                "factors": {},
            }
            row["pick_result"] = grade_displayed_pick(
                status=row["game_status"],
                home_score=row["home_score"],
                away_score=row["away_score"],
                pick_market="straight_up",
                favored_side=row["favored_side"],
                home_handicap=row["home_handicap"],
            )
            if home_games >= SLATE_MIN_GAMES and away_games >= SLATE_MIN_GAMES:
                attach_spread_pick(row)
            if row.get("ats_basis") != "score":
                _clear_spread_pick(row)
            rows.append(row)
            seen.add(game.game_id)
            added += 1
        if added:
            summaries[league_id] = {
                **summaries.get(league_id, {}),
                "status": "ok",
                "slate_rows": added,
                "n": int(summaries.get(league_id, {}).get("n") or 0) + added,
            }
    return rows


def _final_scores(session: Session, league_id: str, year: int) -> list[tuple[Any, ...]]:
    from sqlalchemy import text

    return list(
        session.execute(
            text(
                """
                SELECT game_id, kickoff_utc, home_team_id, away_team_id,
                       home_score, away_score, neutral_site
                FROM games
                WHERE league_id = :lg
                  AND status = 'final'
                  AND home_score IS NOT NULL
                  AND away_score IS NOT NULL
                  AND season_id LIKE :season
                """
            ),
            {"lg": league_id, "season": f"{league_id}-{year}%"},
        ).fetchall()
    )


def _adjusted_ratings(played: list[tuple[str, str, float, bool]]) -> dict[str, float]:
    """Team strength from pre-kickoff margins, shrunk toward the league average.

    Beating a team that itself loses to everyone counts for less than beating
    a team that wins. One blowout cannot produce a 50-point rating.
    """
    teams = sorted({team for home, away, _margin, _neutral in played for team in (home, away)})
    if not teams:
        return {}
    index = {team: i for i, team in enumerate(teams)}
    design = np.zeros((len(played), len(teams)))
    target = np.zeros(len(played))
    for i, (home, away, margin, neutral) in enumerate(played):
        design[i, index[home]] = 1.0
        design[i, index[away]] = -1.0
        target[i] = margin - (0.0 if neutral else SLATE_HOME_FIELD)
    normal = design.T @ design
    normal.flat[:: len(teams) + 1] += SLATE_RIDGE
    coef = np.linalg.solve(normal, design.T @ target)
    return {team: float(coef[index[team]]) for team in teams}


def _strength_before(
    finals: list[tuple[Any, ...]], kick: datetime
) -> tuple[dict[str, str], dict[str, float], dict[str, int]]:
    """Records, opponent-adjusted ratings, and game counts from before this kickoff."""
    from spe.db.base import as_utc

    wins: dict[str, int] = {}
    losses: dict[str, int] = {}
    count: dict[str, int] = {}
    played: list[tuple[str, str, float, bool]] = []
    for _game_id, when, home, away, home_score, away_score, neutral in finals:
        started = as_utc(when)
        if started is None or started >= kick:
            continue
        home_id, away_id = str(home), str(away)
        margin = float(home_score) - float(away_score)
        played.append((home_id, away_id, margin, bool(neutral)))
        count[home_id] = count.get(home_id, 0) + 1
        count[away_id] = count.get(away_id, 0) + 1
        if home_score > away_score:
            wins[home_id] = wins.get(home_id, 0) + 1
            losses[away_id] = losses.get(away_id, 0) + 1
        elif away_score > home_score:
            wins[away_id] = wins.get(away_id, 0) + 1
            losses[home_id] = losses.get(home_id, 0) + 1
    teams = set(wins) | set(losses) | set(count)
    records = {team_id: f"{wins.get(team_id, 0)}-{losses.get(team_id, 0)}" for team_id in teams}
    return records, _adjusted_ratings(played), count


def _live_spread_lines(session: Session, league_id: str) -> pd.DataFrame:
    from sqlalchemy import text

    raw = pd.read_sql(
        text(
            """
            SELECT o.game_id, o.side, o.line, t.price_american, t.bookmaker_id,
                   t.captured_at, g.kickoff_utc
            FROM outcomes o
            JOIN odds_ticks t ON t.outcome_id = o.outcome_id
            JOIN games g ON g.game_id = o.game_id
            WHERE o.market_id = 'point_spread'
              AND g.league_id = :lg
            ORDER BY t.captured_at DESC
            """
        ),
        session.connection(),
        params={"lg": league_id},
    )
    if raw.empty:
        return raw
    return _coherent_spread_quotes(raw)


def _pregame_quotes(group: pd.DataFrame) -> pd.DataFrame:
    """The last number posted before kickoff. An in-game line is not a pick.

    Duke at NC State moved from +4.5 to +35.5 after the game was already a
    blowout. Treating that later number as the bet made it the top call.
    """
    if "kickoff_utc" not in group.columns or group["kickoff_utc"].isna().all():
        return group
    kickoff = pd.to_datetime(group["kickoff_utc"], utc=True).dropna()
    if kickoff.empty:
        return group
    captured = pd.to_datetime(group["captured_at"], utc=True)
    return group.loc[captured < kickoff.iloc[0]]


def _coherent_spread_quotes(raw: pd.DataFrame) -> pd.DataFrame:
    """One home handicap per game. Home and away numbers are opposites.

    A shared school name used to store both +14 and -14 as home. Keep the pair
    that agrees. If both signs are labeled home and no away number exists, drop
    the game instead of guessing which school is the favorite.

    Once a game has kicked off, keep the last pregame pair. A number that
    appears only after kickoff is the live score talking, not a bet.
    """
    raw = raw.sort_values("captured_at", ascending=False)
    chosen: list[dict[str, Any]] = []
    for game_id, group in raw.groupby("game_id", sort=False):
        group = _pregame_quotes(group)
        if group.empty:
            continue
        home_rows = group[group["side"] == "home"]
        away_rows = group[group["side"] == "away"]
        paired_home = None
        paired_away = None
        for _, home_row in home_rows.iterrows():
            opposite = away_rows[np.isclose(away_rows["line"].astype(float), -float(home_row["line"]))]
            if not opposite.empty:
                paired_home = home_row
                paired_away = opposite.iloc[0]
                break
        if paired_home is None:
            if home_rows.empty:
                continue
            signs = {np.sign(float(value)) for value in home_rows["line"] if float(value) != 0.0}
            if len(signs) > 1:
                continue
            paired_home = home_rows.iloc[0]
        chosen.append(
            {
                "game_id": game_id,
                "spread_line": float(paired_home["line"]),
                "home_spread_price": int(paired_home["price_american"]),
                "away_spread_price": None if paired_away is None else int(paired_away["price_american"]),
                "bookmaker_id": paired_home["bookmaker_id"],
                "source": "odds_api",
            }
        )
    if not chosen:
        return pd.DataFrame(
            columns=["game_id", "spread_line", "home_spread_price", "away_spread_price", "bookmaker_id", "source"]
        )
    return pd.DataFrame(chosen)


def _team_names(session: Session, league_id: str) -> dict[str, str]:
    from spe.db.models import Team

    return {
        t.team_id: t.name
        for t in session.query(Team).filter(Team.league_id == league_id).all()
    }


def _live_records(session: Session, league_id: str, now: datetime) -> dict[str, str]:
    from sqlalchemy import text

    rows = session.execute(
        text(
            """
            SELECT home_team_id, away_team_id, home_score, away_score
            FROM games
            WHERE league_id = :lg
              AND status = 'final'
              AND home_score IS NOT NULL
              AND kickoff_utc < :now
              AND season_id LIKE :season
            """
        ),
        {
            "lg": league_id,
            "now": now.isoformat(),
            "season": f"{league_id}-{now.year}%",
        },
    ).fetchall()
    wins: dict[str, int] = {}
    losses: dict[str, int] = {}
    for home, away, hs, aws in rows:
        if hs > aws:
            wins[home] = wins.get(home, 0) + 1
            losses[away] = losses.get(away, 0) + 1
        elif aws > hs:
            wins[away] = wins.get(away, 0) + 1
            losses[home] = losses.get(home, 0) + 1
    teams = set(wins) | set(losses)
    return {tid: f"{wins.get(tid, 0)}-{losses.get(tid, 0)}" for tid in teams}


def _warnings(row: Any) -> list[str]:
    out: list[str] = []
    if float(getattr(row, "sample_size", 0)) < 3:
        out.append("thin_prior_sample")
    if float(getattr(row, "weather_available", 0)) < 0.5:
        out.append("weather_missing")
    spread = getattr(row, "spread_line", None)
    if spread is None or (isinstance(spread, float) and np.isnan(spread)):
        out.append("no_sportsbook_spread")
    out.append("research_unvalidated")
    return out


def _is_consensus_source(book: Any) -> bool:
    if book is None:
        return True
    if isinstance(book, float) and np.isnan(book):
        return True
    text = str(book).strip().lower()
    if not text:
        return True
    return text in {"consensus", "nflverse_consensus"} or "consensus" in text
