from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import OperationalError

from spe.config import settings
from spe.db.session import init_db


def test_production_startup_retries_connection_but_still_checks_schema(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, 'app_env', 'prod')
    monkeypatch.setattr(settings, 'parquet_dir', tmp_path/'parquet')
    engine = MagicMock()
    connection = MagicMock()
    connection.__enter__.return_value.execute.return_value.scalar_one.return_value = '20261116_04'
    engine.connect.side_effect = [OperationalError('connect', {}, Exception('timeout')), connection]
    monkeypatch.setattr('spe.db.session.get_engine', lambda: engine)
    monkeypatch.setattr('time.sleep', lambda _: None)
    init_db()
    assert engine.connect.call_count == 2
    connection.__enter__.return_value.execute.assert_called_once()
    assert settings.parquet_dir.exists()

    engine.connect.side_effect = None
    engine.connect.return_value = connection
    connection.__enter__.return_value.execute.return_value.scalar_one.return_value = 'old_revision'
    with pytest.raises(RuntimeError, match='schema is not at the release revision'):
        init_db()
