import numpy as np
import pandas as pd

raw = pd.read_parquet("data/cache/nflverse/games.parquet")
raw = raw.dropna(subset=["spread_line", "home_score", "away_score"])
raw["margin"] = raw["home_score"] - raw["away_score"]
raw["mkt"] = -raw["spread_line"]
for y, part in raw.groupby("season"):
    if y < 2016:
        continue
    mae = float(np.mean(np.abs(part["margin"] - part["mkt"])))
    # Also ATS home cover rate
    edge = part["margin"] + part["spread_line"]
    cover = float((edge > 0).mean())
    push = float(np.isclose(edge, 0).mean())
    print(f"{y}: n={len(part)} closeMAE={mae:.2f} homeATSrate={cover:.3f} push={push:.3f} meanAbsLine={part['spread_line'].abs().mean():.2f}")
