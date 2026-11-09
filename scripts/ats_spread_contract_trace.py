"""Trace Agent-4 cases through raw → DB → probs → freeze/settle path."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "research" / "ats_audit"
RAW = OUT / "nflverse_games_raw.csv"
CASES = ["2024_01_ARI_BUF", "2024_01_NE_CIN", "2026_09_NYJ_KC"]


def main() -> None:
    import os
    import sys

    sys.path.insert(0, str(ROOT / "packages" / "core"))
    os.chdir(ROOT)

    from spe.db.models import AtsPrediction, AtsSettlement, ClosingLine, Game
    from spe.db.session import session_factory
    from spe.models.ats.labels import grade_side, normal_cover_probs
    from spe.models.ats.spread_contract import home_handicap_from_nflverse_L, market_home_margin_forecast

    raw = pd.read_csv(RAW)
    sha = hashlib.sha256(RAW.read_bytes()).hexdigest()
    traces = []

    with session_factory() as session:
        for gid_raw in CASES:
            game_id = f"nfl:{gid_raw}"
            src = raw.loc[raw["game_id"] == gid_raw]
            if src.empty:
                traces.append({"game_id": gid_raw, "error": "missing_in_raw"})
                continue
            row = src.iloc[0]
            L = float(row["spread_line"]) if pd.notna(row["spread_line"]) else None
            hs = float(row["home_score"]) if pd.notna(row["home_score"]) else None
            aws = float(row["away_score"]) if pd.notna(row["away_score"]) else None
            h = home_handicap_from_nflverse_L(L) if L is not None else None
            M = (hs - aws) if hs is not None and aws is not None else None

            game = session.get(Game, game_id)
            cl = (
                session.query(ClosingLine)
                .filter(
                    ClosingLine.game_id == game_id,
                    ClosingLine.market_id == "point_spread",
                    ClosingLine.source == "nflverse_consensus",
                )
                .one_or_none()
            )
            preds = session.query(AtsPrediction).filter(AtsPrediction.game_id == game_id).all()
            settlements = {
                s.ats_id: s
                for s in session.query(AtsSettlement)
                .filter(AtsSettlement.ats_id.in_([p.ats_id for p in preds] or ["__none__"]))
                .all()
            }

            grade = None
            if M is not None and h is not None:
                grade = grade_side(M, h, "home")

            # Example probability with a neutral mu=L (market-implied) for illustration only.
            demo_probs = None
            if L is not None and h is not None:
                demo_probs = normal_cover_probs(L, h, sigma=13.37)

            wrong_grade = None
            if M is not None and L is not None:
                edge_wrong = M + L
                if abs(edge_wrong) < 1e-12:
                    wrong_grade = "push"
                else:
                    wrong_grade = "cover" if edge_wrong > 0 else "loss"

            traces.append(
                {
                    "game_id_raw": gid_raw,
                    "game_id_db": game_id,
                    "raw_source": {
                        "spread_line_L": L,
                        "home_moneyline": None if pd.isna(row.get("home_moneyline")) else float(row["home_moneyline"]),
                        "away_moneyline": None if pd.isna(row.get("away_moneyline")) else float(row["away_moneyline"]),
                        "home_score": hs,
                        "away_score": aws,
                        "result": None if pd.isna(row.get("result")) else float(row["result"]),
                    },
                    "conversions": {
                        "home_handicap_h": h,
                        "market_home_margin_forecast": (
                            market_home_margin_forecast(L, source_kind="nflverse_margin_forecast")
                            if L is not None
                            else None
                        ),
                        "sign_flips_on_ingest": False,
                        "adapter_notes": "NflverseAdapter downloads games.parquet; ATS backfill stores raw L in closing_lines.home_line",
                    },
                    "database": {
                        "game_exists": game is not None,
                        "game_status": game.status if game else None,
                        "db_home_score": game.home_score if game else None,
                        "db_away_score": game.away_score if game else None,
                        "closing_home_line_L": cl.home_line if cl else None,
                        "closing_equals_raw_L": (cl is not None and L is not None and abs(float(cl.home_line) - L) < 1e-9),
                        "closing_source": cl.source if cl else None,
                    },
                    "probability_calculation": {
                        "convention": "normal_cover_probs(mu, h=-L, sigma)",
                        "demo_mu_equals_L": demo_probs,
                        "defect_if_pass_L_as_h": "Inverts favorite/dog cover thresholds",
                    },
                    "grading": {
                        "correct_home_result": grade,
                        "wrong_M_plus_L_home_result": wrong_grade,
                        "labels_disagree": grade != wrong_grade if grade and wrong_grade else None,
                    },
                    "freeze_settle": {
                        "n_predictions": len(preds),
                        "predictions": [
                            {
                                "ats_id": p.ats_id,
                                "side": p.side,
                                "spread_line": p.spread_line,
                                "p_cover": p.p_cover,
                                "bookmaker_id": p.bookmaker_id,
                                "spread_contract": (p.components or {}).get("spread_contract"),
                                "settlement": settlements.get(p.ats_id).result if settlements.get(p.ats_id) else None,
                            }
                            for p in preds
                        ],
                    },
                    "ui_api_note": (
                        "API/UI consume predict_upcoming_ats rows; after fix they expose home_handicap "
                        "and market_home_margin without flipping stored closing_lines."
                    ),
                }
            )

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "raw_sha256": sha,
        "traces": traces,
    }
    path = OUT / "spread_contract_e2e_trace.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"wrote": str(path), "cases": len(traces)}, indent=2))


if __name__ == "__main__":
    main()
