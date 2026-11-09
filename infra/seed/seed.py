"""Load a reproducible NFL + FBS demonstration slate.

Synthetic research data so the dashboard runs without vendor keys.
Not a licensed live feed. Missing injury coverage is explicit.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages" / "core"))

from spe.config import settings  # noqa: E402
from spe.db.base import utcnow  # noqa: E402
from spe.db.models import (  # noqa: E402
    Bookmaker,
    DepthChartSnapshot,
    FeatureRow,
    Forecast,
    Game,
    InjuryEvent,
    League,
    Market,
    ModelHealthSnapshot,
    ModelVersion,
    OddsTick,
    Outcome,
    Player,
    PlayerGameStat,
    Recommendation,
    Roster,
    Season,
    Settlement,
    SimulationRun,
    Sport,
    Team,
    TeamGameStat,
    Venue,
    WeatherSnapshot,
)
from spe.db.session import init_db, session_factory  # noqa: E402
from spe.ingest.seed_guard import seed_block_reason  # noqa: E402
from spe.ingest.features import build_player_features  # noqa: E402
from spe.market.odds import ev_per_dollar  # noqa: E402
from spe.recommend.engine import RecommendInput, recommend  # noqa: E402

AS_OF = datetime(2026, 11, 6, 18, 0, tzinfo=UTC)
KICK_NFL = datetime(2026, 11, 8, 18, 25, tzinfo=UTC)
KICK_NFL_SNF = datetime(2026, 11, 8, 23, 20, tzinfo=UTC)
KICK_FBS = datetime(2026, 11, 7, 20, 0, tzinfo=UTC)
KICK_FBS_NIGHT = datetime(2026, 11, 7, 23, 30, tzinfo=UTC)


def run() -> None:
    init_db()
    s = session_factory()
    reason = seed_block_reason(s)
    if reason:
        print(f"Refusing to seed: {reason}. Existing rows were not changed.")
        s.close()
        return

    s.add(Sport(sport_id="football", name="American football"))
    s.flush()
    s.add_all(
        [
            League(
                league_id="nfl",
                sport_id="football",
                name="National Football League",
                classification="nfl",
            ),
            League(
                league_id="fbs",
                sport_id="football",
                name="NCAA Division I FBS",
                classification="fbs",
            ),
        ]
    )
    s.flush()
    s.add_all(
        [
            Season(
                season_id="nfl-2026",
                league_id="nfl",
                year=2026,
                start_at=datetime(2026, 9, 7, tzinfo=UTC),
                end_at=datetime(2027, 2, 14, tzinfo=UTC),
            ),
            Season(
                season_id="fbs-2026",
                league_id="fbs",
                year=2026,
                start_at=datetime(2026, 8, 24, tzinfo=UTC),
                end_at=datetime(2027, 1, 20, tzinfo=UTC),
            ),
        ]
    )
    s.flush()

    venues = {
        "arrowhead": Venue(
            venue_id="arrowhead",
            name="GEHA Field at Arrowhead Stadium",
            lat=39.0489,
            lon=-94.4839,
            altitude_m=264,
            roof_type="open",
            surface="grass",
            timezone="America/Chicago",
        ),
        "ford": Venue(
            venue_id="ford",
            name="Ford Field",
            lat=42.3400,
            lon=-83.0456,
            altitude_m=183,
            roof_type="dome",
            surface="turf",
            timezone="America/Detroit",
        ),
        "lincoln": Venue(
            venue_id="lincoln",
            name="Lincoln Financial Field",
            lat=39.9008,
            lon=-75.1675,
            altitude_m=12,
            roof_type="open",
            surface="grass",
            timezone="America/New_York",
        ),
        "levis": Venue(
            venue_id="levis",
            name="Levi's Stadium",
            lat=37.4030,
            lon=-121.9700,
            altitude_m=6,
            roof_type="open",
            surface="grass",
            timezone="America/Los_Angeles",
        ),
        "ohio": Venue(
            venue_id="ohio",
            name="Ohio Stadium",
            lat=40.0017,
            lon=-83.0197,
            altitude_m=222,
            roof_type="open",
            surface="turf",
            timezone="America/New_York",
        ),
        "bryant": Venue(
            venue_id="bryant",
            name="Bryant-Denny Stadium",
            lat=33.2085,
            lon=-87.5504,
            altitude_m=68,
            roof_type="open",
            surface="grass",
            timezone="America/Chicago",
        ),
        "autzen": Venue(
            venue_id="autzen",
            name="Autzen Stadium",
            lat=44.0583,
            lon=-123.0686,
            altitude_m=127,
            roof_type="open",
            surface="turf",
            timezone="America/Los_Angeles",
        ),
        "sanford": Venue(
            venue_id="sanford",
            name="Sanford Stadium",
            lat=33.9498,
            lon=-83.3733,
            altitude_m=186,
            roof_type="open",
            surface="grass",
            timezone="America/New_York",
        ),
    }
    s.add_all(venues.values())
    s.flush()

    def team(tid, league, name, abbr, conf=None):
        return Team(
            team_id=tid,
            league_id=league,
            name=name,
            abbreviation=abbr,
            conference=conf,
            aliases=[abbr, name],
        )

    teams = [
        team("kc", "nfl", "Kansas City Chiefs", "KC"),
        team("buf", "nfl", "Buffalo Bills", "BUF"),
        team("det", "nfl", "Detroit Lions", "DET"),
        team("gb", "nfl", "Green Bay Packers", "GB"),
        team("phi", "nfl", "Philadelphia Eagles", "PHI"),
        team("dal", "nfl", "Dallas Cowboys", "DAL"),
        team("sf", "nfl", "San Francisco 49ers", "SF"),
        team("sea", "nfl", "Seattle Seahawks", "SEA"),
        team("osu", "fbs", "Ohio State Buckeyes", "OSU", "Big Ten"),
        team("mich", "fbs", "Michigan Wolverines", "MICH", "Big Ten"),
        team("ala", "fbs", "Alabama Crimson Tide", "ALA", "SEC"),
        team("lsu", "fbs", "LSU Tigers", "LSU", "SEC"),
        team("ore", "fbs", "Oregon Ducks", "ORE", "Big Ten"),
        team("wash", "fbs", "Washington Huskies", "WASH", "Big Ten"),
        team("uga", "fbs", "Georgia Bulldogs", "UGA", "SEC"),
        team("tex", "fbs", "Texas Longhorns", "TEX", "SEC"),
    ]
    s.add_all(teams)
    s.flush()

    games = [
        Game(
            game_id="nfl-kc-buf",
            season_id="nfl-2026",
            league_id="nfl",
            home_team_id="kc",
            away_team_id="buf",
            kickoff_utc=KICK_NFL,
            venue_id="arrowhead",
            week=10,
            data_tier="demo",
        ),
        Game(
            game_id="nfl-det-gb",
            season_id="nfl-2026",
            league_id="nfl",
            home_team_id="det",
            away_team_id="gb",
            kickoff_utc=KICK_NFL,
            venue_id="ford",
            week=10,
        ),
        Game(
            game_id="nfl-phi-dal",
            season_id="nfl-2026",
            league_id="nfl",
            home_team_id="phi",
            away_team_id="dal",
            kickoff_utc=KICK_NFL_SNF,
            venue_id="lincoln",
            week=10,
        ),
        Game(
            game_id="nfl-sf-sea",
            season_id="nfl-2026",
            league_id="nfl",
            home_team_id="sf",
            away_team_id="sea",
            kickoff_utc=KICK_NFL,
            venue_id="levis",
            week=10,
        ),
        Game(
            game_id="fbs-osu-mich",
            season_id="fbs-2026",
            league_id="fbs",
            home_team_id="osu",
            away_team_id="mich",
            kickoff_utc=KICK_FBS_NIGHT,
            venue_id="ohio",
            week=11,
        ),
        Game(
            game_id="fbs-ala-lsu",
            season_id="fbs-2026",
            league_id="fbs",
            home_team_id="ala",
            away_team_id="lsu",
            kickoff_utc=KICK_FBS,
            venue_id="bryant",
            week=11,
        ),
        Game(
            game_id="fbs-ore-wash",
            season_id="fbs-2026",
            league_id="fbs",
            home_team_id="ore",
            away_team_id="wash",
            kickoff_utc=KICK_FBS,
            venue_id="autzen",
            week=11,
        ),
        Game(
            game_id="fbs-uga-tex",
            season_id="fbs-2026",
            league_id="fbs",
            home_team_id="uga",
            away_team_id="tex",
            kickoff_utc=KICK_FBS_NIGHT,
            venue_id="sanford",
            week=11,
        ),
    ]
    s.add_all(games)
    s.flush()

    players = [
        # NFL KC/BUF
        ("p-kelce", "Travis Kelce", "TE", "TE", "kc"),
        ("p-rice", "Rashee Rice", "WR", "WR", "kc"),
        ("p-pacheco", "Isiah Pacheco", "RB", "RB", "kc"),
        ("p-mahomes", "Patrick Mahomes", "QB", "QB", "kc"),
        ("p-cook", "James Cook", "RB", "RB", "buf"),
        ("p-kincaid", "Dalton Kincaid", "TE", "TE", "buf"),
        ("p-shakir", "Khalil Shakir", "WR", "WR", "buf"),
        # DET/GB
        ("p-gibbs", "Jahmyr Gibbs", "RB", "RB", "det"),
        ("p-stbrown", "Amon-Ra St. Brown", "WR", "WR", "det"),
        ("p-laporta", "Sam LaPorta", "TE", "TE", "det"),
        ("p-jacobs", "Josh Jacobs", "RB", "RB", "gb"),
        ("p-reed", "Jayden Reed", "WR", "WR", "gb"),
        # PHI/DAL
        ("p-saquon", "Saquon Barkley", "RB", "RB", "phi"),
        ("p-ajb", "A.J. Brown", "WR", "WR", "phi"),
        ("p-lamb", "CeeDee Lamb", "WR", "WR", "dal"),
        ("p-prescott", "Dak Prescott", "QB", "QB", "dal"),
        # SF/SEA
        ("p-cmc", "Christian McCaffrey", "RB", "RB", "sf"),
        ("p-aayuk", "Brandon Aiyuk", "WR", "WR", "sf"),
        ("p-kw3", "Kenneth Walker III", "RB", "RB", "sea"),
        # FBS
        ("p-sayin", "Julian Sayin", "QB", "QB", "osu"),
        ("p-tatum", "C.J. Donaldson", "RB", "RB", "osu"),
        ("p-inn", "Carnell Tate", "WR", "WR", "osu"),
        ("p-edwards", "Kalel Mullings", "RB", "RB", "mich"),
        ("p-loveland", "Colston Loveland", "TE", "TE", "mich"),
        ("p-milroe", "Ty Simpson", "QB", "QB", "ala"),
        ("p-bernard", "Jam Miller", "RB", "RB", "ala"),
        ("p-nuss", "Nussmeier", "QB", "QB", "lsu"),
        ("p-harvey", "Caden Durham", "RB", "RB", "lsu"),
        ("p-jeanty", "Noah Whittington", "RB", "RB", "ore"),
        ("p-te", "Kenyon Sadiq", "TE", "TE", "ore"),
        ("p-dillon", "Dillon Johnson", "RB", "RB", "uga"),
        ("p-bowers", "Oscar Delp", "TE", "TE", "uga"),
        ("p-blue", "CJ Baxter", "RB", "RB", "tex"),
        ("p-worthy", "Isaiah Bond", "WR", "WR", "tex"),
    ]
    for pid, name, group, pos, tid in players:
        s.add(
            Player(
                player_id=pid,
                name=name,
                position_group=group,
                position=pos,
                aliases=[name],
            )
        )
    s.flush()
    for pid, name, group, pos, tid in players:
        s.add(
            Roster(
                team_id=tid,
                player_id=pid,
                valid_from=datetime(2026, 8, 1, tzinfo=UTC),
                status="active",
            )
        )
    s.flush()

    s.add_all(
        [
            Bookmaker(bookmaker_id="draftkings", name="DraftKings"),
            Bookmaker(bookmaker_id="fanduel", name="FanDuel"),
            Bookmaker(bookmaker_id="betmgm", name="BetMGM"),
        ]
    )
    s.add_all(
        [
            Market(
                market_id="anytime_td",
                market_type="player_yesno",
                period="game",
                stat="touchdowns",
                grading_rule="Anytime offensive TD (rush, rec, or pass as designated by book). Defensive/ST usually excluded unless book states otherwise.",
                odds_api_key="player_anytime_td",
            ),
            Market(
                market_id="receptions",
                market_type="player_ou",
                period="game",
                stat="receptions",
                grading_rule="Receptions over/under. Push if integer line lands exactly.",
                odds_api_key="player_receptions",
            ),
            Market(
                market_id="receiving_yards",
                market_type="player_ou",
                period="game",
                stat="receiving_yards",
                grading_rule="Receiving yards over/under including overtime unless book states otherwise.",
                odds_api_key="player_reception_yds",
            ),
            Market(
                market_id="rushing_yards",
                market_type="player_ou",
                period="game",
                stat="rushing_yards",
                grading_rule="Rushing yards over/under including overtime unless book states otherwise.",
                odds_api_key="player_rush_yds",
            ),
        ]
    )

    s.flush()
    s.add_all(
        [
            ModelVersion(
                model_version_id="nfl-hier-td-2026w09",
                code_sha="dev-unspecified",
                training_cutoff=datetime(2026, 11, 3, tzinfo=UTC),
                data_snapshot="seed-nfl-2026w09",
                hyperparameters={"k_player": 8, "family": "poisson_eb"},
                calibration_version="cal-nfl-v0",
                league_id="nfl",
                family="hierarchical_td",
            ),
            ModelVersion(
                model_version_id="fbs-hier-td-2026w10",
                code_sha="dev-unspecified",
                training_cutoff=datetime(2026, 11, 3, tzinfo=UTC),
                data_snapshot="seed-fbs-2026w10",
                hyperparameters={"k_player": 14, "family": "poisson_eb", "conference_shrink": True},
                calibration_version="cal-fbs-v0",
                league_id="fbs",
                family="hierarchical_td",
            ),
        ]
    )
    s.flush()

    def weather(game_id, venue_id, temp, wind, gust, precip, roof, issued_off_h=3):
        kick = next(g.kickoff_utc for g in games if g.game_id == game_id)
        s.add(
            WeatherSnapshot(
                venue_id=venue_id,
                game_id=game_id,
                forecast_for=kick,
                issued_at=AS_OF - timedelta(hours=issued_off_h),
                temperature_c=temp,
                wind_mps=wind,
                gust_mps=gust,
                precip_mm=precip,
                roof_status=roof,
                provider="nws",
                provider_record_id=f"{game_id}-wx",
                observed_at=AS_OF - timedelta(hours=issued_off_h),
                effective_at=kick,
                quality_status="ok",
            )
        )

    weather("nfl-kc-buf", "arrowhead", 7.0, 8.5, 13.0, 0.4, "open")
    weather("nfl-det-gb", "ford", 18.0, 0.0, 0.0, 0.0, "closed")
    weather("nfl-phi-dal", "lincoln", 11.0, 4.2, 7.0, 1.8, "open")
    weather("nfl-sf-sea", "levis", 16.0, 5.0, 8.0, 0.0, "open")
    weather("fbs-osu-mich", "ohio", 4.0, 6.0, 11.0, 0.2, "open")
    weather("fbs-ala-lsu", "bryant", 18.0, 3.0, 5.0, 0.0, "open")
    weather("fbs-ore-wash", "autzen", 9.0, 2.0, 4.0, 3.5, "open")
    weather("fbs-uga-tex", "sanford", 14.0, 3.5, 6.0, 0.0, "open")

    # Injuries: never treat missing as healthy.
    s.add(
        InjuryEvent(
            game_id="nfl-kc-buf",
            player_id="p-pacheco",
            designation="questionable",
            practice_status="limited",
            reported_at=AS_OF - timedelta(hours=6),
            source="team_injury_report",
            confidence=0.82,
            coverage_complete=True,
            provider="seed",
            provider_record_id="inj-pacheco",
            observed_at=AS_OF - timedelta(hours=6),
            effective_at=KICK_NFL,
        )
    )
    s.add(
        InjuryEvent(
            game_id="nfl-sf-sea",
            player_id="p-aayuk",
            designation="unknown",
            practice_status=None,
            reported_at=AS_OF - timedelta(days=4),
            source="school_report_gap",
            confidence=0.25,
            coverage_complete=False,
            provider="seed",
            provider_record_id="inj-aiyuk-gap",
            observed_at=AS_OF - timedelta(days=4),
            effective_at=KICK_NFL,
            quality_status="stale",
        )
    )
    s.add(
        InjuryEvent(
            game_id="fbs-osu-mich",
            player_id="p-edwards",
            designation="probable",
            practice_status="full",
            reported_at=AS_OF - timedelta(hours=20),
            source="conference_report",
            confidence=0.55,
            coverage_complete=False,
            provider="seed",
            provider_record_id="inj-mullings",
            observed_at=AS_OF - timedelta(hours=20),
            effective_at=KICK_FBS_NIGHT,
            quality_status="thin",
        )
    )

    # Feature + outcome + forecast factory
    props: list[dict] = [
        # Blueprint example: p=.44 at +180 → EV +23.2% (still haircut-tested)
        dict(
            oid="o-rice-atd",
            gid="nfl-kc-buf",
            pid="p-rice",
            market="anytime_td",
            side="yes",
            line=None,
            p=0.44,
            lo=0.36,
            hi=0.51,
            american=180,
            book="draftkings",
            avail="active",
            avail_p=0.96,
            inj_c=0.9,
            cal_n=220,
            fresh=True,
            usage=dict(snap_share=0.88, td_weight=0.22, rec_lambda=6.4, rec_yards_mu=78, rush_yards_mu=4),
            version="nfl-hier-td-2026w09",
        ),
        dict(
            oid="o-kelce-atd",
            gid="nfl-kc-buf",
            pid="p-kelce",
            market="anytime_td",
            side="yes",
            line=None,
            p=0.38,
            lo=0.30,
            hi=0.46,
            american=-110,
            book="fanduel",
            avail="active",
            avail_p=0.97,
            inj_c=0.9,
            cal_n=220,
            fresh=True,
            usage=dict(snap_share=0.82, td_weight=0.18, rec_lambda=6.1, rec_yards_mu=62, rush_yards_mu=1),
            version="nfl-hier-td-2026w09",
        ),
        dict(
            oid="o-pacheco-rush",
            gid="nfl-kc-buf",
            pid="p-pacheco",
            market="rushing_yards",
            side="over",
            line=64.5,
            p=0.41,
            lo=0.28,
            hi=0.55,
            american=-115,
            book="betmgm",
            avail="questionable",
            avail_p=0.62,
            inj_c=0.82,
            cal_n=180,
            fresh=True,
            usage=dict(snap_share=0.55, td_weight=0.16, rec_lambda=2.1, rec_yards_mu=12, rush_yards_mu=58),
            version="nfl-hier-td-2026w09",
        ),
        dict(
            oid="o-cook-atd",
            gid="nfl-kc-buf",
            pid="p-cook",
            market="anytime_td",
            side="yes",
            line=None,
            p=0.47,
            lo=0.39,
            hi=0.54,
            american=105,
            book="draftkings",
            avail="active",
            avail_p=0.98,
            inj_c=0.9,
            cal_n=220,
            fresh=True,
            usage=dict(snap_share=0.72, td_weight=0.24, rec_lambda=2.8, rec_yards_mu=18, rush_yards_mu=78),
            version="nfl-hier-td-2026w09",
        ),
        dict(
            oid="o-stbrown-rec",
            gid="nfl-det-gb",
            pid="p-stbrown",
            market="receptions",
            side="over",
            line=7.5,
            p=0.58,
            lo=0.50,
            hi=0.66,
            american=-120,
            book="fanduel",
            avail="active",
            avail_p=0.99,
            inj_c=0.95,
            cal_n=240,
            fresh=True,
            usage=dict(snap_share=0.91, td_weight=0.14, rec_lambda=9.2, rec_yards_mu=96, rush_yards_mu=3),
            version="nfl-hier-td-2026w09",
        ),
        dict(
            oid="o-gibbs-rush",
            gid="nfl-det-gb",
            pid="p-gibbs",
            market="rushing_yards",
            side="over",
            line=78.5,
            p=0.54,
            lo=0.46,
            hi=0.61,
            american=-110,
            book="draftkings",
            avail="active",
            avail_p=0.97,
            inj_c=0.9,
            cal_n=200,
            fresh=True,
            usage=dict(snap_share=0.68, td_weight=0.21, rec_lambda=4.0, rec_yards_mu=28, rush_yards_mu=86),
            version="nfl-hier-td-2026w09",
        ),
        dict(
            oid="o-saquon-atd",
            gid="nfl-phi-dal",
            pid="p-saquon",
            market="anytime_td",
            side="yes",
            line=None,
            p=0.52,
            lo=0.44,
            hi=0.59,
            american=-125,
            book="fanduel",
            avail="active",
            avail_p=0.98,
            inj_c=0.92,
            cal_n=250,
            fresh=True,
            usage=dict(snap_share=0.80, td_weight=0.28, rec_lambda=3.2, rec_yards_mu=22, rush_yards_mu=92),
            version="nfl-hier-td-2026w09",
        ),
        dict(
            oid="o-lamb-yds",
            gid="nfl-phi-dal",
            pid="p-lamb",
            market="receiving_yards",
            side="over",
            line=79.5,
            p=0.49,
            lo=0.41,
            hi=0.58,
            american=-108,
            book="draftkings",
            avail="active",
            avail_p=0.96,
            inj_c=0.9,
            cal_n=210,
            fresh=False,
            usage=dict(snap_share=0.89, td_weight=0.16, rec_lambda=8.4, rec_yards_mu=92, rush_yards_mu=6),
            version="nfl-hier-td-2026w09",
        ),
        dict(
            oid="o-cmc-atd",
            gid="nfl-sf-sea",
            pid="p-cmc",
            market="anytime_td",
            side="yes",
            line=None,
            p=0.57,
            lo=0.49,
            hi=0.64,
            american=-140,
            book="betmgm",
            avail="active",
            avail_p=0.94,
            inj_c=0.88,
            cal_n=260,
            fresh=True,
            usage=dict(snap_share=0.85, td_weight=0.30, rec_lambda=5.5, rec_yards_mu=42, rush_yards_mu=78),
            version="nfl-hier-td-2026w09",
        ),
        dict(
            oid="o-aiyuk-rec",
            gid="nfl-sf-sea",
            pid="p-aayuk",
            market="receptions",
            side="over",
            line=4.5,
            p=0.51,
            lo=0.33,
            hi=0.64,
            american=-105,
            book="fanduel",
            avail="missing_report",
            avail_p=0.5,
            inj_c=0.25,
            cal_n=90,
            fresh=True,
            usage=dict(snap_share=0.4, td_weight=0.08, rec_lambda=4.5, rec_yards_mu=55, rush_yards_mu=2),
            version="nfl-hier-td-2026w09",
        ),
        dict(
            oid="o-tate-atd",
            gid="fbs-osu-mich",
            pid="p-inn",
            market="anytime_td",
            side="yes",
            line=None,
            p=0.33,
            lo=0.22,
            hi=0.46,
            american=165,
            book="draftkings",
            avail="active",
            avail_p=0.93,
            inj_c=0.7,
            cal_n=95,
            fresh=True,
            usage=dict(snap_share=0.78, td_weight=0.19, rec_lambda=5.1, rec_yards_mu=68, rush_yards_mu=2),
            version="fbs-hier-td-2026w10",
        ),
        dict(
            oid="o-mullings-rush",
            gid="fbs-osu-mich",
            pid="p-edwards",
            market="rushing_yards",
            side="over",
            line=72.5,
            p=0.48,
            lo=0.31,
            hi=0.62,
            american=-110,
            book="fanduel",
            avail="unknown",
            avail_p=0.7,
            inj_c=0.55,
            cal_n=60,
            fresh=True,
            usage=dict(snap_share=0.62, td_weight=0.22, rec_lambda=1.4, rec_yards_mu=8, rush_yards_mu=74),
            version="fbs-hier-td-2026w10",
        ),
        dict(
            oid="o-miller-atd",
            gid="fbs-ala-lsu",
            pid="p-bernard",
            market="anytime_td",
            side="yes",
            line=None,
            p=0.41,
            lo=0.30,
            hi=0.52,
            american=140,
            book="draftkings",
            avail="active",
            avail_p=0.95,
            inj_c=0.8,
            cal_n=110,
            fresh=True,
            usage=dict(snap_share=0.70, td_weight=0.23, rec_lambda=1.8, rec_yards_mu=12, rush_yards_mu=82),
            version="fbs-hier-td-2026w10",
        ),
        dict(
            oid="o-durham-rush",
            gid="fbs-ala-lsu",
            pid="p-harvey",
            market="rushing_yards",
            side="over",
            line=68.5,
            p=0.53,
            lo=0.42,
            hi=0.63,
            american=-115,
            book="betmgm",
            avail="active",
            avail_p=0.92,
            inj_c=0.75,
            cal_n=88,
            fresh=True,
            usage=dict(snap_share=0.66, td_weight=0.20, rec_lambda=2.0, rec_yards_mu=14, rush_yards_mu=76),
            version="fbs-hier-td-2026w10",
        ),
        dict(
            oid="o-sadiq-rec",
            gid="fbs-ore-wash",
            pid="p-te",
            market="receptions",
            side="over",
            line=3.5,
            p=0.57,
            lo=0.45,
            hi=0.68,
            american=-118,
            book="fanduel",
            avail="active",
            avail_p=0.94,
            inj_c=0.78,
            cal_n=70,
            fresh=True,
            usage=dict(snap_share=0.74, td_weight=0.12, rec_lambda=4.6, rec_yards_mu=48, rush_yards_mu=2),
            version="fbs-hier-td-2026w10",
        ),
        dict(
            oid="o-baxter-atd",
            gid="fbs-uga-tex",
            pid="p-blue",
            market="anytime_td",
            side="yes",
            line=None,
            p=0.36,
            lo=0.24,
            hi=0.49,
            american=175,
            book="draftkings",
            avail="active",
            avail_p=0.9,
            inj_c=0.72,
            cal_n=84,
            fresh=True,
            usage=dict(snap_share=0.58, td_weight=0.18, rec_lambda=1.6, rec_yards_mu=10, rush_yards_mu=66),
            version="fbs-hier-td-2026w10",
        ),
        dict(
            oid="o-bond-yds",
            gid="fbs-uga-tex",
            pid="p-worthy",
            market="receiving_yards",
            side="over",
            line=54.5,
            p=0.50,
            lo=0.38,
            hi=0.61,
            american=-105,
            book="fanduel",
            avail="active",
            avail_p=0.91,
            inj_c=0.7,
            cal_n=76,
            fresh=True,
            usage=dict(snap_share=0.72, td_weight=0.11, rec_lambda=4.8, rec_yards_mu=62, rush_yards_mu=8),
            version="fbs-hier-td-2026w10",
        ),
        dict(
            oid="o-jacobs-atd",
            gid="nfl-det-gb",
            pid="p-jacobs",
            market="anytime_td",
            side="yes",
            line=None,
            p=0.42,
            lo=0.34,
            hi=0.50,
            american=120,
            book="draftkings",
            avail="active",
            avail_p=0.97,
            inj_c=0.9,
            cal_n=230,
            fresh=True,
            usage=dict(snap_share=0.71, td_weight=0.22, rec_lambda=2.4, rec_yards_mu=14, rush_yards_mu=74),
            version="nfl-hier-td-2026w09",
        ),
    ]

    now = AS_OF
    for p in props:
        s.add(
            Outcome(
                outcome_id=p["oid"],
                market_id=p["market"],
                game_id=p["gid"],
                player_id=p["pid"],
                side=p["side"],
                line=p["line"],
                description=f"{p['pid']} {p['market']} {p['side']} {p['line']}",
            )
        )
        s.flush()
        captured = now if p["fresh"] else now - timedelta(hours=6)
        s.add(
            OddsTick(
                outcome_id=p["oid"],
                bookmaker_id=p["book"],
                price_american=p["american"],
                captured_at=captured,
                source_at=captured,
                is_live=False,
                provider="odds_api",
                provider_record_id=f"tick-{p['oid']}",
                observed_at=captured,
                effective_at=captured,
                quality_status="ok" if p["fresh"] else "stale",
            )
        )
        reasons = ["MARKET_ANCHORED_SEPARATELY"]
        if p["avail"] == "missing_report":
            reasons.append("INJURY_COVERAGE_INCOMPLETE")
        fc = Forecast(
            forecast_id=f"fc-{p['oid']}",
            outcome_id=p["oid"],
            as_of=now,
            model_version_id=p["version"],
            p=p["p"],
            p_low=p["lo"],
            p_high=p["hi"],
            sim_count=10000,
            data_cutoff=datetime(2026, 11, 6, 16, 0, tzinfo=UTC),
            availability_status=p["avail"],
            reason_codes=reasons,
            components={"hier_mean": p["p"], "market_not_in_features": True},
            is_official=True,
        )
        s.add(fc)
        s.flush()
        rec = recommend(
            RecommendInput(
                p=p["p"],
                p_low=p["lo"],
                p_high=p["hi"],
                american=p["american"],
                price_captured_at=captured,
                game_status="scheduled",
                availability_status=p["avail"],
                availability_prob=p["avail_p"],
                injury_confidence=p["inj_c"],
                source_fresh=p["fresh"],
                grading_supported=True,
                calibration_n=p["cal_n"],
            ),
            now=now,
        )
        s.add(
            Recommendation(
                forecast_id=fc.forecast_id,
                odds_tick_id=None,
                ev=rec.ev,
                ev_after_haircut=rec.ev_after_haircut,
                threshold=rec.threshold,
                status=rec.status,
                reason_codes=rec.reason_codes,
                kelly_fractional=rec.kelly_fractional,
                created_at=now,
            )
        )
        blob = build_player_features(
            as_of=now,
            sample_size=p["cal_n"],
            source="seed",
            usage=p["usage"],
            opponent={"epa_pass": -0.02, "epa_rush": 0.04},
            availability={
                "prob": p["avail_p"],
                "status": p["avail"],
                "report_missing": p["avail"] in {"missing_report", "unknown"},
            },
            weather={"included": True},
            market_anchor=None,
        )
        s.add(
            FeatureRow(
                game_id=p["gid"],
                player_id=p["pid"],
                feature_version="feat-v0",
                as_of=now,
                feature_blob=blob,
                coverage_mask={"injury": p["avail"] not in {"missing_report", "unknown"}, "weather": True},
                sample_size=p["cal_n"],
                missingness=blob["missingness"],
                source="seed",
            )
        )
        s.add(
            DepthChartSnapshot(
                game_id=p["gid"],
                player_id=p["pid"],
                position=next(x[3] for x in players if x[0] == p["pid"]),
                rank=1,
                snapshot_at=now,
                provider="seed",
                provider_record_id=f"dc-{p['pid']}",
                observed_at=now,
                effective_at=now,
            )
        )

    # Historical ledger rows (settled) so Track Record is not empty.
    s.add(
        Outcome(
            outcome_id="o-hist-gibbs",
            market_id="anytime_td",
            game_id="nfl-det-gb",
            player_id="p-gibbs",
            side="yes",
            description="historical shadow pick",
        )
    )
    s.flush()
    s.add(
        Forecast(
            forecast_id="fc-hist-gibbs",
            outcome_id="o-hist-gibbs",
            as_of=AS_OF - timedelta(days=7),
            model_version_id="nfl-hier-td-2026w09",
            p=0.46,
            p_low=0.38,
            p_high=0.54,
            sim_count=10000,
            data_cutoff=AS_OF - timedelta(days=7, hours=2),
            availability_status="active",
            reason_codes=["SHADOW"],
            is_official=False,
        )
    )
    s.flush()
    s.add(
        Recommendation(
            forecast_id="fc-hist-gibbs",
            ev=0.09,
            ev_after_haircut=0.05,
            threshold=0.04,
            status="play",
            reason_codes=["EDGE_AFTER_HAIRCUT_OK", "SHADOW_LEDGER"],
            created_at=AS_OF - timedelta(days=7),
        )
    )
    s.add(
        Settlement(
            outcome_id="o-hist-gibbs",
            result="win",
            settled_at=AS_OF - timedelta(days=6),
            official_source="seed_official_box",
        )
    )

    s.add_all(
        [
            ModelHealthSnapshot(
                league_id="nfl",
                market_id="anytime_td",
                as_of=AS_OF,
                brier=0.198,
                log_loss=0.572,
                ece=0.031,
                skill_vs_novig=0.012,
                n=420,
                coverage=0.987,
                freshness_minutes=38,
                clv=0.004,
                notes="Prototype seed metrics — not a live backtest. No profit claim.",
            ),
            ModelHealthSnapshot(
                league_id="fbs",
                market_id="anytime_td",
                as_of=AS_OF,
                brier=0.221,
                log_loss=0.631,
                ece=0.058,
                skill_vs_novig=-0.006,
                n=180,
                coverage=0.91,
                freshness_minutes=52,
                clv=None,
                notes="FBS sample thinner; gate fails if this is hidden behind NFL skill.",
            ),
            ModelHealthSnapshot(
                league_id="nfl",
                market_id="receptions",
                as_of=AS_OF,
                brier=0.241,
                log_loss=0.681,
                ece=0.027,
                skill_vs_novig=0.008,
                n=310,
                coverage=0.972,
                freshness_minutes=38,
                clv=0.002,
                notes="Prototype seed metrics.",
            ),
            ModelHealthSnapshot(
                league_id="fbs",
                market_id="rushing_yards",
                as_of=AS_OF,
                brier=0.252,
                log_loss=0.702,
                ece=0.071,
                skill_vs_novig=None,
                n=96,
                coverage=0.84,
                freshness_minutes=52,
                clv=None,
                notes="Yes-only and sparse FBS props: no-vig unidentified on some books.",
            ),
        ]
    )

    s.add(
        SimulationRun(
            run_id="sim-demo-kc-buf",
            game_id="nfl-kc-buf",
            as_of=AS_OF,
            seed=42,
            draw_count=10000,
            assumptions={"engine": "drive_dirichlet_v0", "rules": "nfl"},
            scenario="official",
            model_version_id="nfl-hier-td-2026w09",
            summary={
                "player_td_prob": {"p-rice": 0.44, "p-kelce": 0.38, "p-cook": 0.47},
                "score_quantiles": {"home": [17, 21, 24, 28, 34], "away": [16, 20, 23, 27, 31]},
            },
            convergence={"max_td_mc_se": 0.005, "draw_count": 10000, "target_se": 0.005},
            research_only=False,
        )
    )

    s.commit()
    s.close()
    print(f"Seeded {settings.database_url}")


if __name__ == "__main__":
    run()
