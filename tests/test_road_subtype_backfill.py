"""Subtype-only regressions with an independent disposable PostgreSQL cluster.

No application secrets, production connections, or actual model requests are used.
The cluster uses its own Unix socket and port 55443, separate from other tests.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import json
from pathlib import Path
import subprocess
import tempfile
import threading
from uuid import uuid4

from PIL import Image
import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
import pytest

from annotation_schema import FIELD_DEFINITIONS, validate_annotation
from annotation_worker import APIError, Config, MODEL


PROJECT = Path(__file__).resolve().parents[1]
PG = Path('/usr/lib/postgresql/14/bin')


@pytest.fixture(scope='module')
def backfill():
    path = PROJECT / 'road_subtype_backfill.py'
    assert path.is_file(), 'The dedicated subtype-only backfill worker is missing'
    return importlib.import_module('road_subtype_backfill')


def unknown_labels():
    return [dict(field=item['code'], state='unknown', values=[], scope='frame',
                 origin='model', evidence='representative', reason='当前帧无法判断')
            for item in FIELD_DEFINITIONS]


def get_label(labels, code):
    return next(item for item in labels if item['field'] == code)


def original_labels(*, parent='城市道路', parent_reason=True):
    labels = unknown_labels()
    if parent:
        item = get_label(labels, 'top_road_category')
        item.update(state='observed', values=[parent])
        if not parent_reason:
            item.pop('reason')
    return labels


def subtype_payload(labels, *, parent='城市道路', child='主干路'):
    old_parent = get_label(labels, 'top_road_category')
    selected_parent = deepcopy(old_parent)
    if old_parent['state'] == 'unknown' and parent:
        selected_parent.update(state='observed', values=[parent], reason='视觉推断：城市建成环境')
    subtype = deepcopy(get_label(labels, 'top_road_subcategory'))
    if child:
        subtype.update(state='observed', values=[child], reason='视觉推断：连续城市道路及沿线出入口')
    return {'labels': [selected_parent, subtype]}


def response_for(labels, **changes):
    return dict(raw_content=json.dumps(subtype_payload(labels, **changes), ensure_ascii=False),
                model=MODEL, usage={'total_tokens': 31})


def test_merge_only_replaces_subtype_and_preserves_parent_without_optional_reason():
    assert (PROJECT / 'road_subtype_backfill.py').is_file(), 'Subtype merge implementation is missing'
    backfill = importlib.import_module('road_subtype_backfill')
    labels = original_labels(parent_reason=False)
    before = deepcopy(labels)
    merged = backfill.merge_response(labels, subtype_payload(labels))
    assert labels == before
    assert get_label(merged, 'top_road_subcategory')['values'] == ['主干路']
    assert [item for item in merged if item['field'] != 'top_road_subcategory'] == [
        item for item in before if item['field'] != 'top_road_subcategory']
    assert validate_annotation(merged) == []


def test_unknown_parent_can_change_only_with_compatible_observed_child(backfill):
    labels = original_labels(parent=None)
    merged = backfill.merge_response(labels, subtype_payload(labels))
    assert get_label(merged, 'top_road_category')['values'] == ['城市道路']
    assert get_label(merged, 'top_road_subcategory')['values'] == ['主干路']
    assert merged[2:] == labels[2:]
    assert validate_annotation(merged) == []


def test_no_estimate_preserves_original_labels_and_reasons(backfill):
    labels = original_labels(parent=None)
    payload = subtype_payload(labels, parent=None, child=None)
    payload['labels'][0]['reason'] = '未见道路'
    payload['labels'][1]['reason'] = '当前无可见道路，无法估计'
    assert backfill.merge_response(labels, payload) == labels


@pytest.mark.parametrize('mutation', [
    'other_parent', 'wrong_parent_child', 'extra_label', 'extra_global',
    'wrong_origin', 'no_visual_reason', 'duplicate_parent', 'observed_parent_only',
])
def test_merge_rejects_invalid_or_out_of_scope_changes(backfill, mutation):
    labels = original_labels(parent=None if mutation == 'observed_parent_only' else '城市道路')
    payload = subtype_payload(labels)
    parent, child = payload['labels']
    if mutation == 'other_parent':
        parent.update(values=['公路'])
    elif mutation == 'wrong_parent_child':
        child['values'] = ['高速公路']
    elif mutation == 'extra_label':
        payload['labels'].append(deepcopy(labels[2]))
    elif mutation == 'extra_global':
        payload['confidence'] = 0.9
    elif mutation == 'wrong_origin':
        child['origin'] = 'visual_estimation'
    elif mutation == 'no_visual_reason':
        child['reason'] = '猜测为主干路'
    elif mutation == 'duplicate_parent':
        payload['labels'][1] = deepcopy(parent)
    elif mutation == 'observed_parent_only':
        child.update(state='unknown', values=[], reason='道路等级无法估计')
    with pytest.raises(ValueError):
        backfill.merge_response(labels, payload)


@pytest.fixture(scope='module')
def database():
    for binary in ('initdb', 'pg_ctl'):
        if not (PG / binary).is_file():
            pytest.skip('PostgreSQL test binaries are unavailable')
    with tempfile.TemporaryDirectory(prefix='road-subtype-pg-') as temporary:
        root = Path(temporary)
        data, socket = root / 'data', root / 'socket'
        socket.mkdir()
        options = dict(host=str(socket), port=55443, user='subtype_test_admin',
                       dbname='aerial_db', connect_timeout=5)
        def pg(name, *arguments, check=True):
            return subprocess.run([str(PG / name), *map(str, arguments)], check=check,
                                  capture_output=True, text=True, timeout=30)
        try:
            pg('initdb', '-D', data, '-U', options['user'], '--auth-local=trust',
               '--auth-host=reject', '--encoding=UTF8', '--no-locale')
            pg('pg_ctl', '-D', data, '-l', root / 'postgres.log', '-o',
               f"-k {socket} -p 55443 -c listen_addresses=''", '-w', 'start')
            with psycopg.connect(**{**options, 'dbname': 'postgres'}, autocommit=True) as setup:
                setup.execute('CREATE ROLE ad4che NOLOGIN')
                setup.execute('CREATE ROLE reader NOLOGIN')
                setup.execute('CREATE DATABASE aerial_db OWNER ad4che')
            with psycopg.connect(**options, autocommit=True) as setup:
                setup.execute('CREATE SCHEMA aerial AUTHORIZATION ad4che')
                setup.execute('SET ROLE ad4che')
                setup.execute('GRANT USAGE ON SCHEMA aerial TO reader')
                setup.execute('''CREATE TABLE aerial.videos (
                    sha256 BYTEA PRIMARY KEY, deleted_at TIMESTAMPTZ)''')
                for migration in sorted((PROJECT / 'migrations').glob('*.sql')):
                    setup.execute(migration.read_text())
            yield make_conninfo(**options)
        finally:
            if (data / 'PG_VERSION').exists():
                pg('pg_ctl', '-D', data, '-m', 'immediate', '-w', 'stop', check=False)


@pytest.fixture
def context(database, tmp_path):
    config = Config(database, database, 'unused-subtype-test-secret', tmp_path / 'frames',
                    tmp_path / 'unused-boundaries.json')
    with psycopg.connect(database, autocommit=True, row_factory=dict_row) as writer, \
         psycopg.connect(database, autocommit=True, row_factory=dict_row) as observer:
        writer.execute('SET ROLE ad4che')
        observer.execute('SET ROLE ad4che')
        try:
            yield config, writer, observer
        finally:
            writer.execute('DELETE FROM aerial.annotation_batches')
            writer.execute('DELETE FROM aerial.videos')


def prepare(context, **overrides):
    config, writer, _ = context
    batch_id, video_hash = uuid4(), uuid4().bytes * 2
    folder = config.output_root / str(batch_id)
    folder.mkdir(parents=True)
    frame = folder / 'representative.jpg'
    Image.new('RGB', (16, 16), 'gray').save(frame, 'JPEG')
    digest = hashlib.sha256(frame.read_bytes()).hexdigest()
    values = dict(batch_id=batch_id, source_directory=f'/subtype-fixture/{batch_id}',
                  representative_video_sha256=video_hash, frame_timestamp_ms=5000,
                  frame_path=str(frame), labels=Jsonb(original_labels()), validation_passed=True,
                  raw_model_response='ORIGINAL FULL MODEL RESPONSE', model_name=MODEL,
                  prompt_version='battery-frame-v3',
                  processing_info=Jsonb({'status': 'completed', 'attempts': 1,
                                         'frame_sha256': digest, 'city': '测试城市'}))
    values.update(overrides)
    writer.execute('INSERT INTO aerial.videos(sha256) VALUES (%s)', (video_hash,))
    with writer.transaction():
        writer.execute('SET CONSTRAINTS ALL DEFERRED')
        from psycopg import sql
        writer.execute(sql.SQL('INSERT INTO aerial.annotation_batches ({}) VALUES ({})').format(
            sql.SQL(',').join(map(sql.Identifier, values)),
            sql.SQL(',').join(sql.Placeholder() for _ in values)), list(values.values()))
        writer.execute('INSERT INTO aerial.annotation_batch_videos VALUES (%s,%s)', (batch_id, video_hash))
    return batch_id


def row(connection, batch_id):
    return connection.execute('SELECT * FROM aerial.annotation_batches WHERE batch_id=%s',
                              (batch_id,)).fetchone()


def test_apply_preserves_full_response_and_audits_subtype_provenance(backfill, context, monkeypatch):
    config, writer, observer = context
    batch_id = prepare(context)
    before = row(observer, batch_id)
    monkeypatch.setattr(backfill, 'call_model', lambda *args, **kwargs: response_for(before['labels']))
    claim = backfill.claim_batch(writer)
    assert claim.batch_id == batch_id
    during = row(observer, batch_id)
    assert during['processing_info']['status'] == 'completed'
    assert during['validation_passed'] is True
    assert backfill.process_claim(config, writer, claim) == 'applied'
    after = row(observer, batch_id)
    assert after['labels'][2:] == before['labels'][2:]
    assert after['labels'][0] == before['labels'][0]
    assert after['raw_model_response'] == 'ORIGINAL FULL MODEL RESPONSE'
    assert after['prompt_version'] == 'battery-frame-v3'
    assert after['model_name'] == MODEL
    assert after['label_revision'] == before['label_revision'] + 1
    assert after['validation_passed'] is True
    assert after['validation_errors'] == []
    assert after['processing_info']['status'] == 'completed'
    history = after['processing_info']['road_subtype_history']
    assert len(history) == 1
    audit = history[0]
    assert audit['old_labels'] == before['labels'][:2]
    assert audit['new_labels'] == after['labels'][:2]
    assert audit['raw_model_response'] == response_for(before['labels'])['raw_content']
    assert audit['model'] == MODEL
    assert audit['prompt_version'] == 'road-subtype-visual-v1'
    assert audit['frame_sha256'] == before['processing_info']['frame_sha256']
    assert audit['usage'] == {'total_tokens': 31}
    assert audit['finished_at']
    assert backfill.claim_batch(writer) is None
    caches = list(config.output_root.rglob('subtype-*.json'))
    assert len(caches) == 1
    assert caches[0].stat().st_mode & 0o777 == 0o600


def test_unknown_output_is_terminal_without_revision_or_label_change(backfill, context, monkeypatch):
    config, writer, observer = context
    batch_id = prepare(context, labels=Jsonb(original_labels(parent=None)))
    before = row(observer, batch_id)
    monkeypatch.setattr(backfill, 'call_model', lambda *args, **kwargs:
                        response_for(before['labels'], parent=None, child=None))
    assert backfill.process_claim(config, writer, backfill.claim_batch(writer)) == 'no_estimate'
    after = row(observer, batch_id)
    assert after['labels'] == before['labels']
    assert after['label_revision'] == before['label_revision']
    assert after['processing_info']['road_subtype_backfill']['status'] == 'no_estimate'
    assert backfill.claim_batch(writer) is None


def test_unknown_parent_pair_is_applied_without_changing_other_fields(backfill, context, monkeypatch):
    config, writer, observer = context
    batch_id = prepare(context, labels=Jsonb(original_labels(parent=None)))
    before = row(observer, batch_id)
    monkeypatch.setattr(backfill, 'call_model', lambda *args, **kwargs: response_for(before['labels']))
    assert backfill.process_claim(config, writer, backfill.claim_batch(writer)) == 'applied'
    after = row(observer, batch_id)
    assert after['labels'][0]['values'] == ['城市道路']
    assert after['labels'][1]['values'] == ['主干路']
    assert after['labels'][2:] == before['labels'][2:]
    assert after['validation_passed'] is True
    assert after['raw_model_response'] == before['raw_model_response']


@pytest.mark.parametrize('condition', ['approved', 'rejected', 'invalid_labels', 'not_validated',
                                       'deleted', 'no_member', 'already_observed', 'parent_unlabeled'])
def test_claim_excludes_ineligible_batches(backfill, context, condition):
    _, writer, _ = context
    labels = original_labels()
    values = {}
    if condition in ('approved', 'rejected'):
        values['review_status'] = condition
        if condition == 'approved':
            values.update(reviewer_name='fixture', reviewed_at=datetime.now(timezone.utc))
    elif condition == 'invalid_labels':
        labels.pop()
    elif condition == 'not_validated':
        values['validation_passed'] = False
    elif condition == 'already_observed':
        get_label(labels, 'top_road_subcategory').update(
            state='observed', values=['支路'], reason='existing')
    elif condition == 'parent_unlabeled':
        get_label(labels, 'top_road_category').update(state='unlabeled', values=[])
    values['labels'] = Jsonb(labels)
    batch_id = prepare(context, **values)
    if condition == 'deleted':
        writer.execute('UPDATE aerial.videos SET deleted_at=now()')
    elif condition == 'no_member':
        with writer.transaction():
            writer.execute('UPDATE aerial.annotation_batches SET representative_video_sha256=NULL,frame_path=NULL,frame_timestamp_ms=NULL WHERE batch_id=%s', (batch_id,))
            writer.execute('DELETE FROM aerial.annotation_batch_videos')
    assert backfill.claim_batch(writer) is None


def test_claim_uses_distinct_rows_across_connections(backfill, context):
    _, first, second = context
    batch_ids = {prepare(context), prepare(context)}
    claims = [backfill.claim_batch(first), backfill.claim_batch(second)]
    assert {claim.batch_id for claim in claims} == batch_ids
    assert claims[0].token != claims[1].token
    assert backfill.claim_batch(first) is None


def test_live_claim_cannot_be_reclaimed_after_source_revision_changes(backfill, context, monkeypatch):
    config, writer, observer = context
    batch_id = prepare(context)
    before = row(observer, batch_id)
    first_claim = backfill.claim_batch(writer)
    changed = deepcopy(before['labels'])
    get_label(changed, 'road.surface.material').update(state='observed', values=['混凝土'])
    with observer.transaction():
        observer.execute('UPDATE aerial.annotation_batches SET labels=%s WHERE batch_id=%s',
                         (Jsonb(changed), batch_id))
        observer.execute('UPDATE aerial.annotation_batches SET validation_passed=true WHERE batch_id=%s',
                         (batch_id,))
    assert backfill.claim_batch(observer) is None
    monkeypatch.setattr(backfill, 'call_model', lambda *args, **kwargs: response_for(before['labels']))
    assert backfill.process_claim(config, writer, first_claim) == 'source_changed'
    after = row(observer, batch_id)
    assert after['labels'] == changed
    assert after['validation_passed'] is True


def test_source_conflict_does_not_terminally_mark_new_revision(backfill, context, monkeypatch):
    config, writer, observer = context
    batch_id = prepare(context)
    before = row(observer, batch_id)
    def first_model(*args, **kwargs):
        changed = deepcopy(before['labels'])
        get_label(changed, 'road.surface.material').update(state='observed', values=['混凝土'])
        with observer.transaction():
            observer.execute('UPDATE aerial.annotation_batches SET labels=%s WHERE batch_id=%s',
                             (Jsonb(changed), batch_id))
            observer.execute('UPDATE aerial.annotation_batches SET validation_passed=true WHERE batch_id=%s',
                             (batch_id,))
        return response_for(before['labels'])
    monkeypatch.setattr(backfill, 'call_model', first_model)
    assert backfill.process_claim(config, writer, backfill.claim_batch(writer)) == 'source_changed'
    latest_claim = backfill.claim_batch(writer)
    assert latest_claim is not None
    def forbidden(*args, **kwargs):
        pytest.fail('The same image and parent must reuse the existing paid response')
    monkeypatch.setattr(backfill, 'call_model', forbidden)
    assert backfill.process_claim(config, writer, latest_claim) == 'applied'
    after = row(observer, batch_id)
    assert get_label(after['labels'], 'road.surface.material')['values'] == ['混凝土']
    assert get_label(after['labels'], 'top_road_subcategory')['values'] == ['主干路']
    assert [item['status'] for item in after['processing_info']['road_subtype_history']] == ['source_changed', 'applied']


@pytest.mark.parametrize('change', ['labels', 'evidence', 'review', 'claim_token', 'deleted'])
def test_concurrent_changes_preserve_newer_results(backfill, context, monkeypatch, change):
    config, writer, observer = context
    batch_id = prepare(context)
    before = row(observer, batch_id)
    def model(*args, **kwargs):
        if change == 'labels':
            changed = deepcopy(before['labels'])
            get_label(changed, 'road.surface.material').update(state='observed', values=['混凝土'])
            observer.execute('UPDATE aerial.annotation_batches SET labels=%s WHERE batch_id=%s', (Jsonb(changed), batch_id))
        elif change == 'evidence':
            observer.execute('UPDATE aerial.annotation_batches SET frame_timestamp_ms=7000 WHERE batch_id=%s', (batch_id,))
        elif change == 'review':
            observer.execute("UPDATE aerial.annotation_batches SET review_status='approved',reviewer_name='fixture',reviewed_at=now() WHERE batch_id=%s", (batch_id,))
        elif change == 'claim_token':
            observer.execute("UPDATE aerial.annotation_batches SET processing_info=jsonb_set(processing_info,'{road_subtype_backfill,claim_token}',%s) WHERE batch_id=%s", (Jsonb('replacement-owner'), batch_id))
        else:
            observer.execute('UPDATE aerial.videos SET deleted_at=now()')
        return response_for(before['labels'])
    monkeypatch.setattr(backfill, 'call_model', model)
    claim = backfill.claim_batch(writer)
    assert backfill.process_claim(config, writer, claim) == 'source_changed'
    after = row(observer, batch_id)
    assert get_label(after['labels'], 'top_road_subcategory')['state'] == 'unknown'
    assert after['raw_model_response'] == before['raw_model_response']
    if change == 'review':
        assert after['review_status'] == 'approved'
        assert after['validation_passed'] is True
    if change == 'labels':
        assert get_label(after['labels'], 'road.surface.material')['values'] == ['混凝土']
        assert after['validation_passed'] is False


def test_frame_digest_mismatch_never_sends_or_changes_labels(backfill, context, monkeypatch):
    config, writer, observer = context
    batch_id = prepare(context)
    before = row(observer, batch_id)
    Path(before['frame_path']).write_bytes(b'replaced-image')
    def forbidden(*args, **kwargs):
        pytest.fail('A mismatched frame must never be sent to the model')
    monkeypatch.setattr(backfill, 'call_model', forbidden)
    assert backfill.process_claim(config, writer, backfill.claim_batch(writer)) == 'failed'
    after = row(observer, batch_id)
    assert after['labels'] == before['labels']
    assert after['processing_info']['road_subtype_backfill']['error_code'] == 'frame_digest_mismatch'
    assert backfill.claim_batch(writer) is None


def test_cache_survives_source_conflict_and_is_reused(backfill, context, monkeypatch):
    config, writer, observer = context
    batch_id = prepare(context)
    before = row(observer, batch_id)
    def first_model(*args, **kwargs):
        observer.execute('UPDATE aerial.annotation_batches SET frame_timestamp_ms=7000 WHERE batch_id=%s', (batch_id,))
        return response_for(before['labels'])
    monkeypatch.setattr(backfill, 'call_model', first_model)
    assert backfill.process_claim(config, writer, backfill.claim_batch(writer)) == 'source_changed'
    cache = next(config.output_root.rglob('subtype-*.json'))
    original_cache = cache.read_bytes()
    observer.execute("UPDATE aerial.annotation_batches SET validation_passed=true,processing_info=processing_info-'road_subtype_backfill' WHERE batch_id=%s", (batch_id,))
    def forbidden(*args, **kwargs):
        pytest.fail('An immutable matching response cache should be reused')
    monkeypatch.setattr(backfill, 'call_model', forbidden)
    assert backfill.process_claim(config, writer, backfill.claim_batch(writer)) == 'applied'
    assert cache.read_bytes() == original_cache
    assert row(observer, batch_id)['processing_info']['road_subtype_history'][-1]['cached_response'] is True


def test_transient_failures_stop_at_three_attempts(backfill, context, monkeypatch):
    config, writer, observer = context
    batch_id = prepare(context)
    def unavailable(*args, **kwargs):
        raise APIError('api_rate_limit')
    monkeypatch.setattr(backfill, 'call_model', unavailable)
    for attempt in range(1, 4):
        claim = backfill.claim_batch(writer)
        assert claim is not None
        assert backfill.process_claim(config, writer, claim, retry_seconds=0) == 'failed'
        info = row(observer, batch_id)['processing_info']['road_subtype_backfill']
        assert info['attempts'] == attempt
    assert backfill.claim_batch(writer) is None
    assert len(row(observer, batch_id)['processing_info']['road_subtype_history']) == 3


def test_retry_is_not_reclaimed_until_backoff_expires(backfill, context, monkeypatch):
    config, writer, observer = context
    batch_id = prepare(context)
    def unavailable(*args, **kwargs):
        raise APIError('api_timeout')
    monkeypatch.setattr(backfill, 'call_model', unavailable)
    assert backfill.process_claim(config, writer, backfill.claim_batch(writer), retry_seconds=60) == 'failed'
    assert backfill.claim_batch(writer) is None
    observer.execute("UPDATE aerial.annotation_batches SET processing_info=jsonb_set(processing_info,'{road_subtype_backfill,retry_at}',%s) WHERE batch_id=%s",
                     (Jsonb((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()), batch_id))
    assert backfill.claim_batch(writer).attempts == 2


def test_invalid_response_preserves_original_valid_result_and_raw_attempt(backfill, context, monkeypatch):
    config, writer, observer = context
    batch_id = prepare(context)
    before = row(observer, batch_id)
    monkeypatch.setattr(backfill, 'call_model', lambda *args, **kwargs:
                        dict(raw_content='malformed JSON', model=MODEL, usage={}))
    assert backfill.process_claim(config, writer, backfill.claim_batch(writer)) == 'validation_failed'
    after = row(observer, batch_id)
    assert after['labels'] == before['labels']
    assert after['validation_passed'] is True
    assert after['processing_info']['road_subtype_history'][0]['raw_model_response'] == 'malformed JSON'
    assert backfill.claim_batch(writer) is None


def test_interrupted_claim_recovers_with_attempt_cap(backfill, context, monkeypatch):
    config, writer, observer = context
    batch_id = prepare(context)
    first = backfill.claim_batch(writer)
    backfill.recover_interrupted(writer)
    second = backfill.claim_batch(writer)
    assert second.batch_id == batch_id
    assert second.token != first.token
    assert row(observer, batch_id)['processing_info']['road_subtype_backfill']['attempts'] == 2
    backfill.recover_interrupted(writer)
    assert backfill.claim_batch(writer) is not None
    backfill.recover_interrupted(writer)
    replay = backfill.claim_batch(writer)
    assert replay is not None
    assert replay.attempts == 3
    assert replay.cache_only is True
    def forbidden(*args, **kwargs):
        pytest.fail('Attempt-three recovery cannot issue another paid request')
    monkeypatch.setattr(backfill, 'call_model', forbidden)
    assert backfill.process_claim(config, writer, replay) == 'failed'
    task = row(observer, batch_id)['processing_info']['road_subtype_backfill']
    assert task['attempts'] == 3
    assert task['error_code'] == 'attempt_limit_reached'
    assert backfill.claim_batch(writer) is None


def test_third_attempt_durable_cache_recovers_without_fourth_model_request(backfill, context, monkeypatch):
    config, writer, observer = context
    batch_id = prepare(context)
    for _ in range(2):
        assert backfill.claim_batch(writer) is not None
        backfill.recover_interrupted(writer)
    third_claim = backfill.claim_batch(writer)
    assert third_claim.attempts == 3
    labels = row(observer, batch_id)['labels']
    monkeypatch.setattr(backfill, 'call_model', lambda *args, **kwargs: response_for(labels))
    original_finish = backfill._finish
    def simulate_crash_after_cache(*args, **kwargs):
        raise SystemExit('simulated process interruption after durable response cache')
    monkeypatch.setattr(backfill, '_finish', simulate_crash_after_cache)
    with pytest.raises(SystemExit):
        backfill.process_claim(config, writer, third_claim)
    assert len(list(config.output_root.rglob('subtype-*.json'))) == 1
    backfill.recover_interrupted(writer)
    replay = backfill.claim_batch(writer)
    assert replay is not None
    assert replay.attempts == 3
    assert replay.cache_only is True
    def forbidden(*args, **kwargs):
        pytest.fail('The third paid response is already cached; no fourth request is allowed')
    monkeypatch.setattr(backfill, 'call_model', forbidden)
    monkeypatch.setattr(backfill, '_finish', original_finish)
    assert backfill.process_claim(config, writer, replay) == 'applied'
    after = row(observer, batch_id)
    assert after['processing_info']['road_subtype_backfill']['attempts'] == 3
    assert after['processing_info']['road_subtype_history'][-1]['cached_response'] is True
    assert get_label(after['labels'], 'top_road_subcategory')['values'] == ['主干路']
    assert backfill.claim_batch(writer) is None


def test_bounded_parallel_run_updates_exactly_limit_rows(backfill, context, monkeypatch):
    config, writer, observer = context
    for _ in range(5):
        prepare(context)
    monkeypatch.setattr(backfill, 'call_model', lambda *args, **kwargs: response_for(original_labels()))
    assert backfill.run(config, concurrency=4, limit=2) == 0
    rows = observer.execute('SELECT labels FROM aerial.annotation_batches').fetchall()
    assert sum(get_label(item['labels'], 'top_road_subcategory')['state'] == 'observed' for item in rows) == 2


def test_parallel_run_performs_model_work_on_distinct_connections_concurrently(backfill, context, monkeypatch):
    config, _, observer = context
    for _ in range(2):
        prepare(context)
    rendezvous = threading.Barrier(2)
    def concurrent_model(*args, **kwargs):
        rendezvous.wait(timeout=5)
        return response_for(original_labels())
    monkeypatch.setattr(backfill, 'call_model', concurrent_model)
    assert backfill.run(config, concurrency=2, limit=2) == 0
    labels = [item['labels'] for item in observer.execute('SELECT labels FROM aerial.annotation_batches').fetchall()]
    assert len(labels) == 2
    assert all(get_label(item, 'top_road_subcategory')['values'] == ['主干路'] for item in labels)


def test_stop_event_prevents_new_claims(backfill, context):
    config, _, observer = context
    batch_id = prepare(context)
    stopped = threading.Event()
    stopped.set()
    assert backfill.run(config, concurrency=2, stop_event=stopped) == 0
    assert row(observer, batch_id)['processing_info'].get('road_subtype_backfill') is None


def test_fatal_api_failure_stops_job_and_keeps_existing_results(backfill, context, monkeypatch):
    config, _, observer = context
    for _ in range(3):
        prepare(context)
    def unauthorized(*args, **kwargs):
        raise APIError('api_auth', fatal=True)
    monkeypatch.setattr(backfill, 'call_model', unauthorized)
    assert backfill.run(config, concurrency=1) == 78
    histories = observer.execute("SELECT processing_info->'road_subtype_history' AS history FROM aerial.annotation_batches").fetchall()
    assert sum(bool(item['history']) for item in histories) == 1
    assert all(get_label(item['labels'], 'top_road_subcategory')['state'] == 'unknown'
               for item in observer.execute('SELECT labels FROM aerial.annotation_batches').fetchall())


def test_job_advisory_lock_excludes_duplicate_run(backfill, context, monkeypatch):
    config, writer, observer = context
    batch_id = prepare(context)
    writer.execute('SELECT pg_advisory_lock(%s)', (backfill.LOCK_ID,))
    try:
        assert backfill.run(config, concurrency=1) == 0
        assert row(observer, batch_id)['processing_info'].get('road_subtype_backfill') is None
    finally:
        writer.execute('SELECT pg_advisory_unlock(%s)', (backfill.LOCK_ID,))
