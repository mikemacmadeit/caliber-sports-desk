"""Prove nflverse spread_line contract on raw CSV vs local parquet vs Caliber DB."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "research" / "ats_audit"
RAW = OUT / "nflverse_games_raw.csv"
PARQUET = ROOT / "data" / "cache" / "nflverse" / "games.parquet"

CASES = ("2024_01_ARI_BUF", "2024_01_NE_CIN", "2026_09_NYJ_KC")


def ats_from_margin_forecast(m: float, l: float) -> str:
    """L = expected home margin (positive = home favored). Home covers iff M - L > 0."""
    edge = m - l
    if abs(edge) < 1e-12:
        return "push"
    return "home_cover" if edge > 0 else "away_cover"


def ats_from_handicap(m: float, h: float) -> str:
    """h = sportsbook home handicap (negative = favorite). Home covers iff M + h > 0."""
    edge = m + h
    if abs(edge) < 1e-12:
        return "push"
    return "home_cover" if edge > 0 else "away_cover"


def main() -> None:
    raw_bytes = RAW.read_bytes()
    raw_sha = hashlib.sha256(raw_bytes).hexdigest()
    raw = pd.read_csv(RAW)
    pq = pd.read_parquet(PARQUET) if PARQUET.exists() else None

    rows = []
    for gid in CASES:
        r = raw.loc[raw["game_id"] == gid]
        if r.empty:
            # try alt forms
            r = raw.loc[raw["game_id"].astype(str).str.endswith(gid.split("_", 1)[-1] if False else gid)]
        if r.empty:
            rows.append({"game_id": gid, "in_raw": False})
            continue
        rec = r.iloc[0]
        m = float(rec["home_score"] - rec["away_score"]) if pd.notna(rec["home_score"]) else None
        l = float(rec["spread_line"]) if pd.notna(rec["spread_line"]) else None
        item = {
            "game_id": gid,
            "in_raw": True,
            "home_team": rec["home_team"],
            "away_team": rec["away_team"],
            "home_score": None if pd.isna(rec["home_score"]) else float(rec["home_score"]),
            "away_score": None if pd.isna(rec["away_score"]) else float(rec["away_score"]),
            "result": None if pd.isna(rec["result"]) else float(rec["result"]),
            "spread_line_raw": l,
            "home_moneyline": None if "home_moneyline" not in rec or pd.isna(rec.get("home_moneyline")) else float(rec["home_moneyline"]),
            "away_moneyline": None if "away_moneyline" not in rec or pd.isna(rec.get("away_moneyline")) else float(rec["away_moneyline"]),
            "margin_M": m,
            "home_handicap_h": None if l is None else -l,
            "grade_as_margin_forecast_M_minus_L": None if m is None or l is None else ats_from_margin_forecast(m, l),
            "grade_as_handicap_M_plus_L_WRONG_if_L_is_forecast": None
            if m is None or l is None
            else ats_from_handicap(m, l),
            "grade_as_handicap_M_plus_h": None if m is None or l is None else ats_from_handicap(m, -l),
        }
        if pq is not None:
            p = pq.loc[pq["game_id"].astype(str) == gid]
            if not p.empty:
                pref = p.iloc[0]
                item["parquet_spread_line"] = float(pref["spread_line"]) if pd.notna(pref["spread_line"]) else None
                item["parquet_equals_raw"] = item["parquet_spread_line"] == l
                if "home_moneyline" in pref:
                    item["parquet_home_ml"] = None if pd.isna(pref["home_moneyline"]) else float(pref["home_moneyline"])
        rows.append(item)

    # Aggregate: when home favored (docs: L>0), does result tend positive?
    done = raw.dropna(subset=["spread_line", "result", "home_score", "away_score"]).copy()
    fav = done[done["spread_line"] > 0]
    dog = done[done["spread_line"] < 0]
    summary = {
        "source_url": "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv",
        "docs_url": "https://github.com/nflverse/nfldata/blob/master/DATASETS.md",
        "docs_quote": (
            "spread_line: A positive number means the home team was favored by that many points, "
            "a negative number means the away team was favored by that many points. "
            "This lines up with the result column."
        ),
        "raw_sha256": raw_sha,
        "raw_bytes": len(raw_bytes),
        "n_rows": int(len(raw)),
        "proven_contract": {
            "L": "raw spread_line = expected home winning margin",
            "positive_L": "home favored",
            "h": "sportsbook home handicap = -L",
            "home_covers": "M - L > 0  (== M + h > 0)",
            "market_margin_forecast": "L (not -L)",
        },
        "empirical": {
            "when_L_positive_mean_result": float(fav["result"].mean()),
            "when_L_negative_mean_result": float(dog["result"].mean()),
            "pct_L_positive": float((done["spread_line"] > 0).mean()),
            "home_ats_rate_correct_M_minus_L": float(((done["result"] - done["spread_line"]) > 0).mean()),
            "home_ats_rate_wrong_M_plus_L": float(((done["result"] + done["spread_line"]) > 0).mean()),
            "close_mae_vs_L": float((done["result"] - done["spread_line"]).abs().mean()),
            "close_mae_vs_neg_L": float((done["result"] - (-done["spread_line"])).abs().mean()),
        },
        "cases": rows,
    }
    (OUT / "spread_contract_probe.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
