"""Real PostgreSQL checks. Only a disposable localhost test database is accepted."""
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from spe.db.base import Base
from spe.db.models import BoardSnapshot


@pytest.fixture
def pgdb():
    value = os.environ.get("TEST_POSTGRES_URL")
    if not value:
        pytest.skip("TEST_POSTGRES_URL is not configured")
    url = make_url(value)
    assert url.host in {"localhost", "127.0.0.1"} and (url.database or "").endswith("_test"), "Use a disposable local *_test database"
    schema = "launch_test_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    Base.metadata.create_all(engine)
    try:
        with Session(engine, expire_on_commit=False) as db:
            yield db
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_migrated_postgres_schema_matches_models(pgdb):
    # This checks the public schema created by Alembic, not the fixture's create_all.
    engine = pgdb.get_bind()
    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM public.alembic_version")).scalar_one()
        assert version == "20261116_04"
    inspector = inspect(engine)
    for table in Base.metadata.sorted_tables:
        columns = {c["name"] for c in inspector.get_columns(table.name, schema="public")}
        assert {c.name for c in table.columns} <= columns, table.name


def test_postgres_evidence_export(pgdb, tmp_path):
    from test_release_regressions import test_exporter_uses_measured_plays_on_shared_database
    test_exporter_uses_measured_plays_on_shared_database(pgdb, tmp_path)


def test_postgres_shared_inputs(pgdb, tmp_path, monkeypatch):
    from test_release_regressions import test_shared_input_reaches_independent_worker
    test_shared_input_reaches_independent_worker(pgdb, tmp_path, monkeypatch)


def test_postgres_concurrent_rate_limits(pgdb):
    from test_launch_hardening import test_attempts_survive_new_sessions_and_concurrent_requests
    test_attempts_survive_new_sessions_and_concurrent_requests(pgdb)


def test_postgres_delayed_and_repeated_billing(pgdb):
    from test_launch_hardening import test_duplicate_and_delayed_stripe_events_never_restore_canceled_access
    test_duplicate_and_delayed_stripe_events_never_restore_canceled_access(pgdb)


def test_postgres_json_query(pgdb):
    from app.queries import _games_for_desk_takes
    from test_publication_pipeline import seed
    seed(pgdb)
    pgdb.add(BoardSnapshot(key="test-json", payload={"game_id": "g"}, updated_at=datetime.now(UTC)))
    pgdb.commit()
    assert pgdb.query(BoardSnapshot).filter(BoardSnapshot.payload["game_id"].as_string() == "g").count() == 1
    # Exercise the real app query formerly using SQLite-only json_extract.
    assert isinstance(_games_for_desk_takes(pgdb, "nfl"), list)


def test_postgres_public_availability_timestamp(pgdb):
    from spe.ingest.public_availability import store_reports
    # Executes the real join/filter. A string bind raises SQLSTATE 42883, even on empty tables.
    assert store_reports(pgdb, [])["inserted"] == 0


def test_postgres_record_query_count(pgdb):
    from test_functional_readiness import test_history_uses_two_queries_for_a_hundred_picks
    test_history_uses_two_queries_for_a_hundred_picks(pgdb)


def test_postgres_td_grades_are_batched(pgdb):
    from test_optimization_review import test_td_card_grades_batch_sources_and_match_individual_grading
    test_td_card_grades_batch_sources_and_match_individual_grading(pgdb)


def test_postgres_published_corrections(pgdb):
    from test_launch_hardening import test_stat_correction_is_auditable_and_consistent_across_record_views
    test_stat_correction_is_auditable_and_consistent_across_record_views(pgdb)


def test_postgres_single_use_recovery_is_atomic(pgdb):
    from concurrent.futures import ThreadPoolExecutor
    from spe.accounts.auth import AuthError, begin_password_reset, finish_password_reset, register_user
    user = register_user(pgdb, "reset@example.com", "long-password")
    _, token = begin_password_reset(pgdb, user.email)
    pgdb.commit()
    def use_token(_):
        with Session(pgdb.get_bind()) as db:
            try:
                finish_password_reset(db, token, "replacement-password")
                db.commit()
                return True
            except AuthError:
                db.rollback()
                return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(use_token, range(2))) == [False, True]
