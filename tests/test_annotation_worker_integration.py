"""Worker regressions against the isolated PostgreSQL annotation fixture."""

from contextlib import nullcontext
import hashlib
import json
from pathlib import Path

from PIL import Image
import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
import pytest

import annotation_worker as worker
from annotation_schema import FIELD_DEFINITIONS
from test_annotation_schema import (
    database, connection, insert_batch, insert_video, add_membership,
    update_batch, approve_batch,
)


def unknown_labels():
    return [
        {'field': field['code'], 'state': 'unknown', 'values': [],
         'scope': 'frame', 'origin': 'model', 'evidence': 'representative',
         'reason': '单帧无法可靠判断'}
        for field in FIELD_DEFINITIONS
    ]


def reviewed_labels():
    labels = unknown_labels()
    material = next(label for label in labels if label['field'] == 'road.surface.material')
    material.update(state='observed', values=['混凝土'])
    material.pop('reason')
    return labels


@pytest.fixture
def worker_context(database, connection, tmp_path, monkeypatch):
    # The imported fixture owns an ephemeral Unix-socket PostgreSQL cluster.
    # Autocommit lets the independent worker connection observe human updates.
    connection.commit()
    connection.autocommit = True
    dsn = make_conninfo(**database)
    config = worker.Config(dsn, dsn, 'unused-test-key', tmp_path / 'frames',
                           tmp_path / 'unused-boundaries.json')
    monkeypatch.setattr(worker, 'Heartbeat', lambda *args: nullcontext())
    with psycopg.connect(**database, autocommit=True) as writer:
        writer.execute('SET ROLE ad4che')
        try:
            yield config, writer, connection
        finally:
            connection.execute('DELETE FROM aerial.annotation_batches')
            connection.execute('DELETE FROM aerial.videos')


def prepare_batch(context, monkeypatch):
    config, writer, reviewer = context
    sha256 = insert_video(reviewer)
    batch = insert_batch(reviewer)
    batch_id = batch['batch_id']
    add_membership(reviewer, batch_id, sha256)
    video = worker.Video(sha256, Path('/temporary-fixture/source.mp4'), 10,
                         None, 'source.mp4', None, None)
    monkeypatch.setattr(worker, 'read_videos', lambda *_: [video])

    def select_fixture_frame(videos, directory):
        directory.mkdir(parents=True, exist_ok=True)
        frame = directory / 'representative.jpg'
        Image.new('RGB', (16, 16), 'gray').save(frame, 'JPEG')
        digest = hashlib.sha256(frame.read_bytes()).hexdigest()
        return worker.FrameCandidate(video, 5000, 0.5), frame, digest, 1.0

    monkeypatch.setattr(worker, 'select_frame', select_fixture_frame)
    assert worker.claim_batch(writer) == batch_id
    return batch_id


def model_response():
    return {'raw_content': json.dumps({'labels': unknown_labels()}, ensure_ascii=False),
            'model': 'deepseek-flash', 'usage': {'total_tokens': 10}}


def batch_row(reviewer, batch_id):
    return reviewer.execute(
        'SELECT * FROM aerial.annotation_batches WHERE batch_id=%s',
        (batch_id,),
    ).fetchone()


def test_queued_batch_at_attempt_limit_is_not_claimed(worker_context):
    _, writer, reviewer = worker_context
    sha256 = insert_video(reviewer)
    batch = insert_batch(reviewer, processing_info=Jsonb({'status': 'queued', 'attempts': 3}))
    add_membership(reviewer, batch['batch_id'], sha256)

    assert worker.claim_batch(writer) is None
    assert batch_row(reviewer, batch['batch_id'])['processing_info']['attempts'] == 3


def test_frame_selection_falls_back_to_readable_short_clip(tmp_path, monkeypatch):
    readable = worker.Video(b'a' * 32, tmp_path / 'a-readable.mp4', 10,
                            None, 'a-readable.mp4', None, None)
    inaccessible = worker.Video(b'b' * 32, tmp_path / 'b-inaccessible.mp4', 1000,
                                None, 'b-inaccessible.mp4', None, None)
    monkeypatch.setattr(worker.os, 'access', lambda path, _: path == readable.path)

    def extract_fixture_image(candidate, destination):
        if candidate.video.path != readable.path:
            raise PermissionError('Fixture source is inaccessible')
        Image.new('RGB', (32, 32), 'gray').save(destination, 'JPEG')
        return 1.0

    monkeypatch.setattr(worker, 'extract_candidate', extract_fixture_image)
    selected, frame, digest, _ = worker.select_frame(
        [readable, inaccessible], tmp_path / 'output',
    )

    assert selected.video.sha256 == readable.sha256
    assert 0 < selected.timestamp_ms < 10000
    assert frame.is_file()
    assert hashlib.sha256(frame.read_bytes()).hexdigest() == digest


