from datetime import UTC

from spe.ingest.canonical import parse_kickoff


def test_nflverse_gametime_is_eastern():
    kick = parse_kickoff("2026-11-08", "13:00")
    assert kick is not None
    assert kick.astimezone(UTC).isoformat() == "2026-11-08T18:00:00+00:00"


def test_missing_gametime_stays_a_utc_placeholder():
    kick = parse_kickoff("2026-11-08", None)
    assert kick is not None
    assert kick.astimezone(UTC).hour == 18
