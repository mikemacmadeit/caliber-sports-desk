import numpy as np
import pandas as pd

exp = pd.read_csv("data/research/ats_audit/nfl_holdout_export.csv")
exp = exp.dropna(subset=["spread_line", "margin"])
print("n", len(exp))
print("seasons", exp["kickoff_utc"].astype(str).str[:4].value_counts().sort_index())
mkt = -exp["spread_line"]
print("close MAE", float(np.mean(np.abs(exp["margin"] - mkt))))
print("model MAE", float(np.mean(np.abs(exp["margin"] - exp["predicted_margin"]))))
for y, part in exp.groupby(exp["kickoff_utc"].astype(str).str[:4]):
    mkt_y = -part["spread_line"]
    print(
        y,
        "n",
        len(part),
        "closeMAE",
        round(float(np.mean(np.abs(part["margin"] - mkt_y))), 2),
        "modelMAE",
        round(float(np.mean(np.abs(part["margin"] - part["predicted_margin"]))), 2),
        "mean_abs_spread",
        round(float(part["spread_line"].abs().mean()), 2),
    )

raw = pd.read_parquet("data/cache/nflverse/games.parquet")
raw = raw[raw["season"] >= 2024].dropna(subset=["spread_line", "home_score", "away_score"])
raw["margin"] = raw["home_score"] - raw["away_score"]
raw["mkt"] = -raw["spread_line"]
print("parquet 2024+ close MAE", float(np.mean(np.abs(raw["margin"] - raw["mkt"]))))
print(raw[["game_id", "spread_line", "home_score", "away_score", "result"]].head(3).to_string())

exp["gid_raw"] = exp["game_id"].str.replace("nfl:", "", regex=False)
merged = exp.merge(
    raw[["game_id", "spread_line", "home_score", "away_score"]].rename(
        columns={
            "game_id": "gid_raw",
            "spread_line": "raw_spread",
            "home_score": "rhs",
            "away_score": "raw_aws",
        }
    ),
    on="gid_raw",
    how="left",
)
print("spread match rate", float((merged["spread_line"] == merged["raw_spread"]).mean()))
print(
    "score match",
    float(((merged["home_score"] == merged["rhs"]) & (merged["away_score"] == merged["raw_aws"])).mean()),
)
# Is result column equal to margin?
print("result vs margin corr on parquet", float(np.corrcoef(raw["result"].fillna(raw["margin"]), raw["margin"])[0, 1]))
# Typical: abs error of close
print("close MAE by season parquet")
for y, part in raw.groupby("season"):
    print(y, round(float(np.mean(np.abs(part["margin"] - part["mkt"]))), 2), "n", len(part))
