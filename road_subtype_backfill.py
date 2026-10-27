"""Bounded, resumable visual road-subtype backfill using existing frame evidence.

This job changes only an unknown subtype, plus an unknown parent when a compatible
observed pair is returned. Full-response provenance and other labels are retained.
Operational state and append-only attempt history stay in processing_info.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import tempfile
import threading
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from annotation_schema import (
    ROAD_SUBTYPE_PROMPT_VERSION, SCHEMA_VERSION,
    build_road_subtype_prompt, validate_annotation,
)
from annotation_worker import APIError, Config, MODEL, call_model, load_env_file, log, utc_now


LOCK_ID = 634129872542  # Independent from the full annotation worker's lock.
TASK_KEY = 'road_subtype_backfill'
HISTORY_KEY = 'road_subtype_history'
PARENT = 'top_road_category'
CHILD = 'top_road_subcategory'
MAX_ATTEMPTS = 3
RETRYABLE = frozenset({'api_rate_limit', 'api_unavailable', 'api_timeout', 'interrupted'})


@dataclass(frozen=True)
class Claim:
    batch_id: object
    token: str
    snapshot: dict
    attempts: int
    started_at: str
    cache_only: bool = False


class BackfillError(Exception):
    """Only safe, fixed error codes may be logged or stored as errors."""


def _by_field(labels):
    return {item['field']: item for item in labels}


def merge_response(labels: list, payload: object) -> list:
    """Validate the narrow response and preserve every out-of-scope source label."""
    if validate_annotation(labels):
        raise ValueError('invalid_source_labels')
    original = _by_field(labels)
    if (original[CHILD]['state'] != 'unknown'
            or original[PARENT]['state'] not in ('observed', 'unknown')):
        raise ValueError('ineligible_source_labels')
    if (not isinstance(payload, dict) or set(payload) != {'labels'}
            or not isinstance(payload['labels'], list) or len(payload['labels']) != 2):
        raise ValueError('invalid_subtype_response_shape')
    if any(not isinstance(item, dict) for item in payload['labels']):
        raise ValueError('invalid_subtype_response_shape')
    fields = [item.get('field') for item in payload['labels']]
    if fields != [PARENT, CHILD]:
        raise ValueError('invalid_subtype_response_fields')
    parent, child = payload['labels']
    if parent.get('state') not in ('observed', 'unknown') or child.get('state') not in ('observed', 'unknown'):
        raise ValueError('invalid_subtype_response_state')
    if original[PARENT]['state'] == 'observed' and parent != original[PARENT]:
        raise ValueError('observed_parent_changed')
    if original[PARENT]['state'] == 'unknown' and parent.get('state') == 'observed':
        if child.get('state') != 'observed':
            raise ValueError('observed_parent_requires_observed_child')
        if not isinstance(parent.get('reason'), str) or not parent['reason'].startswith('视觉推断：'):
            raise ValueError('visual_parent_requires_reason')
    if child.get('state') == 'observed':
        if not isinstance(child.get('reason'), str) or not child['reason'].startswith('视觉推断：'):
            raise ValueError('visual_subtype_requires_reason')
    response_by_field = {PARENT: parent, CHILD: child}
    candidate = [deepcopy(response_by_field.get(item['field'], item)) for item in labels]
    if validate_annotation(candidate):
        raise ValueError('invalid_merged_labels')
    # A successful inability to estimate is terminal but does not rewrite reasons.
    return candidate if child['state'] == 'observed' else deepcopy(labels)


_ACTIVE_REPRESENTATIVE = '''EXISTS (
    SELECT 1 FROM aerial.annotation_batch_videos bv
    JOIN aerial.videos v ON v.sha256=bv.video_sha256
    WHERE bv.batch_id=b.batch_id AND bv.video_sha256=b.representative_video_sha256
      AND v.deleted_at IS NULL)'''

_ELIGIBLE = f'''b.processing_info->>'status'='completed'
    AND b.validation_passed AND b.validation_errors='[]'::jsonb
    AND b.review_status='pending' AND b.schema_version=%s
    AND jsonb_array_length(b.labels)=27
    AND b.labels @> '[{{"field":"top_road_subcategory","state":"unknown","values":[]}}]'::jsonb
    AND EXISTS (SELECT 1 FROM jsonb_array_elements(b.labels) parent
        WHERE parent->>'field'='top_road_category' AND parent->>'state' IN ('observed','unknown'))
    AND {_ACTIVE_REPRESENTATIVE}'''


def _set_task(connection, batch_id, task, *, audit=None):
    if audit is None:
        connection.execute(f'''UPDATE aerial.annotation_batches SET processing_info=
            jsonb_set(processing_info,'{{{TASK_KEY}}}',%s,true) WHERE batch_id=%s''',
                           (Jsonb(task), batch_id))
    else:
        connection.execute(f'''UPDATE aerial.annotation_batches SET processing_info=
            jsonb_set(processing_info,'{{{TASK_KEY}}}',%s,true) || jsonb_build_object(
            '{HISTORY_KEY}',COALESCE(processing_info->'{HISTORY_KEY}','[]'::jsonb) || %s)
            WHERE batch_id=%s''', (Jsonb(task), Jsonb([audit]), batch_id))


def claim_batch(connection) -> Claim | None:
    """Claim one eligible source revision without hiding its current publication."""
    while True:
        with connection.transaction():
            row = connection.execute(f'''SELECT b.* FROM aerial.annotation_batches b
                WHERE {_ELIGIBLE}
                AND COALESCE(b.processing_info->'{TASK_KEY}'->>'status','') <> 'labeling'
                AND (
                  b.processing_info->'{TASK_KEY}' IS NULL
                  OR b.processing_info->'{TASK_KEY}'->>'prompt_version' IS DISTINCT FROM %s
                  OR COALESCE(b.processing_info->'{TASK_KEY}'->>'result_revision',
                              b.processing_info->'{TASK_KEY}'->>'input_revision','') <> b.label_revision::text
                  OR (b.processing_info->'{TASK_KEY}'->>'status'='cache_replay'
                      AND (b.processing_info->'{TASK_KEY}'->>'attempts')::int >= %s)
                  OR (b.processing_info->'{TASK_KEY}'->>'status'='retry'
                      AND (b.processing_info->'{TASK_KEY}'->>'attempts')::int < %s
                      AND COALESCE((b.processing_info->'{TASK_KEY}'->>'retry_at')::timestamptz,
                                   '-infinity') <= now()))
                ORDER BY b.created_at,b.batch_id FOR UPDATE OF b SKIP LOCKED LIMIT 1''',
                (SCHEMA_VERSION, ROAD_SUBTYPE_PROMPT_VERSION, MAX_ATTEMPTS, MAX_ATTEMPTS)).fetchone()
            if row is None:
                return None
            old = row['processing_info'].get(TASK_KEY, {})
            same_target = (old.get('prompt_version') == ROAD_SUBTYPE_PROMPT_VERSION
                           and old.get('input_revision') == row['label_revision'])
            attempts = old.get('attempts', 0) if same_target else 0
            cache_only = same_target and old.get('status') == 'cache_replay' and attempts >= MAX_ATTEMPTS
            next_attempt = attempts if cache_only else attempts + 1
            token, started = str(uuid4()), utc_now()
            task = dict(prompt_version=ROAD_SUBTYPE_PROMPT_VERSION, method='visual_estimation',
                        status='labeling', attempts=next_attempt, cache_only=cache_only, claim_token=token,
                        input_revision=row['label_revision'], started_at=started)
            if validate_annotation(row['labels']):
                task.update(status='skipped', error_code='invalid_source_labels',
                            result_revision=row['label_revision'], finished_at=utc_now())
                _set_task(connection, row['batch_id'], task)
                continue
            _set_task(connection, row['batch_id'], task)
            return Claim(row['batch_id'], token, deepcopy(row), next_attempt, started, cache_only)


def recover_interrupted(connection):
    """Recover prior job claims only while holding this job's session lock."""
    with connection.transaction():
        rows = connection.execute(f'''SELECT batch_id,processing_info FROM aerial.annotation_batches
            WHERE processing_info->'{TASK_KEY}'->>'status'='labeling' FOR UPDATE''').fetchall()
        for row in rows:
            task = {**row['processing_info'][TASK_KEY], 'error_code': 'interrupted'}
            task.update(status='retry' if task.get('attempts', 0) < MAX_ATTEMPTS else 'cache_replay',
                        retry_at=utc_now(), finished_at=utc_now())
            _set_task(connection, row['batch_id'], task)


