"""CFB board live-proof freeze builds blend rows from finder payload."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from spe.shadow.fbs_board_live_proof import BLEND_FAMILY, build_board_freeze


def test_build_board_freeze_keeps_prekick_only(tmp_path: Path, monkeypatch) -> None:
    now = datetime(2026, 11, 7, 18, 0, tzinfo=UTC)
    kick_future = (now + timedelta(hours=4)).isoformat()
    kick_past = (now - timedelta(hours=1)).isoformat()
    finder = {
        "games": [
            {
                "league_id": "fbs",
                "game_id": "espn:fbs:future",
                "away": "A",
                "home": "B",
                "players": [
                    {
                        "player_id": "fbs:1",
                        "player_name": "Alpha",
                        "game_level_supported": True,
                        "probabilities": {
                            "primary_research": 0.30,
                            "c_recent": 0.28,
                            "player_td_v1": 0.32,
                        },
                    },
                    {
                        "player_id": "fbs:2",
                        "player_name": "Demoted",
                        "game_level_supported": False,
                        "probabilities": {"c_recent": 0.50, "player_td_v1": 0.40},
                    },
                ],
            },
            {
                "league_id": "fbs",
                "game_id": "espn:fbs:past",
                "away": "C",
                "home": "D",
                "players": [
                    {
                        "player_id": "fbs:3",
                        "player_name": "Late",
                        "game_level_supported": True,
                        "probabilities": {
                            "primary_research": 0.22,
                            "c_recent": 0.20,
                            "player_td_v1": 0.24,
                        },
                    }
                ],
            },
        ]
    }
    path = tmp_path / "anytime_td_finder.json"
    path.write_text(json.dumps(finder), encoding="utf-8")
    monkeypatch.setattr("spe.shadow.fbs_board_live_proof.finder_payload_path", lambda: path)
    monkeypatch.setattr(
        "spe.shadow.fbs_board_live_proof.freeze_path",
        lambda: tmp_path / "freeze.json",
    )

    class _Maps:
        def all(self):
            return [
                {"game_id": "espn:fbs:future", "kickoff_utc": kick_future},
                {"game_id": "espn:fbs:past", "kickoff_utc": kick_past},
            ]

    class _R:
        def mappings(self):
            return _Maps()

    class _S:
        def execute(self, *_a, **_k):
            return _R()

    freeze = build_board_freeze(_S(), now=now)
    assert freeze["n_games"] == 1
    assert freeze["n_prekick_players"] == 1
    assert freeze["n_demoted_skipped"] == 1
    assert freeze["n_games_already_kicked"] == 1
    assert freeze["board_primary"] == BLEND_FAMILY
    pl = freeze["games"][0]["players"][0]
    assert pl["player_id"] == "fbs:1"
    assert abs(float(pl["p_blend"]) - 0.30) < 1e-9
