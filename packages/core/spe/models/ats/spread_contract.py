"""ATS spread-sign contract. Source semantics differ; do not conflate them.

nflverse / nfldata games.spread_line (consensus closing):
  L = expected home winning margin (aligns with result = home_score - away_score).
  Positive L ⇒ home favored.
  Sportsbook-style home handicap h = -L.
  Home covers iff M - L > 0  (equivalently M + h > 0).
  Market margin forecast = L (not -L).

The Odds API `spreads` market `point` on a team outcome:
  Already a sportsbook handicap for that team (negative ⇒ that team favored).
  Home-side point is h directly. Do NOT apply the nflverse L→h conversion.

Caliber DB:
  closing_lines.home_line for source='nflverse_consensus' stores raw L (unchanged).
  outcomes.line for market_id='point_spread' from odds_api stores sportsbook handicap.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    import pandas as pd

SourceKind = Literal["nflverse_margin_forecast", "sportsbook_handicap", "unknown"]

NFLVERSE_CONSENSUS_SOURCES = frozenset({"nflverse_consensus", "nflverse", "consensus"})
CURRENT_SPREAD_CONTRACT = "nflverse_L_v1"
LEGACY_SPREAD_CONTRACT = "pre_nflverse_L_fix"


def classify_spread_source(source: str | None) -> SourceKind:
    if source is None:
        return "unknown"
    text = str(source).strip().lower()
    if text in NFLVERSE_CONSENSUS_SOURCES or "nflverse" in text:
        return "nflverse_margin_forecast"
    if text in {"odds_api", "draftkings", "fanduel", "betmgm", "fanatics"} or not text.endswith(
        "consensus"
    ):
        # Named books / odds_api ticks are handicaps. Unknown non-consensus → unknown.
        if text in {"odds_api", "cfbd"} or text in {
            "draftkings",
            "fanduel",
            "betmgm",
            "fanatics",
            "caesars",
            "williamhill_us",
        }:
            return "sportsbook_handicap"
    if "consensus" in text:
        return "nflverse_margin_forecast"
    return "unknown"


def home_handicap_from_nflverse_L(spread_line_L: float) -> float:
    """Convert nflverse expected-home-margin L to sportsbook home handicap h."""
    return -float(spread_line_L)


def market_home_margin_forecast(spread_value: float, *, source_kind: SourceKind) -> float:
    """Expected home margin implied by the stored spread value under its source contract."""
    if source_kind == "nflverse_margin_forecast":
        return float(spread_value)  # L
    if source_kind == "sportsbook_handicap":
        return -float(spread_value)  # h negative ⇒ positive expected margin
    raise ValueError(f"Cannot derive market margin forecast from source_kind={source_kind}")


def to_home_handicap(spread_value: float, *, source_kind: SourceKind) -> float:
    if source_kind == "nflverse_margin_forecast":
        return home_handicap_from_nflverse_L(spread_value)
    if source_kind == "sportsbook_handicap":
        return float(spread_value)
    raise ValueError(f"Cannot convert unknown spread source_kind={source_kind}")


def grade_home_ats_from_scores(
    home_score: float,
    away_score: float,
    *,
    spread_value: float,
    source_kind: SourceKind,
) -> str:
    """Return home_cover|away_cover|push using the correct source contract."""
    m = float(home_score) - float(away_score)
    if source_kind == "nflverse_margin_forecast":
        edge = m - float(spread_value)
    elif source_kind == "sportsbook_handicap":
        edge = m + float(spread_value)
    else:
        raise ValueError(f"Cannot grade unknown source_kind={source_kind}")
    if abs(edge) < 1e-12:
        return "push"
    return "home_cover" if edge > 0 else "away_cover"


def infer_board_spread_kind(book: str | None, has_live_tick: bool = False) -> SourceKind:
    """Upcoming board rows: consensus books are nflverse L; live books are handicaps."""
    if has_live_tick and book and "consensus" not in str(book).lower():
        return "sportsbook_handicap"
    return classify_spread_source(book)


def contract_snapshot() -> dict[str, Any]:
    return {
        "nflverse_docs": (
            "Positive spread_line ⇒ home favored by that many points; aligns with result."
        ),
        "raw_source": "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv",
        "L": "expected home margin",
        "h": "-L sportsbook home handicap",
        "home_covers": "M - L > 0",
        "odds_api": "team point is already sportsbook handicap; do not negate",
        "db_nflverse_home_line": "stores raw L (no flip on ingest)",
    }


def attach_spread_fields(
    frame: pd.DataFrame,
    *,
    default_kind: SourceKind = "nflverse_margin_forecast",
    source_col: str = "source",
    spread_col: str = "spread_line",
) -> pd.DataFrame:
    """Add home_handicap + market_home_margin without mutating stored spread_line values."""
    import pandas as pd

    out = frame.copy()
    if spread_col not in out.columns:
        out["home_handicap"] = pd.NA
        out["market_home_margin"] = pd.NA
        out["spread_source_kind"] = default_kind
        return out

    kinds: list[str] = []
    handicaps: list[float | None] = []
    margins: list[float | None] = []
    has_source = source_col in out.columns
    for _, row in out.iterrows():
        spread = row.get(spread_col)
        if spread is None or (isinstance(spread, float) and pd.isna(spread)):
            kinds.append(default_kind)
            handicaps.append(None)
            margins.append(None)
            continue
        if has_source and row.get(source_col) is not None and not (
            isinstance(row.get(source_col), float) and pd.isna(row.get(source_col))
        ):
            kind = classify_spread_source(str(row.get(source_col)))
            if kind == "unknown":
                kind = default_kind
        else:
            kind = default_kind
        kinds.append(kind)
        try:
            handicaps.append(to_home_handicap(float(spread), source_kind=kind))
            margins.append(market_home_margin_forecast(float(spread), source_kind=kind))
        except ValueError:
            handicaps.append(None)
            margins.append(None)
    out["spread_source_kind"] = kinds
    out["home_handicap"] = handicaps
    out["market_home_margin"] = margins
    return out
