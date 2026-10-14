"""Operational state stays in the two existing annotation tables."""
from pathlib import Path

import pytest
import psycopg
from psycopg.types.json import Jsonb

from test_annotation_schema import database, connection, insert_batch

MIGRATION = Path(__file__).resolve().parents[1] / 'migrations/002_annotation_processing.sql'


def test_processing_migration_exists():
    assert MIGRATION.exists(), 'Missing operational state migration'


def test_batch_processing_defaults_to_queued(connection):
    row = insert_batch(connection)
    assert row['processing_info'] == {'status': 'queued', 'attempts': 0}


@pytest.mark.parametrize('value', [[], None, {}, {'status': 'approved', 'attempts': 0}])
def test_invalid_operational_states_are_rejected(connection, value):
    with pytest.raises(psycopg.Error):
        insert_batch(connection, processing_info=Jsonb(value))