def _request_key(digest, parent, prompt):
    data = dict(frame_sha256=digest, parent=parent, prompt_version=ROAD_SUBTYPE_PROMPT_VERSION,
                model=MODEL, schema_version=SCHEMA_VERSION,
                prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest())
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(encoded).hexdigest(), data


def _cached_response(config, claim, frame, digest, parent, prompt, *, cache_only=False):
    key, request = _request_key(digest, parent, prompt)
    directory = config.output_root / str(claim.batch_id) / 'road-subtype'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    path = directory / f'subtype-{ROAD_SUBTYPE_PROMPT_VERSION}-{key}.json'
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding='utf-8'))
            if cached.get('request') != request or not isinstance(cached.get('response'), dict):
                raise ValueError
            return cached['response'], True, key
        except (OSError, ValueError, TypeError, AttributeError):
            raise BackfillError('cache_invalid') from None
    if cache_only:
        raise BackfillError('attempt_limit_reached')
    response = call_model(config, frame, prompt=prompt, max_tokens=1024)
    value = dict(request=request, response=response, created_at=utc_now())
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=directory, prefix='.subtype-',
                                         suffix='.tmp', encoding='utf-8', delete=False) as output:
            temporary = Path(output.name)
            os.fchmod(output.fileno(), 0o600)
            json.dump(value, output, ensure_ascii=False, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        # Link only when absent: a prior immutable response can never be replaced.
        try:
            os.link(temporary, path)
        except FileExistsError:
            raise BackfillError('cache_conflict') from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return response, False, key


def _same_source(row, claim):
    before = claim.snapshot
    return (row['review_status'] == 'pending' and row['validation_passed']
            and row['validation_errors'] == [] and row['processing_info'].get('status') == 'completed'
            and row['representative_active']
            and all(row[field] == before[field] for field in (
                'label_revision', 'labels', 'schema_version', 'representative_video_sha256',
                'frame_timestamp_ms', 'frame_path', 'frame_roi', 'supplemental_frames'))
            and row['processing_info'].get('frame_sha256') == before['processing_info'].get('frame_sha256'))


def _finish(connection, claim, *, status, audit, labels=None, error_code=None, retry_seconds=60):
    with connection.transaction():
        row = connection.execute(f'''SELECT b.*,{_ACTIVE_REPRESENTATIVE} AS representative_active
            FROM aerial.annotation_batches b WHERE batch_id=%s FOR UPDATE OF b''',
                                 (claim.batch_id,)).fetchone()
        if row is None:
            return 'source_changed'
        task = row['processing_info'].get(TASK_KEY, {})
        owner = task.get('claim_token') == claim.token
        if not owner or not _same_source(row, claim):
            status, error_code, labels = 'source_changed', 'source_changed', None
        finished = utc_now()
        if labels is not None:
            row = connection.execute('''UPDATE aerial.annotation_batches SET labels=%s
                WHERE batch_id=%s RETURNING *''', (Jsonb(labels), claim.batch_id)).fetchone()
            # The existing evidence/label trigger clears validation on the first UPDATE.
            connection.execute('''UPDATE aerial.annotation_batches
                SET validation_passed=true,validation_errors='[]'::jsonb WHERE batch_id=%s''',
                               (claim.batch_id,))
        final_audit = {**audit, 'status': status, 'finished_at': finished,
                       'output_revision': row['label_revision'],
                       'new_labels': [deepcopy(item) for item in row['labels']
                                      if item['field'] in (PARENT, CHILD)]}
        if error_code:
            final_audit['error_code'] = error_code
        if not owner:
            # Preserve another owner's task while retaining this paid response in history.
            connection.execute(f'''UPDATE aerial.annotation_batches SET processing_info=
                processing_info || jsonb_build_object('{HISTORY_KEY}',
                COALESCE(processing_info->'{HISTORY_KEY}','[]'::jsonb) || %s)
                WHERE batch_id=%s''', (Jsonb([final_audit]), claim.batch_id))
            return status
        # A rejected stale attempt has not processed the newer source revision.
        result_revision = claim.snapshot['label_revision'] if status == 'source_changed' else row['label_revision']
        task = {**task, 'status': status, 'result_revision': result_revision,
                'finished_at': finished}
        if error_code:
            task['error_code'] = error_code
        if status == 'retry':
            task['retry_at'] = (datetime.now(timezone.utc) + timedelta(seconds=retry_seconds)).isoformat()
        _set_task(connection, claim.batch_id, task, audit=final_audit)
        return status


def process_claim(config, connection, claim: Claim, *, retry_seconds=60) -> str:
    """Perform one paid attempt or cached replay and commit with source comparison."""
    before = claim.snapshot
    original = _by_field(before['labels'])
    audit = dict(claim_token=claim.token, attempt=claim.attempts, method='visual_estimation',
                 model=MODEL, prompt_version=ROAD_SUBTYPE_PROMPT_VERSION,
                 schema_version=SCHEMA_VERSION, input_revision=before['label_revision'],
                 started_at=claim.started_at, cache_only=claim.cache_only,
                 old_labels=[deepcopy(item) for item in before['labels'] if item['field'] in (PARENT, CHILD)])
    try:
        frame = Path(before['frame_path'])
        try:
            digest = hashlib.sha256(frame.read_bytes()).hexdigest()
        except (OSError, TypeError):
            raise BackfillError('frame_unavailable') from None
        audit['frame_sha256'] = digest
        if digest != before['processing_info'].get('frame_sha256'):
            raise BackfillError('frame_digest_mismatch')
        prompt = build_road_subtype_prompt(original[PARENT])
        response, cached, key = _cached_response(config, claim, frame, digest, original[PARENT], prompt,
                                               cache_only=claim.cache_only)
        audit.update(raw_model_response=response.get('raw_content'), usage=response.get('usage', {}),
                     cached_response=cached, request_sha256=key)
        try:
            if response.get('model') != MODEL or not isinstance(response.get('raw_content'), str):
                raise ValueError('invalid_model_response')
            labels = merge_response(before['labels'], json.loads(response['raw_content']))
        except (ValueError, TypeError, KeyError):
            audit['validation_errors'] = ['invalid_subtype_response']
            return _finish(connection, claim, status='validation_failed', audit=audit,
                           error_code='invalid_subtype_response')
        if hashlib.sha256(frame.read_bytes()).hexdigest() != digest:
            raise BackfillError('frame_digest_mismatch')
        changed = labels != before['labels']
        return _finish(connection, claim, status='applied' if changed else 'no_estimate',
                       audit=audit, labels=labels if changed else None)
    except APIError as error:
        retry = error.code in RETRYABLE and claim.attempts < MAX_ATTEMPTS and not error.fatal
        result = _finish(connection, claim, status='retry' if retry else 'failed', audit=audit,
                         error_code=error.code, retry_seconds=retry_seconds)
        if error.fatal:
            raise
        return 'failed' if result == 'retry' else result
    except BackfillError as error:
        return _finish(connection, claim, status='failed', audit=audit, error_code=str(error))
    except Exception:
        return _finish(connection, claim, status='failed', audit=audit, error_code='worker_exception')


def _retry_delay(connection):
    row = connection.execute(f'''SELECT MIN(COALESCE(
        (b.processing_info->'{TASK_KEY}'->>'retry_at')::timestamptz,now())) AS next_at
        FROM aerial.annotation_batches b WHERE {_ELIGIBLE}
        AND b.processing_info->'{TASK_KEY}'->>'prompt_version'=%s
        AND b.processing_info->'{TASK_KEY}'->>'status'='retry'
        AND (b.processing_info->'{TASK_KEY}'->>'attempts')::int < %s''',
        (SCHEMA_VERSION, ROAD_SUBTYPE_PROMPT_VERSION, MAX_ATTEMPTS)).fetchone()
    return None if row['next_at'] is None else max(0, (row['next_at'] - datetime.now(timezone.utc)).total_seconds())


def run(config, *, concurrency=4, limit=0, stop_event=None, retry_seconds=60) -> int:
    """Drain eligible work with one connection per thread and an optional claim cap."""
    if concurrency < 1 or concurrency > 32 or limit < 0:
        raise ValueError('Invalid concurrency or claim limit')
    stop = stop_event if stop_event is not None else threading.Event()
    lock = threading.Lock()
    claimed, fatal = 0, False
    os.umask(0o077)
    with psycopg.connect(config.writer_dsn, autocommit=True, connect_timeout=10,
                         row_factory=dict_row) as coordinator:
        if coordinator.execute('SELECT current_database() AS name').fetchone()['name'] != 'aerial_db':
            raise ValueError('Wrong target database')
        if not coordinator.execute('SELECT pg_try_advisory_lock(%s) AS locked', (LOCK_ID,)).fetchone()['locked']:
            log('subtype_already_running')
            return 0
        recover_interrupted(coordinator)

        def consume():
            nonlocal claimed, fatal
            with psycopg.connect(config.writer_dsn, autocommit=True, connect_timeout=10,
                                 row_factory=dict_row) as connection:
                while not stop.is_set():
                    # This short lock makes --limit exact across all worker threads.
                    with lock:
                        if stop.is_set() or limit and claimed >= limit:
                            return
                        claim = claim_batch(connection)
                        if claim is not None:
                            claimed += 1
                    if claim is None:
                        delay = _retry_delay(connection)
                        if delay is None:
                            return
                        stop.wait(min(max(delay, 0.05), 1))
                        continue
                    try:
                        status = process_claim(config, connection, claim, retry_seconds=retry_seconds)
                        log('subtype_result', batch=str(claim.batch_id)[:8], status=status,
                            attempt=claim.attempts)
                    except APIError:
                        with lock:
                            fatal = True
                        stop.set()
                        log('subtype_queue_blocked', code='api_credentials_or_quota')
                        return

        with ThreadPoolExecutor(max_workers=min(concurrency, limit) if limit else concurrency) as executor:
            futures = [executor.submit(consume) for _ in range(min(concurrency, limit) if limit else concurrency)]
            for future in futures:
                try:
                    future.result()
                except Exception:
                    stop.set()
                    log('subtype_worker_failed', code='worker_exception')
                    return 1
        log('subtype_job_stopped', attempts=claimed, fatal=fatal, interrupted=stop.is_set() and not fatal)
        return 78 if fatal else 0


def main():
    parser = argparse.ArgumentParser(description='Backfill visually estimated road subcategories on existing frames')
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--concurrency', type=int, default=4)
    parser.add_argument('--max-batches', '--limit', dest='limit', type=int, default=0,
                        help='Maximum claimed attempts; zero drains eligible work')
    parser.add_argument('--once', action='store_true', help='Claim at most one batch for a pilot')
    args = parser.parse_args()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        if args.env_file:
            load_env_file(args.env_file)
        return run(Config.environment(), concurrency=args.concurrency,
                   limit=1 if args.once else args.limit, stop_event=stop)
    except (KeyError, ValueError):
        log('subtype_start_failed', code='configuration_error')
        return 78
    except Exception:
        log('subtype_start_failed', code='worker_exception')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
