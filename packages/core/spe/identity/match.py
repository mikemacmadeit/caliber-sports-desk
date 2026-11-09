from __future__ import annotations

import re
from dataclasses import dataclass

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
_NOISE = re.compile(r"[^a-z0-9\s]")


def normalize_name(name: str) -> str:
    tokens = _NOISE.sub(" ", name.lower()).split()
    tokens = [t for t in tokens if t not in _SUFFIXES]
    return " ".join(tokens)


def _token_sort(name: str) -> str:
    return " ".join(sorted(normalize_name(name).split()))


def _bigrams(s: str) -> set[str]:
    padded = f" {s} "
    return {padded[i : i + 2] for i in range(len(padded) - 1)}


def dice(a: str, b: str) -> float:
    ba, bb = _bigrams(a), _bigrams(b)
    if not ba or not bb:
        return 0.0
    return 2 * len(ba & bb) / (len(ba) + len(bb))


@dataclass(frozen=True)
class MatchDecision:
    canonical_id: str | None
    confidence: float
    review_status: str
    candidates: tuple[tuple[str, float], ...]
    reason: str


AUTO_MIN = 0.93
AMBIGUOUS_GAP = 0.12


def match_name(
    query: str,
    catalog: list[tuple[str, str]],
    *,
    auto_min: float = AUTO_MIN,
) -> MatchDecision:
    """Never silently join same-name or near-tie players.

    catalog: list of (canonical_id, display_name)
    """
    q = _token_sort(query)
    scored: list[tuple[str, str, float]] = []
    for cid, name in catalog:
        scored.append((cid, name, dice(q, _token_sort(name))))
    scored.sort(key=lambda x: x[2], reverse=True)
    if not scored or scored[0][2] <= 0:
        return MatchDecision(None, 0.0, "unmatched", (), "NO_CANDIDATE")
    top = scored[0]
    second = scored[1] if len(scored) > 1 else None
    pairs = tuple((c, s) for c, _, s in scored[:5])
    query_norm = normalize_name(query)
    exact = [item for item in scored if normalize_name(item[1]) == query_norm]
    exact_ids = {item[0] for item in exact}
    if len(exact_ids) > 1:
        return MatchDecision(None, 1.0, "needs_review", pairs, "DUPLICATE_DISPLAY_NAME")
    if len(exact_ids) == 1:
        chosen = exact[0]
        return MatchDecision(chosen[0], chosen[2], "auto", pairs, "AUTO_MATCH")
    # "James Jordan" and "Jordan James" sort to the same tokens and are different people.
    query_sorted = _token_sort(query)
    reordered = [
        item[0]
        for item in scored
        if item[2] >= 0.99 and _token_sort(item[1]) == query_sorted and normalize_name(item[1]) != query_norm
    ]
    if reordered:
        return MatchDecision(None, top[2], "needs_review", pairs, "REORDERED_NAME")
    if second and (top[2] - second[2]) < AMBIGUOUS_GAP and second[2] >= auto_min - 0.15:
        return MatchDecision(None, top[2], "needs_review", pairs, "AMBIGUOUS_NEAR_TIE")
    return MatchDecision(None, top[2], "needs_review", pairs, "LOW_CONFIDENCE")
