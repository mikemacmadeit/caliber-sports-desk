"""Identity helpers.

Import ``spe.identity.resolve`` / ``spe.identity.games`` directly when needed.
Keeping this package init light avoids circular imports with ``spe.ingest``.
"""

from spe.identity.match import MatchDecision, match_name, normalize_name

__all__ = [
    "MatchDecision",
    "match_name",
    "normalize_name",
]
