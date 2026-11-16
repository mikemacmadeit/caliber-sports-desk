"""Adopt the existing Caliber schema and add launch-safety tables.

This revision is a frozen schema snapshot; later changes require new revisions.
Existing customer data is never dropped or rebuilt.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261116_01"
down_revision = None
branch_labels = None
depends_on = None

LEGACY_COLUMNS = [('games', 'data_tier', "VARCHAR(16) DEFAULT 'demo' NOT NULL"), ('games', 'home_score', 'INTEGER'), ('games', 'away_score', 'INTEGER'), ('forecasts', 'data_tier', "VARCHAR(16) DEFAULT 'demo' NOT NULL"), ('recommendations', 'data_tier', "VARCHAR(16) DEFAULT 'demo' NOT NULL"), ('plays', 'wp', 'FLOAT'), ('plays', 'success', 'BOOLEAN'), ('plays', 'is_pass', 'BOOLEAN DEFAULT FALSE NOT NULL'), ('plays', 'is_rush', 'BOOLEAN DEFAULT FALSE NOT NULL'), ('plays', 'is_td', 'BOOLEAN DEFAULT FALSE NOT NULL'), ('plays', 'is_turnover', 'BOOLEAN DEFAULT FALSE NOT NULL'), ('plays', 'is_red_zone', 'BOOLEAN DEFAULT FALSE NOT NULL'), ('plays', 'is_goal_line', 'BOOLEAN DEFAULT FALSE NOT NULL'), ('plays', 'is_scrimmage', 'BOOLEAN DEFAULT FALSE NOT NULL'), ('plays', 'first_down', 'BOOLEAN DEFAULT FALSE NOT NULL'), ('plays', 'game_seconds_remaining', 'INTEGER'), ('plays', 'drive_id', 'VARCHAR(32)'), ('plays', 'passer_player_id', 'VARCHAR(64)'), ('plays', 'rusher_player_id', 'VARCHAR(64)'), ('plays', 'receiver_player_id', 'VARCHAR(64)'), ('player_game_stats', 'gl_carries', 'INTEGER DEFAULT 0 NOT NULL'), ('player_game_stats', 'team_id', 'VARCHAR(64)'), ('weather_snapshots', 'precip_probability', 'FLOAT'), ('weather_snapshots', 'summary', 'VARCHAR(160)'), ('weather_snapshots', 'alerts', 'JSON'), ('participation_plays', 'possession_team', 'VARCHAR(8)'), ('participation_plays', 'offense_formation', 'VARCHAR(32)'), ('participation_plays', 'offense_personnel', 'TEXT'), ('participation_plays', 'defense_personnel', 'TEXT'), ('participation_plays', 'defenders_in_box', 'FLOAT'), ('participation_plays', 'number_of_pass_rushers', 'FLOAT'), ('participation_plays', 'route', 'VARCHAR(64)'), ('participation_plays', 'defense_man_zone_type', 'VARCHAR(32)'), ('participation_plays', 'defense_coverage_type', 'VARCHAR(32)'), ('participation_plays', 'time_to_throw', 'FLOAT'), ('participation_plays', 'was_pressure', 'BOOLEAN'), ('participation_plays', 'ngs_air_yards', 'FLOAT'), ('account_users', 'is_admin', 'BOOLEAN DEFAULT FALSE NOT NULL')]


def upgrade():
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if 'account_users' not in existing:
        op.create_table('account_users',
        sa.Column('user_id', sa.String(length=64), nullable=False),
        sa.Column('email', sa.String(length=320), nullable=False),
        sa.Column('password_hash', sa.String(length=256), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('stripe_customer_id', sa.String(length=64), nullable=True),
        sa.Column('subscription_id', sa.String(length=64), nullable=True),
        sa.Column('subscription_status', sa.String(length=32), nullable=False),
        sa.Column('current_period_end', sa.DateTime(timezone=True), nullable=True),
        sa.Column('is_admin', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('user_id'),
        sa.UniqueConstraint('email'),
        sa.UniqueConstraint('stripe_customer_id')
        )
    if 'ai_reviews' not in existing:
        op.create_table('ai_reviews',
        sa.Column('review_id', sa.String(length=64), nullable=False),
        sa.Column('game_id', sa.String(length=64), nullable=False),
        sa.Column('forecast_id', sa.String(length=64), nullable=False),
        sa.Column('origin', sa.String(length=32), nullable=False),
        sa.Column('suggested_experiment', sa.Text(), nullable=False),
        sa.Column('applied', sa.Boolean(), nullable=False),
        sa.Column('evidence', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('review_id')
        )
    if 'ats_predictions' not in existing:
        op.create_table('ats_predictions',
        sa.Column('ats_id', sa.String(length=64), nullable=False),
        sa.Column('game_id', sa.String(length=64), nullable=False),
        sa.Column('league_id', sa.String(length=16), nullable=False),
        sa.Column('home_team_id', sa.String(length=64), nullable=False),
        sa.Column('away_team_id', sa.String(length=64), nullable=False),
        sa.Column('bookmaker_id', sa.String(length=64), nullable=True),
        sa.Column('market_id', sa.String(length=64), nullable=False),
        sa.Column('side', sa.String(length=16), nullable=False),
        sa.Column('spread_line', sa.Float(), nullable=False),
        sa.Column('price_american', sa.Integer(), nullable=True),
        sa.Column('odds_tick_id', sa.String(length=64), nullable=True),
        sa.Column('predicted_margin', sa.Float(), nullable=False),
        sa.Column('predicted_home_score', sa.Float(), nullable=False),
        sa.Column('predicted_away_score', sa.Float(), nullable=False),
        sa.Column('p_cover', sa.Float(), nullable=False),
        sa.Column('p_push', sa.Float(), nullable=False),
        sa.Column('margin_sigma', sa.Float(), nullable=False),
        sa.Column('model_vs_market', sa.Float(), nullable=True),
        sa.Column('expected_value', sa.Float(), nullable=True),
        sa.Column('model_version_id', sa.String(length=64), nullable=False),
        sa.Column('predicted_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('data_quality_status', sa.String(length=32), nullable=False),
        sa.Column('confidence', sa.Float(), nullable=False),
        sa.Column('warnings', sa.JSON(), nullable=False),
        sa.Column('components', sa.JSON(), nullable=False),
        sa.Column('research_only', sa.Boolean(), nullable=False),
        sa.Column('play_enabled', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('ats_id')
        )
    if 'ats_predictions' not in existing:
        op.create_index('ix_ats_dedupe', 'ats_predictions', ['game_id', 'bookmaker_id', 'side', 'spread_line', 'model_version_id', 'odds_tick_id'], unique=False)
    if 'ats_predictions' not in existing:
        op.create_index(op.f('ix_ats_predictions_game_id'), 'ats_predictions', ['game_id'], unique=False)
    if 'ats_predictions' not in existing:
        op.create_index(op.f('ix_ats_predictions_league_id'), 'ats_predictions', ['league_id'], unique=False)
    if 'ats_predictions' not in existing:
        op.create_index(op.f('ix_ats_predictions_model_version_id'), 'ats_predictions', ['model_version_id'], unique=False)
    if 'ats_predictions' not in existing:
        op.create_index(op.f('ix_ats_predictions_predicted_at'), 'ats_predictions', ['predicted_at'], unique=False)
    if 'auth_rate_limits' not in existing:
        op.create_table('auth_rate_limits',
        sa.Column('key', sa.String(length=64), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('key')
        )
    if 'auth_rate_limits' not in existing:
        op.create_index(op.f('ix_auth_rate_limits_expires_at'), 'auth_rate_limits', ['expires_at'], unique=False)
    if 'backtest_runs' not in existing:
        op.create_table('backtest_runs',
        sa.Column('run_id', sa.String(length=64), nullable=False),
        sa.Column('league_id', sa.String(length=16), nullable=False),
        sa.Column('market_id', sa.String(length=64), nullable=False),
        sa.Column('model_family', sa.String(length=64), nullable=False),
        sa.Column('train_end', sa.DateTime(timezone=True), nullable=False),
        sa.Column('test_start', sa.DateTime(timezone=True), nullable=False),
        sa.Column('test_end', sa.DateTime(timezone=True), nullable=False),
        sa.Column('n', sa.Integer(), nullable=False),
        sa.Column('brier', sa.Float(), nullable=True),
        sa.Column('log_loss', sa.Float(), nullable=True),
        sa.Column('ece', sa.Float(), nullable=True),
        sa.Column('coverage', sa.Float(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('run_id')
        )
    if 'backtest_runs' not in existing:
        op.create_index(op.f('ix_backtest_runs_league_id'), 'backtest_runs', ['league_id'], unique=False)
    if 'billing_events' not in existing:
        op.create_table('billing_events',
        sa.Column('event_id', sa.String(length=128), nullable=False),
        sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('event_id')
        )
    if 'board_snapshots' not in existing:
        op.create_table('board_snapshots',
        sa.Column('key', sa.String(length=200), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('key')
        )
    if 'bookmakers' not in existing:
        op.create_table('bookmakers',
        sa.Column('bookmaker_id', sa.String(length=64), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('region', sa.String(length=16), nullable=False),
        sa.PrimaryKeyConstraint('bookmaker_id')
        )
    if 'credit_usage' not in existing:
        op.create_table('credit_usage',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('provider', sa.String(length=64), nullable=False),
        sa.Column('day', sa.String(length=10), nullable=False),
        sa.Column('credits_used', sa.Integer(), nullable=False),
        sa.Column('budget', sa.Integer(), nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('provider', 'day', name='uq_credit_day')
        )
    if 'data_coverage' not in existing:
        op.create_table('data_coverage',
        sa.Column('coverage_id', sa.String(length=64), nullable=False),
        sa.Column('provider', sa.String(length=64), nullable=False),
        sa.Column('dataset', sa.String(length=64), nullable=False),
        sa.Column('league_id', sa.String(length=16), nullable=False),
        sa.Column('season_year', sa.Integer(), nullable=True),
        sa.Column('row_count', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('last_ingested_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('notes', sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint('coverage_id'),
        sa.UniqueConstraint('provider', 'dataset', 'league_id', 'season_year', name='uq_coverage')
        )
    if 'ingest_runs' not in existing:
        op.create_table('ingest_runs',
        sa.Column('run_id', sa.String(length=64), nullable=False),
        sa.Column('provider', sa.String(length=64), nullable=False),
        sa.Column('dataset', sa.String(length=64), nullable=False),
        sa.Column('league_id', sa.String(length=16), nullable=False),
        sa.Column('season_year', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('rows_upserted', sa.Integer(), nullable=False),
        sa.Column('rows_skipped', sa.Integer(), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('notes', sa.Text(), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('run_id')
        )
    if 'ingest_runs' not in existing:
        op.create_index(op.f('ix_ingest_runs_provider'), 'ingest_runs', ['provider'], unique=False)
    if 'job_runs' not in existing:
        op.create_table('job_runs',
        sa.Column('run_id', sa.String(length=64), nullable=False),
        sa.Column('job_name', sa.String(length=64), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('summary', sa.JSON(), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('run_id')
        )
    if 'job_runs' not in existing:
        op.create_index(op.f('ix_job_runs_job_name'), 'job_runs', ['job_name'], unique=False)
    if 'market_comparisons' not in existing:
        op.create_table('market_comparisons',
        sa.Column('comparison_id', sa.String(length=64), nullable=False),
        sa.Column('player_id', sa.String(length=64), nullable=False),
        sa.Column('game_id', sa.String(length=64), nullable=False),
        sa.Column('market_id', sa.String(length=64), nullable=False),
        sa.Column('bookmaker_id', sa.String(length=64), nullable=False),
        sa.Column('line', sa.Float(), nullable=True),
        sa.Column('price_american', sa.Integer(), nullable=False),
        sa.Column('forecast_probability', sa.Float(), nullable=False),
        sa.Column('forecast_as_of', sa.DateTime(timezone=True), nullable=False),
        sa.Column('model_version_id', sa.String(length=64), nullable=False),
        sa.Column('actual_outcome', sa.Float(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('comparison_id')
        )
    if 'market_comparisons' not in existing:
        op.create_index(op.f('ix_market_comparisons_game_id'), 'market_comparisons', ['game_id'], unique=False)
    if 'market_comparisons' not in existing:
        op.create_index(op.f('ix_market_comparisons_player_id'), 'market_comparisons', ['player_id'], unique=False)
    if 'markets' not in existing:
        op.create_table('markets',
        sa.Column('market_id', sa.String(length=64), nullable=False),
        sa.Column('sport', sa.String(length=32), nullable=False),
        sa.Column('market_type', sa.String(length=64), nullable=False),
        sa.Column('period', sa.String(length=32), nullable=False),
        sa.Column('stat', sa.String(length=64), nullable=False),
        sa.Column('grading_rule', sa.Text(), nullable=False),
        sa.Column('odds_api_key', sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint('market_id')
        )
    if 'model_health_snapshots' not in existing:
        op.create_table('model_health_snapshots',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('league_id', sa.String(length=16), nullable=False),
        sa.Column('market_id', sa.String(length=64), nullable=False),
        sa.Column('as_of', sa.DateTime(timezone=True), nullable=False),
        sa.Column('brier', sa.Float(), nullable=False),
        sa.Column('log_loss', sa.Float(), nullable=False),
        sa.Column('ece', sa.Float(), nullable=False),
        sa.Column('skill_vs_novig', sa.Float(), nullable=True),
        sa.Column('n', sa.Integer(), nullable=False),
        sa.Column('coverage', sa.Float(), nullable=False),
        sa.Column('freshness_minutes', sa.Float(), nullable=False),
        sa.Column('clv', sa.Float(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint('id')
        )
    if 'model_versions' not in existing:
        op.create_table('model_versions',
        sa.Column('model_version_id', sa.String(length=64), nullable=False),
        sa.Column('code_sha', sa.String(length=64), nullable=False),
        sa.Column('training_cutoff', sa.DateTime(timezone=True), nullable=False),
        sa.Column('data_snapshot', sa.String(length=128), nullable=False),
        sa.Column('hyperparameters', sa.JSON(), nullable=False),
        sa.Column('calibration_version', sa.String(length=32), nullable=False),
        sa.Column('league_id', sa.String(length=16), nullable=False),
        sa.Column('family', sa.String(length=32), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('model_version_id')
        )
    if 'news_articles' not in existing:
        op.create_table('news_articles',
        sa.Column('article_id', sa.String(length=64), nullable=False),
        sa.Column('url', sa.Text(), nullable=False),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('domain', sa.String(length=128), nullable=True),
        sa.Column('language', sa.String(length=16), nullable=True),
        sa.Column('source_country', sa.String(length=8), nullable=True),
        sa.Column('theme', sa.String(length=32), nullable=False),
        sa.Column('league_id', sa.String(length=16), nullable=True),
        sa.Column('seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('query', sa.Text(), nullable=False),
        sa.Column('verified_injury', sa.Boolean(), nullable=False),
        sa.Column('provider', sa.String(length=64), nullable=False),
        sa.Column('provider_record_id', sa.String(length=160), nullable=False),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('effective_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ingested_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('quality_status', sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint('article_id')
        )
    if 'news_articles' not in existing:
        op.create_index(op.f('ix_news_articles_provider'), 'news_articles', ['provider'], unique=False)
    if 'news_articles' not in existing:
        op.create_index(op.f('ix_news_articles_seen_at'), 'news_articles', ['seen_at'], unique=False)
    if 'players' not in existing:
        op.create_table('players',
        sa.Column('player_id', sa.String(length=64), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('birth_date', sa.String(length=16), nullable=True),
        sa.Column('position_group', sa.String(length=16), nullable=False),
        sa.Column('position', sa.String(length=16), nullable=False),
        sa.Column('aliases', sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint('player_id')
        )
    if 'players' not in existing:
        op.create_index(op.f('ix_players_name'), 'players', ['name'], unique=False)
    if 'provider_id_map' not in existing:
        op.create_table('provider_id_map',
        sa.Column('map_id', sa.String(length=64), nullable=False),
        sa.Column('provider', sa.String(length=64), nullable=False),
        sa.Column('entity_type', sa.String(length=32), nullable=False),
        sa.Column('external_id', sa.String(length=160), nullable=False),
        sa.Column('canonical_id', sa.String(length=64), nullable=False),
        sa.Column('confidence', sa.Float(), nullable=False),
        sa.Column('review_status', sa.String(length=32), nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('map_id'),
        sa.UniqueConstraint('provider', 'entity_type', 'external_id', name='uq_provider_entity')
        )
    if 'provider_id_map' not in existing:
        op.create_index('ix_provider_canonical', 'provider_id_map', ['entity_type', 'canonical_id'], unique=False)
    if 'provider_id_map' not in existing:
        op.create_index(op.f('ix_provider_id_map_canonical_id'), 'provider_id_map', ['canonical_id'], unique=False)
    if 'published_picks' not in existing:
        op.create_table('published_picks',
        sa.Column('pick_id', sa.String(length=64), nullable=False),
        sa.Column('game_id', sa.String(length=64), nullable=False),
        sa.Column('league_id', sa.String(length=16), nullable=False),
        sa.Column('market_id', sa.String(length=64), nullable=False),
        sa.Column('published_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('kickoff_utc', sa.DateTime(timezone=True), nullable=False),
        sa.Column('snapshot', sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint('pick_id'),
        sa.UniqueConstraint('game_id', 'market_id', name='uq_published_game_market')
        )
    if 'published_picks' not in existing:
        op.create_index(op.f('ix_published_picks_game_id'), 'published_picks', ['game_id'], unique=False)
    if 'published_picks' not in existing:
        op.create_index(op.f('ix_published_picks_league_id'), 'published_picks', ['league_id'], unique=False)
    if 'published_picks' not in existing:
        op.create_index(op.f('ix_published_picks_market_id'), 'published_picks', ['market_id'], unique=False)
    if 'published_stats' not in existing:
        op.create_table('published_stats',
        sa.Column('key', sa.String(length=200), nullable=False),
        sa.Column('game_id', sa.String(length=64), nullable=False),
        sa.Column('player_id', sa.String(length=64), nullable=False),
        sa.Column('market_id', sa.String(length=64), nullable=False),
        sa.Column('actual', sa.Float(), nullable=False),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('source', sa.String(length=128), nullable=False),
        sa.PrimaryKeyConstraint('key')
        )
    if 'published_stats' not in existing:
        op.create_index(op.f('ix_published_stats_game_id'), 'published_stats', ['game_id'], unique=False)
    if 'raw_snapshots' not in existing:
        op.create_table('raw_snapshots',
        sa.Column('snapshot_id', sa.String(length=64), nullable=False),
        sa.Column('provider', sa.String(length=64), nullable=False),
        sa.Column('endpoint', sa.String(length=128), nullable=False),
        sa.Column('provider_record_id', sa.String(length=160), nullable=False),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ingested_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('license_name', sa.String(length=128), nullable=False),
        sa.Column('credit_cost', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('snapshot_id')
        )
    if 'raw_snapshots' not in existing:
        op.create_index(op.f('ix_raw_snapshots_provider'), 'raw_snapshots', ['provider'], unique=False)
    if 'shadow_predictions' not in existing:
        op.create_table('shadow_predictions',
        sa.Column('shadow_id', sa.String(length=64), nullable=False),
        sa.Column('player_id', sa.String(length=64), nullable=False),
        sa.Column('game_id', sa.String(length=64), nullable=False),
        sa.Column('league_id', sa.String(length=16), nullable=False),
        sa.Column('market_id', sa.String(length=64), nullable=False),
        sa.Column('outcome_id', sa.String(length=64), nullable=False),
        sa.Column('bookmaker_id', sa.String(length=64), nullable=False),
        sa.Column('line', sa.Float(), nullable=True),
        sa.Column('side', sa.String(length=16), nullable=False),
        sa.Column('price_american', sa.Integer(), nullable=False),
        sa.Column('odds_tick_id', sa.String(length=64), nullable=True),
        sa.Column('forecast_probability', sa.Float(), nullable=False),
        sa.Column('market_implied_raw', sa.Float(), nullable=False),
        sa.Column('market_implied_novig', sa.Float(), nullable=True),
        sa.Column('expected_value', sa.Float(), nullable=True),
        sa.Column('model_version_id', sa.String(length=64), nullable=False),
        sa.Column('forecast_as_of', sa.DateTime(timezone=True), nullable=False),
        sa.Column('data_quality_status', sa.String(length=32), nullable=False),
        sa.Column('abstain_reasons', sa.JSON(), nullable=False),
        sa.Column('recommendation_status', sa.String(length=16), nullable=False),
        sa.Column('components', sa.JSON(), nullable=False),
        sa.Column('research_only', sa.Boolean(), nullable=False),
        sa.Column('play_enabled', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('shadow_id')
        )
    if 'shadow_predictions' not in existing:
        op.create_index('ix_shadow_dedupe', 'shadow_predictions', ['outcome_id', 'model_version_id', 'odds_tick_id'], unique=False)
    if 'shadow_predictions' not in existing:
        op.create_index(op.f('ix_shadow_predictions_forecast_as_of'), 'shadow_predictions', ['forecast_as_of'], unique=False)
    if 'shadow_predictions' not in existing:
        op.create_index(op.f('ix_shadow_predictions_game_id'), 'shadow_predictions', ['game_id'], unique=False)
    if 'shadow_predictions' not in existing:
        op.create_index(op.f('ix_shadow_predictions_league_id'), 'shadow_predictions', ['league_id'], unique=False)
    if 'shadow_predictions' not in existing:
        op.create_index(op.f('ix_shadow_predictions_market_id'), 'shadow_predictions', ['market_id'], unique=False)
    if 'shadow_predictions' not in existing:
        op.create_index(op.f('ix_shadow_predictions_model_version_id'), 'shadow_predictions', ['model_version_id'], unique=False)
    if 'shadow_predictions' not in existing:
        op.create_index(op.f('ix_shadow_predictions_outcome_id'), 'shadow_predictions', ['outcome_id'], unique=False)
    if 'shadow_predictions' not in existing:
        op.create_index(op.f('ix_shadow_predictions_player_id'), 'shadow_predictions', ['player_id'], unique=False)
    if 'sports' not in existing:
        op.create_table('sports',
        sa.Column('sport_id', sa.String(length=32), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint('sport_id')
        )
    if 'venues' not in existing:
        op.create_table('venues',
        sa.Column('venue_id', sa.String(length=64), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('lat', sa.Float(), nullable=False),
        sa.Column('lon', sa.Float(), nullable=False),
        sa.Column('altitude_m', sa.Float(), nullable=False),
        sa.Column('roof_type', sa.String(length=32), nullable=False),
        sa.Column('surface', sa.String(length=32), nullable=False),
        sa.Column('orientation_deg', sa.Float(), nullable=True),
        sa.Column('timezone', sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint('venue_id')
        )
    if 'ats_settlements' not in existing:
        op.create_table('ats_settlements',
        sa.Column('settlement_id', sa.String(length=64), nullable=False),
        sa.Column('ats_id', sa.String(length=64), nullable=False),
        sa.Column('result', sa.String(length=16), nullable=False),
        sa.Column('home_score', sa.Integer(), nullable=True),
        sa.Column('away_score', sa.Integer(), nullable=True),
        sa.Column('actual_margin', sa.Float(), nullable=True),
        sa.Column('settled_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('official_source', sa.String(length=128), nullable=False),
        sa.Column('void_reason', sa.Text(), nullable=True),
        sa.Column('evidence', sa.JSON(), nullable=False),
        sa.Column('flat_stake_return', sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(['ats_id'], ['ats_predictions.ats_id'], ),
        sa.PrimaryKeyConstraint('settlement_id'),
        sa.UniqueConstraint('ats_id')
        )
    if 'auth_sessions' not in existing:
        op.create_table('auth_sessions',
        sa.Column('session_id', sa.String(length=64), nullable=False),
        sa.Column('user_id', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['account_users.user_id'], ),
        sa.PrimaryKeyConstraint('session_id')
        )
    if 'auth_sessions' not in existing:
        op.create_index(op.f('ix_auth_sessions_user_id'), 'auth_sessions', ['user_id'], unique=False)
    if 'backtest_predictions' not in existing:
        op.create_table('backtest_predictions',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('run_id', sa.String(length=64), nullable=False),
        sa.Column('game_id', sa.String(length=64), nullable=False),
        sa.Column('player_id', sa.String(length=64), nullable=True),
        sa.Column('as_of', sa.DateTime(timezone=True), nullable=False),
        sa.Column('train_cutoff', sa.DateTime(timezone=True), nullable=False),
        sa.Column('p', sa.Float(), nullable=False),
        sa.Column('y', sa.Float(), nullable=False),
        sa.Column('components', sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(['run_id'], ['backtest_runs.run_id'], ),
        sa.PrimaryKeyConstraint('id')
        )
    if 'backtest_predictions' not in existing:
        op.create_index(op.f('ix_backtest_predictions_game_id'), 'backtest_predictions', ['game_id'], unique=False)
    if 'backtest_predictions' not in existing:
        op.create_index(op.f('ix_backtest_predictions_run_id'), 'backtest_predictions', ['run_id'], unique=False)
    if 'billing_cursors' not in existing:
        op.create_table('billing_cursors',
        sa.Column('user_id', sa.String(length=64), nullable=False),
        sa.Column('event_created', sa.Integer(), nullable=False),
        sa.Column('terminal', sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['account_users.user_id'], ),
        sa.PrimaryKeyConstraint('user_id')
        )
    if 'leagues' not in existing:
        op.create_table('leagues',
        sa.Column('league_id', sa.String(length=16), nullable=False),
        sa.Column('sport_id', sa.String(length=32), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('season_type', sa.String(length=32), nullable=False),
        sa.Column('classification', sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(['sport_id'], ['sports.sport_id'], ),
        sa.PrimaryKeyConstraint('league_id')
        )
    if 'ngs_weeks' not in existing:
        op.create_table('ngs_weeks',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('season_year', sa.Integer(), nullable=False),
        sa.Column('week', sa.Integer(), nullable=False),
        sa.Column('season_type', sa.String(length=8), nullable=False),
        sa.Column('player_id', sa.String(length=64), nullable=False),
        sa.Column('team_id', sa.String(length=64), nullable=True),
        sa.Column('stat_type', sa.String(length=16), nullable=False),
        sa.Column('metrics', sa.JSON(), nullable=False),
        sa.Column('provider', sa.String(length=64), nullable=False),
        sa.Column('provider_record_id', sa.String(length=160), nullable=False),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('effective_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ingested_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('quality_status', sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(['player_id'], ['players.player_id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('season_year', 'week', 'season_type', 'player_id', 'stat_type', 'provider', name='uq_ngs_week')
        )
    if 'ngs_weeks' not in existing:
        op.create_index(op.f('ix_ngs_weeks_player_id'), 'ngs_weeks', ['player_id'], unique=False)
    if 'ngs_weeks' not in existing:
        op.create_index(op.f('ix_ngs_weeks_provider'), 'ngs_weeks', ['provider'], unique=False)
    if 'ngs_weeks' not in existing:
        op.create_index(op.f('ix_ngs_weeks_season_year'), 'ngs_weeks', ['season_year'], unique=False)
    if 'password_resets' not in existing:
        op.create_table('password_resets',
        sa.Column('token_id', sa.String(length=64), nullable=False),
        sa.Column('user_id', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['account_users.user_id'], ),
        sa.PrimaryKeyConstraint('token_id')
        )
    if 'password_resets' not in existing:
        op.create_index(op.f('ix_password_resets_user_id'), 'password_resets', ['user_id'], unique=False)
    if 'player_analytics' not in existing:
        op.create_table('player_analytics',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('league_id', sa.String(length=16), nullable=False),
        sa.Column('player_id', sa.String(length=64), nullable=False),
        sa.Column('team_id', sa.String(length=64), nullable=True),
        sa.Column('season_year', sa.Integer(), nullable=False),
        sa.Column('week', sa.Integer(), nullable=False),
        sa.Column('game_id', sa.String(length=64), nullable=False),
        sa.Column('grain', sa.String(length=16), nullable=False),
        sa.Column('split', sa.String(length=16), nullable=False),
        sa.Column('metrics', sa.JSON(), nullable=False),
        sa.Column('coverage', sa.JSON(), nullable=False),
        sa.Column('sample_size', sa.Integer(), nullable=False),
        sa.Column('source', sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(['player_id'], ['players.player_id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('league_id', 'player_id', 'grain', 'split', 'season_year', 'week', 'game_id', name='uq_player_analytics')
        )
    if 'player_analytics' not in existing:
        op.create_index(op.f('ix_player_analytics_league_id'), 'player_analytics', ['league_id'], unique=False)
    if 'player_analytics' not in existing:
        op.create_index(op.f('ix_player_analytics_player_id'), 'player_analytics', ['player_id'], unique=False)
    if 'published_corrections' not in existing:
        op.create_table('published_corrections',
        sa.Column('correction_id', sa.String(length=64), nullable=False),
        sa.Column('pick_id', sa.String(length=64), nullable=False),
        sa.Column('result', sa.String(length=16), nullable=False),
        sa.Column('actual', sa.Float(), nullable=True),
        sa.Column('units', sa.Float(), nullable=False),
        sa.Column('settled_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('evidence', sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(['pick_id'], ['published_picks.pick_id'], ),
        sa.PrimaryKeyConstraint('correction_id')
        )
    if 'published_corrections' not in existing:
        op.create_index(op.f('ix_published_corrections_pick_id'), 'published_corrections', ['pick_id'], unique=False)
    if 'published_results' not in existing:
        op.create_table('published_results',
        sa.Column('pick_id', sa.String(length=64), nullable=False),
        sa.Column('result', sa.String(length=16), nullable=False),
        sa.Column('actual', sa.Float(), nullable=True),
        sa.Column('units', sa.Float(), nullable=False),
        sa.Column('settled_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('evidence', sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(['pick_id'], ['published_picks.pick_id'], ),
        sa.PrimaryKeyConstraint('pick_id')
        )
    if 'shadow_settlements' not in existing:
        op.create_table('shadow_settlements',
        sa.Column('settlement_id', sa.String(length=64), nullable=False),
        sa.Column('shadow_id', sa.String(length=64), nullable=False),
        sa.Column('result', sa.String(length=16), nullable=False),
        sa.Column('actual_value', sa.Float(), nullable=True),
        sa.Column('settled_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('official_source', sa.String(length=128), nullable=False),
        sa.Column('void_reason', sa.Text(), nullable=True),
        sa.Column('evidence', sa.JSON(), nullable=False),
        sa.Column('flat_stake_return', sa.Float(), nullable=True),
        sa.Column('closing_price_american', sa.Integer(), nullable=True),
        sa.Column('closing_line_value', sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(['shadow_id'], ['shadow_predictions.shadow_id'], ),
        sa.PrimaryKeyConstraint('settlement_id'),
        sa.UniqueConstraint('shadow_id')
        )
    if 'seasons' not in existing:
        op.create_table('seasons',
        sa.Column('season_id', sa.String(length=32), nullable=False),
        sa.Column('league_id', sa.String(length=16), nullable=False),
        sa.Column('year', sa.Integer(), nullable=False),
        sa.Column('start_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('end_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['league_id'], ['leagues.league_id'], ),
        sa.PrimaryKeyConstraint('season_id')
        )
    if 'teams' not in existing:
        op.create_table('teams',
        sa.Column('team_id', sa.String(length=64), nullable=False),
        sa.Column('league_id', sa.String(length=16), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('abbreviation', sa.String(length=16), nullable=False),
        sa.Column('conference', sa.String(length=64), nullable=True),
        sa.Column('aliases', sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(['league_id'], ['leagues.league_id'], ),
        sa.PrimaryKeyConstraint('team_id')
        )
    if 'teams' not in existing:
        op.create_index(op.f('ix_teams_league_id'), 'teams', ['league_id'], unique=False)
    if 'depth_chart_weeks' not in existing:
        op.create_table('depth_chart_weeks',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('season_year', sa.Integer(), nullable=False),
        sa.Column('week', sa.Integer(), nullable=False),
        sa.Column('game_type', sa.String(length=8), nullable=False),
        sa.Column('team_id', sa.String(length=64), nullable=False),
        sa.Column('player_id', sa.String(length=64), nullable=False),
        sa.Column('formation', sa.String(length=32), nullable=True),
        sa.Column('position', sa.String(length=16), nullable=True),
        sa.Column('depth_position', sa.String(length=16), nullable=True),
        sa.Column('depth_rank', sa.Integer(), nullable=True),
        sa.Column('provider', sa.String(length=64), nullable=False),
        sa.Column('provider_record_id', sa.String(length=160), nullable=False),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('effective_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ingested_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('quality_status', sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(['player_id'], ['players.player_id'], ),
        sa.ForeignKeyConstraint(['team_id'], ['teams.team_id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('season_year', 'week', 'game_type', 'team_id', 'player_id', 'formation', 'depth_position', 'provider', name='uq_depth_week')
        )
    if 'depth_chart_weeks' not in existing:
        op.create_index(op.f('ix_depth_chart_weeks_player_id'), 'depth_chart_weeks', ['player_id'], unique=False)
    if 'depth_chart_weeks' not in existing:
        op.create_index(op.f('ix_depth_chart_weeks_provider'), 'depth_chart_weeks', ['provider'], unique=False)
    if 'depth_chart_weeks' not in existing:
        op.create_index(op.f('ix_depth_chart_weeks_season_year'), 'depth_chart_weeks', ['season_year'], unique=False)
    if 'depth_chart_weeks' not in existing:
        op.create_index(op.f('ix_depth_chart_weeks_team_id'), 'depth_chart_weeks', ['team_id'], unique=False)
    if 'games' not in existing:
        op.create_table('games',
        sa.Column('game_id', sa.String(length=64), nullable=False),
        sa.Column('season_id', sa.String(length=32), nullable=False),
        sa.Column('league_id', sa.String(length=16), nullable=False),
        sa.Column('home_team_id', sa.String(length=64), nullable=False),
        sa.Column('away_team_id', sa.String(length=64), nullable=False),
        sa.Column('kickoff_utc', sa.DateTime(timezone=True), nullable=False),
        sa.Column('venue_id', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('week', sa.Integer(), nullable=True),
        sa.Column('neutral_site', sa.Boolean(), nullable=False),
        sa.Column('data_tier', sa.String(length=16), nullable=False),
        sa.Column('home_score', sa.Integer(), nullable=True),
        sa.Column('away_score', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['away_team_id'], ['teams.team_id'], ),
        sa.ForeignKeyConstraint(['home_team_id'], ['teams.team_id'], ),
        sa.ForeignKeyConstraint(['league_id'], ['leagues.league_id'], ),
        sa.ForeignKeyConstraint(['season_id'], ['seasons.season_id'], ),
        sa.ForeignKeyConstraint(['venue_id'], ['venues.venue_id'], ),
        sa.PrimaryKeyConstraint('game_id')
        )
    if 'games' not in existing:
        op.create_index(op.f('ix_games_kickoff_utc'), 'games', ['kickoff_utc'], unique=False)
    if 'games' not in existing:
        op.create_index(op.f('ix_games_league_id'), 'games', ['league_id'], unique=False)
    if 'roster_weeks' not in existing:
        op.create_table('roster_weeks',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('season_year', sa.Integer(), nullable=False),
        sa.Column('week', sa.Integer(), nullable=False),
        sa.Column('game_type', sa.String(length=8), nullable=False),
        sa.Column('team_id', sa.String(length=64), nullable=False),
        sa.Column('player_id', sa.String(length=64), nullable=False),
        sa.Column('position', sa.String(length=16), nullable=True),
        sa.Column('depth_chart_position', sa.String(length=16), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=True),
        sa.Column('jersey', sa.String(length=8), nullable=True),
        sa.Column('provider', sa.String(length=64), nullable=False),
        sa.Column('provider_record_id', sa.String(length=160), nullable=False),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('effective_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ingested_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('quality_status', sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(['player_id'], ['players.player_id'], ),
        sa.ForeignKeyConstraint(['team_id'], ['teams.team_id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('season_year', 'week', 'game_type', 'team_id', 'player_id', 'provider', name='uq_roster_week')
        )
    if 'roster_weeks' not in existing:
        op.create_index(op.f('ix_roster_weeks_player_id'), 'roster_weeks', ['player_id'], unique=False)
    if 'roster_weeks' not in existing:
        op.create_index(op.f('ix_roster_weeks_provider'), 'roster_weeks', ['provider'], unique=False)
    if 'roster_weeks' not in existing:
        op.create_index(op.f('ix_roster_weeks_season_year'), 'roster_weeks', ['season_year'], unique=False)
    if 'roster_weeks' not in existing:
        op.create_index(op.f('ix_roster_weeks_team_id'), 'roster_weeks', ['team_id'], unique=False)
    if 'rosters' not in existing:
        op.create_table('rosters',
        sa.Column('roster_id', sa.String(length=64), nullable=False),
        sa.Column('team_id', sa.String(length=64), nullable=False),
        sa.Column('player_id', sa.String(length=64), nullable=False),
        sa.Column('valid_from', sa.DateTime(timezone=True), nullable=False),
        sa.Column('valid_to', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('jersey', sa.String(length=8), nullable=True),
        sa.ForeignKeyConstraint(['player_id'], ['players.player_id'], ),
        sa.ForeignKeyConstraint(['team_id'], ['teams.team_id'], ),
        sa.PrimaryKeyConstraint('roster_id')
        )
    if 'rosters' not in existing:
        op.create_index(op.f('ix_rosters_player_id'), 'rosters', ['player_id'], unique=False)
    if 'rosters' not in existing:
        op.create_index(op.f('ix_rosters_team_id'), 'rosters', ['team_id'], unique=False)
    if 'team_analytics' not in existing:
        op.create_table('team_analytics',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('league_id', sa.String(length=16), nullable=False),
        sa.Column('team_id', sa.String(length=64), nullable=False),
        sa.Column('season_year', sa.Integer(), nullable=False),
        sa.Column('week', sa.Integer(), nullable=False),
        sa.Column('game_id', sa.String(length=64), nullable=False),
        sa.Column('grain', sa.String(length=16), nullable=False),
        sa.Column('split', sa.String(length=16), nullable=False),
        sa.Column('metrics', sa.JSON(), nullable=False),
        sa.Column('coverage', sa.JSON(), nullable=False),
        sa.Column('sample_size', sa.Integer(), nullable=False),
        sa.Column('source', sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(['team_id'], ['teams.team_id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('league_id', 'team_id', 'grain', 'split', 'season_year', 'week', 'game_id', name='uq_team_analytics')
        )
    if 'team_analytics' not in existing:
        op.create_index(op.f('ix_team_analytics_league_id'), 'team_analytics', ['league_id'], unique=False)
    if 'team_analytics' not in existing:
        op.create_index(op.f('ix_team_analytics_team_id'), 'team_analytics', ['team_id'], unique=False)
    if 'ai_runs' not in existing:
        op.create_table('ai_runs',
        sa.Column('run_id', sa.String(length=64), nullable=False),
        sa.Column('kind', sa.String(length=32), nullable=False),
        sa.Column('game_id', sa.String(length=64), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('as_of', sa.DateTime(timezone=True), nullable=False),
        sa.Column('input_hash', sa.String(length=64), nullable=False),
        sa.Column('fingerprint', sa.String(length=64), nullable=False),
        sa.Column('model', sa.String(length=64), nullable=False),
        sa.Column('prompt_version', sa.String(length=32), nullable=False),
        sa.Column('token_input', sa.Integer(), nullable=False),
        sa.Column('token_output', sa.Integer(), nullable=False),
        sa.Column('cost_usd', sa.Float(), nullable=False),
        sa.Column('rejection_reasons', sa.JSON(), nullable=False),
        sa.Column('output', sa.JSON(), nullable=False),
        sa.Column('data_tier', sa.String(length=16), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['game_id'], ['games.game_id'], ),
        sa.PrimaryKeyConstraint('run_id'),
        sa.UniqueConstraint('kind', 'input_hash', name='uq_ai_run_hash')
        )
    if 'ai_runs' not in existing:
        op.create_index(op.f('ix_ai_runs_game_id'), 'ai_runs', ['game_id'], unique=False)
    if 'ai_runs' not in existing:
        op.create_index(op.f('ix_ai_runs_kind'), 'ai_runs', ['kind'], unique=False)
    if 'closing_lines' not in existing:
        op.create_table('closing_lines',
        sa.Column('line_id', sa.String(length=64), nullable=False),
        sa.Column('game_id', sa.String(length=64), nullable=False),
        sa.Column('market_id', sa.String(length=64), nullable=False),
        sa.Column('home_line', sa.Float(), nullable=True),
        sa.Column('away_line', sa.Float(), nullable=True),
        sa.Column('home_price_american', sa.Integer(), nullable=True),
        sa.Column('away_price_american', sa.Integer(), nullable=True),
        sa.Column('source', sa.String(length=64), nullable=False),
        sa.Column('captured_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('provider', sa.String(length=64), nullable=False),
        sa.Column('provider_record_id', sa.String(length=160), nullable=False),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('effective_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ingested_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('quality_status', sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(['game_id'], ['games.game_id'], ),
        sa.PrimaryKeyConstraint('line_id'),
        sa.UniqueConstraint('game_id', 'market_id', 'source', 'provider', name='uq_closing_line')
        )
    if 'closing_lines' not in existing:
        op.create_index(op.f('ix_closing_lines_game_id'), 'closing_lines', ['game_id'], unique=False)
    if 'closing_lines' not in existing:
        op.create_index(op.f('ix_closing_lines_market_id'), 'closing_lines', ['market_id'], unique=False)
    if 'closing_lines' not in existing:
        op.create_index(op.f('ix_closing_lines_provider'), 'closing_lines', ['provider'], unique=False)
    if 'depth_chart_snapshots' not in existing:
        op.create_table('depth_chart_snapshots',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('game_id', sa.String(length=64), nullable=False),
        sa.Column('player_id', sa.String(length=64), nullable=False),
        sa.Column('position', sa.String(length=16), nullable=False),
        sa.Column('rank', sa.Integer(), nullable=False),
        sa.Column('snapshot_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('provider', sa.String(length=64), nullable=False),
        sa.Column('provider_record_id', sa.String(length=160), nullable=False),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('effective_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ingested_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('quality_status', sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(['game_id'], ['games.game_id'], ),
        sa.ForeignKeyConstraint(['player_id'], ['players.player_id'], ),
        sa.PrimaryKeyConstraint('id')
        )
    if 'depth_chart_snapshots' not in existing:
        op.create_index(op.f('ix_depth_chart_snapshots_game_id'), 'depth_chart_snapshots', ['game_id'], unique=False)
    if 'depth_chart_snapshots' not in existing:
        op.create_index(op.f('ix_depth_chart_snapshots_provider'), 'depth_chart_snapshots', ['provider'], unique=False)
    if 'desk_decisions' not in existing:
        op.create_table('desk_decisions',
        sa.Column('decision_id', sa.String(length=64), nullable=False),
        sa.Column('game_id', sa.String(length=64), nullable=False),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(['game_id'], ['games.game_id'], ),
        sa.PrimaryKeyConstraint('decision_id')
        )
    if 'desk_decisions' not in existing:
        op.create_index('ix_desk_game_time', 'desk_decisions', ['game_id', 'decided_at'], unique=False)
    if 'features' not in existing:
        op.create_table('features',
        sa.Column('feature_id', sa.String(length=64), nullable=False),
        sa.Column('game_id', sa.String(length=64), nullable=False),
        sa.Column('player_id', sa.String(length=64), nullable=True),
        sa.Column('feature_version', sa.String(length=32), nullable=False),
        sa.Column('as_of', sa.DateTime(timezone=True), nullable=False),
        sa.Column('feature_blob', sa.JSON(), nullable=False),
        sa.Column('coverage_mask', sa.JSON(), nullable=False),
        sa.Column('sample_size', sa.Integer(), nullable=False),
        sa.Column('missingness', sa.Float(), nullable=False),
        sa.Column('source', sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(['game_id'], ['games.game_id'], ),
        sa.ForeignKeyConstraint(['player_id'], ['players.player_id'], ),
        sa.PrimaryKeyConstraint('feature_id')
        )
    if 'features' not in existing:
        op.create_index(op.f('ix_features_as_of'), 'features', ['as_of'], unique=False)
    if 'features' not in existing:
        op.create_index(op.f('ix_features_game_id'), 'features', ['game_id'], unique=False)
    if 'injury_events' not in existing:
        op.create_table('injury_events',
        sa.Column('injury_event_id', sa.String(length=64), nullable=False),
        sa.Column('game_id', sa.String(length=64), nullable=True),
        sa.Column('player_id', sa.String(length=64), nullable=False),
        sa.Column('designation', sa.String(length=32), nullable=False),
        sa.Column('practice_status', sa.String(length=32), nullable=True),
        sa.Column('reported_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('source', sa.String(length=128), nullable=False),
        sa.Column('confidence', sa.Float(), nullable=False),
        sa.Column('coverage_complete', sa.Boolean(), nullable=False),
        sa.Column('provider', sa.String(length=64), nullable=False),
        sa.Column('provider_record_id', sa.String(length=160), nullable=False),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('effective_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ingested_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('quality_status', sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(['game_id'], ['games.game_id'], ),
        sa.ForeignKeyConstraint(['player_id'], ['players.player_id'], ),
        sa.PrimaryKeyConstraint('injury_event_id')
        )
    if 'injury_events' not in existing:
        op.create_index(op.f('ix_injury_events_player_id'), 'injury_events', ['player_id'], unique=False)
    if 'injury_events' not in existing:
        op.create_index(op.f('ix_injury_events_provider'), 'injury_events', ['provider'], unique=False)
    if 'nflverse_game_context' not in existing:
        op.create_table('nflverse_game_context',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('game_id', sa.String(length=64), nullable=False),
        sa.Column('entity_id', sa.String(length=64), nullable=False),
        sa.Column('team_id', sa.String(length=64), nullable=False),
        sa.Column('dataset', sa.String(length=32), nullable=False),
        sa.Column('metrics', sa.JSON(), nullable=False),
        sa.Column('provider', sa.String(length=64), nullable=False),
        sa.Column('provider_record_id', sa.String(length=160), nullable=False),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('effective_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ingested_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('quality_status', sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(['game_id'], ['games.game_id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('game_id', 'entity_id', 'dataset', name='uq_nflverse_game_context')
        )
    if 'nflverse_game_context' not in existing:
        op.create_index(op.f('ix_nflverse_game_context_entity_id'), 'nflverse_game_context', ['entity_id'], unique=False)
    if 'nflverse_game_context' not in existing:
        op.create_index(op.f('ix_nflverse_game_context_game_id'), 'nflverse_game_context', ['game_id'], unique=False)
    if 'nflverse_game_context' not in existing:
        op.create_index(op.f('ix_nflverse_game_context_provider'), 'nflverse_game_context', ['provider'], unique=False)
    if 'outcomes' not in existing:
        op.create_table('outcomes',
        sa.Column('outcome_id', sa.String(length=64), nullable=False),
        sa.Column('market_id', sa.String(length=64), nullable=False),
        sa.Column('game_id', sa.String(length=64), nullable=False),
        sa.Column('player_id', sa.String(length=64), nullable=True),
        sa.Column('side', sa.String(length=16), nullable=False),
        sa.Column('line', sa.Float(), nullable=True),
        sa.Column('description', sa.String(length=256), nullable=False),
        sa.ForeignKeyConstraint(['game_id'], ['games.game_id'], ),
        sa.ForeignKeyConstraint(['market_id'], ['markets.market_id'], ),
        sa.ForeignKeyConstraint(['player_id'], ['players.player_id'], ),
        sa.PrimaryKeyConstraint('outcome_id')
        )
    if 'outcomes' not in existing:
        op.create_index(op.f('ix_outcomes_game_id'), 'outcomes', ['game_id'], unique=False)
    if 'outcomes' not in existing:
        op.create_index(op.f('ix_outcomes_market_id'), 'outcomes', ['market_id'], unique=False)
    if 'outcomes' not in existing:
        op.create_index(op.f('ix_outcomes_player_id'), 'outcomes', ['player_id'], unique=False)
    if 'participation_player_games' not in existing:
        op.create_table('participation_player_games',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('game_id', sa.String(length=64), nullable=False),
        sa.Column('player_id', sa.String(length=64), nullable=False),
        sa.Column('offense_snaps', sa.Integer(), nullable=False),
        sa.Column('defense_snaps', sa.Integer(), nullable=False),
        sa.Column('offense_plays', sa.Integer(), nullable=False),
        sa.Column('defense_plays', sa.Integer(), nullable=False),
        sa.Column('provider', sa.String(length=64), nullable=False),
        sa.Column('provider_record_id', sa.String(length=160), nullable=False),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('effective_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ingested_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('quality_status', sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(['game_id'], ['games.game_id'], ),
        sa.ForeignKeyConstraint(['player_id'], ['players.player_id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('game_id', 'player_id', 'provider', name='uq_participation_player_game')
        )
    if 'participation_player_games' not in existing:
        op.create_index(op.f('ix_participation_player_games_game_id'), 'participation_player_games', ['game_id'], unique=False)
    if 'participation_player_games' not in existing:
        op.create_index(op.f('ix_participation_player_games_player_id'), 'participation_player_games', ['player_id'], unique=False)
    if 'participation_player_games' not in existing:
        op.create_index(op.f('ix_participation_player_games_provider'), 'participation_player_games', ['provider'], unique=False)
    if 'participation_plays' not in existing:
        op.create_table('participation_plays',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('game_id', sa.String(length=64), nullable=False),
        sa.Column('play_id', sa.String(length=32), nullable=False),
        sa.Column('possession_team', sa.String(length=8), nullable=True),
        sa.Column('offense_formation', sa.String(length=32), nullable=True),
        sa.Column('offense_personnel', sa.Text(), nullable=True),
        sa.Column('defense_personnel', sa.Text(), nullable=True),
        sa.Column('defenders_in_box', sa.Float(), nullable=True),
        sa.Column('number_of_pass_rushers', sa.Float(), nullable=True),
        sa.Column('n_offense', sa.Integer(), nullable=True),
        sa.Column('n_defense', sa.Integer(), nullable=True),
        sa.Column('route', sa.String(length=64), nullable=True),
        sa.Column('defense_man_zone_type', sa.String(length=32), nullable=True),
        sa.Column('defense_coverage_type', sa.String(length=32), nullable=True),
        sa.Column('time_to_throw', sa.Float(), nullable=True),
        sa.Column('was_pressure', sa.Boolean(), nullable=True),
        sa.Column('ngs_air_yards', sa.Float(), nullable=True),
        sa.Column('offense_players', sa.Text(), nullable=True),
        sa.Column('defense_players', sa.Text(), nullable=True),
        sa.Column('provider', sa.String(length=64), nullable=False),
        sa.Column('provider_record_id', sa.String(length=160), nullable=False),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('effective_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ingested_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('quality_status', sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(['game_id'], ['games.game_id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('game_id', 'play_id', 'provider', name='uq_participation_play')
        )
    if 'participation_plays' not in existing:
        op.create_index(op.f('ix_participation_plays_game_id'), 'participation_plays', ['game_id'], unique=False)
    if 'participation_plays' not in existing:
        op.create_index(op.f('ix_participation_plays_provider'), 'participation_plays', ['provider'], unique=False)
    if 'player_game_stats' not in existing:
        op.create_table('player_game_stats',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('game_id', sa.String(length=64), nullable=False),
        sa.Column('player_id', sa.String(length=64), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('routes', sa.Integer(), nullable=False),
        sa.Column('targets', sa.Integer(), nullable=False),
        sa.Column('carries', sa.Integer(), nullable=False),
        sa.Column('receptions', sa.Integer(), nullable=False),
        sa.Column('rush_yards', sa.Float(), nullable=False),
        sa.Column('rec_yards', sa.Float(), nullable=False),
        sa.Column('rush_td', sa.Integer(), nullable=False),
        sa.Column('rec_td', sa.Integer(), nullable=False),
        sa.Column('pass_td', sa.Integer(), nullable=False),
        sa.Column('rz_targets', sa.Integer(), nullable=False),
        sa.Column('rz_carries', sa.Integer(), nullable=False),
        sa.Column('gl_carries', sa.Integer(), nullable=False),
        sa.Column('team_id', sa.String(length=64), nullable=True),
        sa.Column('provider', sa.String(length=64), nullable=False),
        sa.Column('provider_record_id', sa.String(length=160), nullable=False),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('effective_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ingested_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('quality_status', sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(['game_id'], ['games.game_id'], ),
        sa.ForeignKeyConstraint(['player_id'], ['players.player_id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('game_id', 'player_id', 'provider', 'revision', name='uq_pgs')
        )
    if 'player_game_stats' not in existing:
        op.create_index(op.f('ix_player_game_stats_game_id'), 'player_game_stats', ['game_id'], unique=False)
    if 'player_game_stats' not in existing:
        op.create_index(op.f('ix_player_game_stats_player_id'), 'player_game_stats', ['player_id'], unique=False)
    if 'player_game_stats' not in existing:
        op.create_index(op.f('ix_player_game_stats_provider'), 'player_game_stats', ['provider'], unique=False)
    if 'player_game_stats' not in existing:
        op.create_index(op.f('ix_player_game_stats_team_id'), 'player_game_stats', ['team_id'], unique=False)
    if 'player_play_participation' not in existing:
        op.create_table('player_play_participation',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('game_id', sa.String(length=64), nullable=False),
        sa.Column('play_id', sa.String(length=64), nullable=False),
        sa.Column('player_id', sa.String(length=64), nullable=False),
        sa.Column('role', sa.String(length=32), nullable=False),
        sa.Column('route', sa.String(length=32), nullable=True),
        sa.Column('target', sa.Boolean(), nullable=False),
        sa.Column('carry', sa.Boolean(), nullable=False),
        sa.Column('snap', sa.Boolean(), nullable=False),
        sa.Column('coverage_assignment', sa.String(length=64), nullable=True),
        sa.Column('provider', sa.String(length=64), nullable=False),
        sa.Column('provider_record_id', sa.String(length=160), nullable=False),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('effective_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ingested_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('quality_status', sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(['game_id'], ['games.game_id'], ),
        sa.ForeignKeyConstraint(['player_id'], ['players.player_id'], ),
        sa.PrimaryKeyConstraint('id')
        )
    if 'player_play_participation' not in existing:
        op.create_index(op.f('ix_player_play_participation_provider'), 'player_play_participation', ['provider'], unique=False)
    if 'plays' not in existing:
        op.create_table('plays',
        sa.Column('play_pk', sa.String(length=64), nullable=False),
        sa.Column('game_id', sa.String(length=64), nullable=False),
        sa.Column('play_id', sa.String(length=64), nullable=False),
        sa.Column('period', sa.Integer(), nullable=False),
        sa.Column('clock_seconds', sa.Integer(), nullable=False),
        sa.Column('down', sa.Integer(), nullable=True),
        sa.Column('distance', sa.Float(), nullable=True),
        sa.Column('yardline', sa.Integer(), nullable=True),
        sa.Column('offense_id', sa.String(length=64), nullable=False),
        sa.Column('defense_id', sa.String(length=64), nullable=False),
        sa.Column('play_type', sa.String(length=64), nullable=False),
        sa.Column('yards', sa.Float(), nullable=False),
        sa.Column('epa', sa.Float(), nullable=True),
        sa.Column('ppa', sa.Float(), nullable=True),
        sa.Column('wp', sa.Float(), nullable=True),
        sa.Column('success', sa.Boolean(), nullable=True),
        sa.Column('is_pass', sa.Boolean(), nullable=False),
        sa.Column('is_rush', sa.Boolean(), nullable=False),
        sa.Column('is_td', sa.Boolean(), nullable=False),
        sa.Column('is_turnover', sa.Boolean(), nullable=False),
        sa.Column('is_red_zone', sa.Boolean(), nullable=False),
        sa.Column('is_goal_line', sa.Boolean(), nullable=False),
        sa.Column('is_scrimmage', sa.Boolean(), nullable=False),
        sa.Column('first_down', sa.Boolean(), nullable=False),
        sa.Column('game_seconds_remaining', sa.Integer(), nullable=True),
        sa.Column('drive_id', sa.String(length=32), nullable=True),
        sa.Column('passer_player_id', sa.String(length=64), nullable=True),
        sa.Column('rusher_player_id', sa.String(length=64), nullable=True),
        sa.Column('receiver_player_id', sa.String(length=64), nullable=True),
        sa.Column('score_before_off', sa.Integer(), nullable=False),
        sa.Column('score_before_def', sa.Integer(), nullable=False),
        sa.Column('text', sa.Text(), nullable=True),
        sa.Column('provider', sa.String(length=64), nullable=False),
        sa.Column('provider_record_id', sa.String(length=160), nullable=False),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('effective_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ingested_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('quality_status', sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(['defense_id'], ['teams.team_id'], ),
        sa.ForeignKeyConstraint(['game_id'], ['games.game_id'], ),
        sa.ForeignKeyConstraint(['offense_id'], ['teams.team_id'], ),
        sa.PrimaryKeyConstraint('play_pk'),
        sa.UniqueConstraint('game_id', 'play_id', 'provider', name='uq_play')
        )
    if 'plays' not in existing:
        op.create_index(op.f('ix_plays_game_id'), 'plays', ['game_id'], unique=False)
    if 'plays' not in existing:
        op.create_index(op.f('ix_plays_provider'), 'plays', ['provider'], unique=False)
    if 'simulation_runs' not in existing:
        op.create_table('simulation_runs',
        sa.Column('run_id', sa.String(length=64), nullable=False),
        sa.Column('game_id', sa.String(length=64), nullable=False),
        sa.Column('as_of', sa.DateTime(timezone=True), nullable=False),
        sa.Column('seed', sa.Integer(), nullable=False),
        sa.Column('draw_count', sa.Integer(), nullable=False),
        sa.Column('assumptions', sa.JSON(), nullable=False),
        sa.Column('scenario', sa.String(length=32), nullable=False),
        sa.Column('overrides', sa.JSON(), nullable=False),
        sa.Column('model_version_id', sa.String(length=64), nullable=False),
        sa.Column('parquet_path', sa.String(length=512), nullable=True),
        sa.Column('summary', sa.JSON(), nullable=False),
        sa.Column('convergence', sa.JSON(), nullable=False),
        sa.Column('research_only', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['game_id'], ['games.game_id'], ),
        sa.ForeignKeyConstraint(['model_version_id'], ['model_versions.model_version_id'], ),
        sa.PrimaryKeyConstraint('run_id')
        )
    if 'simulation_runs' not in existing:
        op.create_index(op.f('ix_simulation_runs_game_id'), 'simulation_runs', ['game_id'], unique=False)
    if 'team_game_stats' not in existing:
        op.create_table('team_game_stats',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('game_id', sa.String(length=64), nullable=False),
        sa.Column('team_id', sa.String(length=64), nullable=False),
        sa.Column('epa_per_play', sa.Float(), nullable=True),
        sa.Column('success_rate', sa.Float(), nullable=True),
        sa.Column('pace_sec_per_play', sa.Float(), nullable=True),
        sa.Column('explosiveness', sa.Float(), nullable=True),
        sa.Column('red_zone_td_rate', sa.Float(), nullable=True),
        sa.Column('implied_total', sa.Float(), nullable=True),
        sa.Column('provider', sa.String(length=64), nullable=False),
        sa.Column('provider_record_id', sa.String(length=160), nullable=False),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('effective_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ingested_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('quality_status', sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(['game_id'], ['games.game_id'], ),
        sa.ForeignKeyConstraint(['team_id'], ['teams.team_id'], ),
        sa.PrimaryKeyConstraint('id')
        )
    if 'team_game_stats' not in existing:
        op.create_index(op.f('ix_team_game_stats_game_id'), 'team_game_stats', ['game_id'], unique=False)
    if 'team_game_stats' not in existing:
        op.create_index(op.f('ix_team_game_stats_provider'), 'team_game_stats', ['provider'], unique=False)
    if 'weather_snapshots' not in existing:
        op.create_table('weather_snapshots',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('venue_id', sa.String(length=64), nullable=False),
        sa.Column('game_id', sa.String(length=64), nullable=True),
        sa.Column('forecast_for', sa.DateTime(timezone=True), nullable=False),
        sa.Column('issued_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('temperature_c', sa.Float(), nullable=True),
        sa.Column('wind_mps', sa.Float(), nullable=True),
        sa.Column('gust_mps', sa.Float(), nullable=True),
        sa.Column('wind_dir_deg', sa.Float(), nullable=True),
        sa.Column('precip_mm', sa.Float(), nullable=True),
        sa.Column('precip_probability', sa.Float(), nullable=True),
        sa.Column('humidity', sa.Float(), nullable=True),
        sa.Column('summary', sa.String(length=160), nullable=True),
        sa.Column('alerts', sa.JSON(), nullable=True),
        sa.Column('roof_status', sa.String(length=32), nullable=False),
        sa.Column('provider', sa.String(length=64), nullable=False),
        sa.Column('provider_record_id', sa.String(length=160), nullable=False),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('effective_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ingested_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('quality_status', sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(['game_id'], ['games.game_id'], ),
        sa.ForeignKeyConstraint(['venue_id'], ['venues.venue_id'], ),
        sa.PrimaryKeyConstraint('id')
        )
    if 'weather_snapshots' not in existing:
        op.create_index(op.f('ix_weather_snapshots_provider'), 'weather_snapshots', ['provider'], unique=False)
    if 'weather_snapshots' not in existing:
        op.create_index(op.f('ix_weather_snapshots_venue_id'), 'weather_snapshots', ['venue_id'], unique=False)
    if 'ai_alerts' not in existing:
        op.create_table('ai_alerts',
        sa.Column('alert_id', sa.String(length=64), nullable=False),
        sa.Column('game_id', sa.String(length=64), nullable=False),
        sa.Column('run_id', sa.String(length=64), nullable=False),
        sa.Column('kind', sa.String(length=32), nullable=False),
        sa.Column('summary', sa.Text(), nullable=False),
        sa.Column('affected_forecast_ids', sa.JSON(), nullable=False),
        sa.Column('model_response', sa.String(length=40), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['game_id'], ['games.game_id'], ),
        sa.ForeignKeyConstraint(['run_id'], ['ai_runs.run_id'], ),
        sa.PrimaryKeyConstraint('alert_id')
        )
    if 'ai_alerts' not in existing:
        op.create_index(op.f('ix_ai_alerts_game_id'), 'ai_alerts', ['game_id'], unique=False)
    if 'ai_explanations' not in existing:
        op.create_table('ai_explanations',
        sa.Column('explanation_id', sa.String(length=64), nullable=False),
        sa.Column('run_id', sa.String(length=64), nullable=False),
        sa.Column('game_id', sa.String(length=64), nullable=False),
        sa.Column('forecast_id', sa.String(length=64), nullable=False),
        sa.Column('recommendation_status', sa.String(length=32), nullable=False),
        sa.Column('reason_codes', sa.JSON(), nullable=False),
        sa.Column('narrative', sa.Text(), nullable=False),
        sa.Column('factors', sa.JSON(), nullable=False),
        sa.Column('assumptions', sa.JSON(), nullable=False),
        sa.Column('uncertainty', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['game_id'], ['games.game_id'], ),
        sa.ForeignKeyConstraint(['run_id'], ['ai_runs.run_id'], ),
        sa.PrimaryKeyConstraint('explanation_id')
        )
    if 'ai_explanations' not in existing:
        op.create_index(op.f('ix_ai_explanations_game_id'), 'ai_explanations', ['game_id'], unique=False)
    if 'ai_explanations' not in existing:
        op.create_index(op.f('ix_ai_explanations_run_id'), 'ai_explanations', ['run_id'], unique=False)
    if 'ai_findings' not in existing:
        op.create_table('ai_findings',
        sa.Column('finding_id', sa.String(length=64), nullable=False),
        sa.Column('run_id', sa.String(length=64), nullable=False),
        sa.Column('game_id', sa.String(length=64), nullable=False),
        sa.Column('topic', sa.String(length=32), nullable=False),
        sa.Column('claim_type', sa.String(length=16), nullable=False),
        sa.Column('statement', sa.Text(), nullable=False),
        sa.Column('confidence', sa.Float(), nullable=False),
        sa.Column('subject_key', sa.String(length=128), nullable=False),
        sa.Column('stance', sa.String(length=64), nullable=False),
        sa.Column('evidence', sa.JSON(), nullable=False),
        sa.Column('affected_player_ids', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['game_id'], ['games.game_id'], ),
        sa.ForeignKeyConstraint(['run_id'], ['ai_runs.run_id'], ),
        sa.PrimaryKeyConstraint('finding_id')
        )
    if 'ai_findings' not in existing:
        op.create_index(op.f('ix_ai_findings_game_id'), 'ai_findings', ['game_id'], unique=False)
    if 'ai_findings' not in existing:
        op.create_index(op.f('ix_ai_findings_run_id'), 'ai_findings', ['run_id'], unique=False)
    if 'ai_opportunities' not in existing:
        op.create_table('ai_opportunities',
        sa.Column('opportunity_id', sa.String(length=64), nullable=False),
        sa.Column('run_id', sa.String(length=64), nullable=False),
        sa.Column('game_id', sa.String(length=64), nullable=False),
        sa.Column('player_id', sa.String(length=64), nullable=False),
        sa.Column('market_id', sa.String(length=32), nullable=False),
        sa.Column('claim_type', sa.String(length=16), nullable=False),
        sa.Column('explanation', sa.Text(), nullable=False),
        sa.Column('evidence_confidence', sa.Float(), nullable=False),
        sa.Column('evidence', sa.JSON(), nullable=False),
        sa.Column('missing_information', sa.JSON(), nullable=False),
        sa.Column('recommended_analysis', sa.Text(), nullable=False),
        sa.Column('validation_status', sa.String(length=32), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['game_id'], ['games.game_id'], ),
        sa.ForeignKeyConstraint(['run_id'], ['ai_runs.run_id'], ),
        sa.PrimaryKeyConstraint('opportunity_id')
        )
    if 'ai_opportunities' not in existing:
        op.create_index(op.f('ix_ai_opportunities_game_id'), 'ai_opportunities', ['game_id'], unique=False)
    if 'ai_opportunities' not in existing:
        op.create_index(op.f('ix_ai_opportunities_market_id'), 'ai_opportunities', ['market_id'], unique=False)
    if 'ai_opportunities' not in existing:
        op.create_index(op.f('ix_ai_opportunities_player_id'), 'ai_opportunities', ['player_id'], unique=False)
    if 'ai_opportunities' not in existing:
        op.create_index(op.f('ix_ai_opportunities_run_id'), 'ai_opportunities', ['run_id'], unique=False)
    if 'ai_signals' not in existing:
        op.create_table('ai_signals',
        sa.Column('signal_id', sa.String(length=64), nullable=False),
        sa.Column('run_id', sa.String(length=64), nullable=False),
        sa.Column('game_id', sa.String(length=64), nullable=False),
        sa.Column('player_id', sa.String(length=64), nullable=False),
        sa.Column('metric', sa.String(length=32), nullable=False),
        sa.Column('direction', sa.String(length=16), nullable=False),
        sa.Column('magnitude', sa.String(length=16), nullable=False),
        sa.Column('claim_type', sa.String(length=16), nullable=False),
        sa.Column('statement', sa.Text(), nullable=False),
        sa.Column('confidence', sa.Float(), nullable=False),
        sa.Column('validation_status', sa.String(length=32), nullable=False),
        sa.Column('scenario_status', sa.String(length=32), nullable=False),
        sa.Column('simulation_run_id', sa.String(length=64), nullable=True),
        sa.Column('numeric_override', sa.JSON(), nullable=True),
        sa.Column('evidence', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['game_id'], ['games.game_id'], ),
        sa.ForeignKeyConstraint(['run_id'], ['ai_runs.run_id'], ),
        sa.PrimaryKeyConstraint('signal_id')
        )
    if 'ai_signals' not in existing:
        op.create_index(op.f('ix_ai_signals_game_id'), 'ai_signals', ['game_id'], unique=False)
    if 'ai_signals' not in existing:
        op.create_index(op.f('ix_ai_signals_player_id'), 'ai_signals', ['player_id'], unique=False)
    if 'ai_signals' not in existing:
        op.create_index(op.f('ix_ai_signals_run_id'), 'ai_signals', ['run_id'], unique=False)
    if 'forecasts' not in existing:
        op.create_table('forecasts',
        sa.Column('forecast_id', sa.String(length=64), nullable=False),
        sa.Column('outcome_id', sa.String(length=64), nullable=False),
        sa.Column('as_of', sa.DateTime(timezone=True), nullable=False),
        sa.Column('model_version_id', sa.String(length=64), nullable=False),
        sa.Column('p', sa.Float(), nullable=False),
        sa.Column('p_low', sa.Float(), nullable=False),
        sa.Column('p_high', sa.Float(), nullable=False),
        sa.Column('sim_count', sa.Integer(), nullable=False),
        sa.Column('data_cutoff', sa.DateTime(timezone=True), nullable=False),
        sa.Column('availability_status', sa.String(length=32), nullable=False),
        sa.Column('reason_codes', sa.JSON(), nullable=False),
        sa.Column('components', sa.JSON(), nullable=False),
        sa.Column('is_official', sa.Boolean(), nullable=False),
        sa.Column('data_tier', sa.String(length=16), nullable=False),
        sa.ForeignKeyConstraint(['model_version_id'], ['model_versions.model_version_id'], ),
        sa.ForeignKeyConstraint(['outcome_id'], ['outcomes.outcome_id'], ),
        sa.PrimaryKeyConstraint('forecast_id')
        )
    if 'forecasts' not in existing:
        op.create_index(op.f('ix_forecasts_as_of'), 'forecasts', ['as_of'], unique=False)
    if 'forecasts' not in existing:
        op.create_index(op.f('ix_forecasts_outcome_id'), 'forecasts', ['outcome_id'], unique=False)
    if 'odds_ticks' not in existing:
        op.create_table('odds_ticks',
        sa.Column('tick_id', sa.String(length=64), nullable=False),
        sa.Column('outcome_id', sa.String(length=64), nullable=False),
        sa.Column('bookmaker_id', sa.String(length=64), nullable=False),
        sa.Column('price_american', sa.Integer(), nullable=False),
        sa.Column('captured_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('source_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('is_live', sa.Boolean(), nullable=False),
        sa.Column('limits', sa.Float(), nullable=True),
        sa.Column('credit_cost', sa.Integer(), nullable=False),
        sa.Column('provider', sa.String(length=64), nullable=False),
        sa.Column('provider_record_id', sa.String(length=160), nullable=False),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('effective_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ingested_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('quality_status', sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(['bookmaker_id'], ['bookmakers.bookmaker_id'], ),
        sa.ForeignKeyConstraint(['outcome_id'], ['outcomes.outcome_id'], ),
        sa.PrimaryKeyConstraint('tick_id')
        )
    if 'odds_ticks' not in existing:
        op.create_index(op.f('ix_odds_ticks_captured_at'), 'odds_ticks', ['captured_at'], unique=False)
    if 'odds_ticks' not in existing:
        op.create_index(op.f('ix_odds_ticks_outcome_id'), 'odds_ticks', ['outcome_id'], unique=False)
    if 'odds_ticks' not in existing:
        op.create_index(op.f('ix_odds_ticks_provider'), 'odds_ticks', ['provider'], unique=False)
    if 'settlements' not in existing:
        op.create_table('settlements',
        sa.Column('settlement_id', sa.String(length=64), nullable=False),
        sa.Column('outcome_id', sa.String(length=64), nullable=False),
        sa.Column('result', sa.String(length=16), nullable=False),
        sa.Column('settled_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('official_source', sa.String(length=128), nullable=False),
        sa.Column('void_reason', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['outcome_id'], ['outcomes.outcome_id'], ),
        sa.PrimaryKeyConstraint('settlement_id'),
        sa.UniqueConstraint('outcome_id')
        )
    if 'recommendations' not in existing:
        op.create_table('recommendations',
        sa.Column('recommendation_id', sa.String(length=64), nullable=False),
        sa.Column('forecast_id', sa.String(length=64), nullable=False),
        sa.Column('odds_tick_id', sa.String(length=64), nullable=True),
        sa.Column('ev', sa.Float(), nullable=True),
        sa.Column('ev_after_haircut', sa.Float(), nullable=True),
        sa.Column('threshold', sa.Float(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('reason_codes', sa.JSON(), nullable=False),
        sa.Column('kelly_fractional', sa.Float(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('immutable', sa.Boolean(), nullable=False),
        sa.Column('data_tier', sa.String(length=16), nullable=False),
        sa.ForeignKeyConstraint(['forecast_id'], ['forecasts.forecast_id'], ),
        sa.ForeignKeyConstraint(['odds_tick_id'], ['odds_ticks.tick_id'], ),
        sa.PrimaryKeyConstraint('recommendation_id')
        )
    for table, column, ddl in LEGACY_COLUMNS:
        if table in existing and column not in {c["name"] for c in sa.inspect(bind).get_columns(table)}:
            op.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


def downgrade():
    raise RuntimeError("Baseline rollback would delete customer history. Roll back application code; restore a verified backup only if necessary.")