def test_reselection_preserves_existing_frame_files(tmp_path, monkeypatch):
    video = worker.Video(b'a' * 32, tmp_path / 'source.mp4', 100,
                         None, 'source.mp4', None, None)
    monkeypatch.setattr(worker.os, 'access', lambda *_: True)
    extractions = 0

    def extract_different_image(candidate, destination):
        nonlocal extractions
        extractions += 1
        Image.new('RGB', (32, 32), (extractions * 20, 30, 40)).save(destination, 'JPEG')
        return 1.0

    monkeypatch.setattr(worker, 'extract_candidate', extract_different_image)
    directory = tmp_path / 'output'
    _, first_frame, first_digest, _ = worker.select_frame([video], directory)
    first_files = {path: path.read_bytes() for path in directory.rglob('*.jpg')}
    _, second_frame, second_digest, _ = worker.select_frame([video], directory)

    assert second_digest != first_digest
    assert second_frame != first_frame
    for path, original_bytes in first_files.items():
        assert path.read_bytes() == original_bytes


def test_changed_representative_keeps_human_labels_and_terminalizes(worker_context, monkeypatch):
    config, writer, reviewer = worker_context
    batch_id = prepare_batch(worker_context, monkeypatch)
    replacement_frame = config.output_root / 'human-replacement.jpg'
    human_labels = reviewed_labels()

    def replace_evidence_during_model_call(*_):
        Image.new('RGB', (16, 16), 'blue').save(replacement_frame, 'JPEG')
        update_batch(reviewer, batch_id, frame_timestamp_ms=7000,
                     frame_path=str(replacement_frame), labels=Jsonb(human_labels))
        return model_response()

    monkeypatch.setattr(worker, 'call_model', replace_evidence_during_model_call)
    worker.process_batch(config, writer, reviewer, batch_id)
    result = batch_row(reviewer, batch_id)

    assert result['frame_path'] == str(replacement_frame)
    with Image.open(replacement_frame) as image:
        assert image.getpixel((0, 0))[2] > 200
    assert result['frame_timestamp_ms'] == 7000
    assert result['labels'] == human_labels
    assert result['raw_model_response'] is None
    assert result['validation_passed'] is False
    assert result['processing_info']['status'] not in {'queued', 'extracting', 'labeling'}
    assert result['processing_info']['error_code'] == 'source_group_changed'


def test_approval_during_model_call_preserves_review_and_completes_processing(worker_context, monkeypatch):
    config, writer, reviewer = worker_context
    batch_id = prepare_batch(worker_context, monkeypatch)
    approved_frame = config.output_root / 'human-approved.jpg'
    human_labels = reviewed_labels()

    def approve_during_model_call(*_):
        Image.new('RGB', (16, 16), 'green').save(approved_frame, 'JPEG')
        update_batch(reviewer, batch_id, frame_timestamp_ms=7000,
                     frame_path=str(approved_frame), labels=Jsonb(human_labels))
        # The real evidence trigger resets validation, just as in production.
        update_batch(reviewer, batch_id, validation_passed=True)
        approve_batch(reviewer, batch_id)
        return model_response()

    monkeypatch.setattr(worker, 'call_model', approve_during_model_call)
    worker.process_batch(config, writer, reviewer, batch_id)
    result = batch_row(reviewer, batch_id)

    assert result['review_status'] == 'approved'
    assert result['reviewer_name'] == '测试审核员'
    assert result['reviewed_at'] is not None
    assert result['frame_path'] == str(approved_frame)
    with Image.open(approved_frame) as image:
        assert image.getpixel((0, 0))[1] > 100
    assert result['frame_timestamp_ms'] == 7000
    assert result['labels'] == human_labels
    assert result['validation_passed'] is True
    assert result['raw_model_response'] is None
    assert result['processing_info']['status'] == 'completed'
