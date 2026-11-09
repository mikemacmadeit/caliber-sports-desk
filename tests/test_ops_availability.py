"""Injury coverage classification. Missing reports are never healthy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from spe.ops.availability import availability_for_player, designation_rules_player_out


def test_complete_report_unnamed_player_is_probable_not_assumed_from_silence():
    now = datetime(2026, 11, 6, 18, 0, tzinfo=UTC)
    info = availability_for_player(
        league_id="nfl",
        kickoff=now + timedelta(hours=24),
        player_injury=None,
        game_has_complete_report=True,
        now=now,
    )
    assert info["coverage"] == "report_received_complete"
    assert info["availability_status"] == "probable"
    assert info["abstain_reasons"] == []


def test_missing_report_inside_window_is_expected_missing():
    now = datetime(2026, 11, 6, 18, 0, tzinfo=UTC)
    info = availability_for_player(
        league_id="nfl",
        kickoff=now + timedelta(hours=24),
        player_injury=None,
        game_has_complete_report=False,
        now=now,
    )
    assert info["coverage"] == "report_expected_missing"
    assert info["availability_status"] == "unknown"
    assert "INJURY_REPORT_EXPECTED_MISSING" in info["abstain_reasons"]
    assert "NEVER_ASSUME_HEALTHY" in info["abstain_reasons"]


def test_far_kickoff_is_not_yet_scheduled():
    now = datetime(2026, 11, 6, 18, 0, tzinfo=UTC)
    info = availability_for_player(
        league_id="nfl",
        kickoff=now + timedelta(hours=120),
        player_injury=None,
        game_has_complete_report=False,
        now=now,
    )
    assert info["coverage"] == "report_not_yet_scheduled"
    assert "INJURY_REPORT_NOT_YET_SCHEDULED" in info["abstain_reasons"]


def test_fbs_source_unavailable_never_healthy():
    now = datetime(2026, 11, 6, 18, 0, tzinfo=UTC)
    info = availability_for_player(
        league_id="fbs",
        kickoff=now + timedelta(hours=12),
        player_injury=None,
        game_has_complete_report=False,
        now=now,
    )
    assert info["coverage"] == "source_unavailable"
    assert "INJURY_SOURCE_UNAVAILABLE" in info["abstain_reasons"]


def test_explicit_out_is_not_a_missed_practice():
    assert designation_rules_player_out("Out")
    assert designation_rules_player_out("Injured Reserve")
    assert not designation_rules_player_out("Did Not Participate In Practice")
    assert not designation_rules_player_out("Questionable")


def test_confirmed_out_is_unavailable():
    now = datetime(2026, 11, 6, 18, 0, tzinfo=UTC)
    row = SimpleNamespace(practice_status=None, designation="Out", confidence=0.9)
    info = availability_for_player(
        league_id="nfl",
        kickoff=now + timedelta(hours=12),
        player_injury=row,
        game_has_complete_report=True,
        now=now,
    )
    assert info["coverage"] == "confirmed_player_unavailable"
    assert "CONFIRMED_INACTIVE" in info["abstain_reasons"]
