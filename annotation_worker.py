"""Resumable PostgreSQL batch annotation; original videos are always read-only."""
from __future__ import annotations

import argparse
import base64
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request

from PIL import Image, ImageFilter, ImageStat
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from shapely.geometry import Point, shape
from shapely.strtree import STRtree
from shapely import make_valid

from annotation_schema import (
    PROMPT_VERSION, SCHEMA_VERSION, build_annotation_prompt,
    extract_labels, validate_annotation,
)

MODEL = 'deepseek-flash'
LOCK_ID = 634129872541
TERMINAL = {'completed', 'validation_failed', 'skipped'}
RETRYABLE = {'api_rate_limit', 'api_unavailable', 'api_timeout', 'interrupted', 'worker_exception'}
STOP = threading.Event()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(event: str, **values) -> None:
    print(json.dumps({'at': utc_now(), 'event': event, **values}, ensure_ascii=False), flush=True)


def load_env_file(path: Path) -> None:
    if path.stat().st_mode & 0o077:
        raise ValueError('Worker secret configuration must have mode 0600')
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#') or '=' not in line:
            continue
        name, value = line.split('=', 1)
        if name.startswith(('NDS_', 'DEEPSEEK_')):
            parts = shlex.split(value)
            os.environ[name] = parts[0] if parts else ''


@dataclass(frozen=True)
class Config:
    reader_dsn: str = field(repr=False)
    writer_dsn: str = field(repr=False)
    api_key: str = field(repr=False)
    output_root: Path
    boundary_path: Path
    poll_seconds: float = 10
    discovery_seconds: float = 300

    @classmethod
    def environment(cls):
        if os.environ.get('DEEPSEEK_VISION_MODEL', MODEL) != MODEL:
            raise ValueError('Only the configured DeepSeek Flash vision model is supported')
        return cls(
            os.environ['NDS_AERIAL_DATABASE_URL'], os.environ['NDS_ANNOTATION_DATABASE_URL'],
            os.environ['DEEPSEEK_API_KEY'], Path(os.environ['NDS_ANNOTATION_OUTPUT_ROOT']),
            Path(os.environ['NDS_CITY_BOUNDARIES_PATH']),
            max(1, float(os.environ.get('NDS_ANNOTATION_POLL_SECONDS', 10))),
            max(60, float(os.environ.get('NDS_ANNOTATION_DISCOVERY_SECONDS', 300))),
        )


@dataclass(frozen=True)
class Video:
    sha256: bytes
    path: Path
    duration: float | None
    collected_at: datetime | None
    original_name: str
    longitude: float | None
    latitude: float | None


@dataclass(frozen=True)
class FrameCandidate:
    video: Video
    timestamp_ms: int
    fraction: float


def video_order_key(video: Video):
    match = re.search(r'DJI_(\d{14})', video.original_name)
    try:
        timestamp = datetime.strptime(match[1], '%Y%m%d%H%M%S') if match else None
    except ValueError:
        timestamp = None
    if timestamp is None and video.collected_at is not None:
        timestamp = video.collected_at.replace(tzinfo=None)
    return timestamp or datetime.max, video.original_name, video.sha256


def group_videos(videos: list[Video]) -> dict[str, list[Video]]:
    groups = defaultdict(list)
    for video in videos:
        if video.path.is_absolute():
            groups[str(video.path.parent)].append(video)
    return {folder: sorted(rows, key=video_order_key) for folder, rows in groups.items()}


def locate_candidate(videos: list[Video], fraction: float) -> FrameCandidate:
    eligible = [video for video in sorted(videos, key=video_order_key)
                if video.duration is not None and math.isfinite(video.duration) and video.duration > 0]
    total = sum(video.duration for video in eligible)
    if not eligible or not 0 <= fraction <= 1:
        raise ValueError('No valid duration for representative selection')
    target = total * fraction
    elapsed = 0.0
    for index, video in enumerate(eligible):
        if target < elapsed + video.duration or index == len(eligible) - 1:
            margin = min(1.0, video.duration * 0.05)
            seconds = min(video.duration - margin, max(margin, target - elapsed))
            return FrameCandidate(video, max(0, round(seconds * 1000)), fraction)
        elapsed += video.duration
    raise ValueError('No representative candidate')


class CityResolver:
    def __init__(self, path: Path):
        data = json.loads(path.read_text())
        polygons, names = [], []
        for feature in data['features']:
            geometry = shape(feature['geometry'])
            if not geometry.is_valid:
                geometry = make_valid(geometry)
            if not geometry.is_empty:
                polygons.append(geometry)
                name = feature['properties']['地名']
                names.append({'福州市': 'Fuzhou (Fujian)', '抚州市': 'Fuzhou (Jiangxi)'}.get(name, name))
        self.tree, self.names = STRtree(polygons), names

    def city(self, video: Video) -> str:
        if video.longitude is None or video.latitude is None:
            return '缺少坐标'
        if not (math.isfinite(video.longitude) and math.isfinite(video.latitude)
                and -180 <= video.longitude <= 180 and -90 <= video.latitude <= 90):
            return '边界外或坐标异常'
        names = {self.names[int(index)] for index in self.tree.query(
            Point(video.longitude, video.latitude), predicate='intersects')}
        return next(iter(names)) if len(names) == 1 else '边界外或坐标异常'


def read_videos(connection, batch_id=None) -> list[Video]:
    query = '''SELECT v.sha256,v.storage_path,v.duration_seconds,v.collected_at,
                      v.original_name,v.collect_lon,v.collect_lat
               FROM aerial.videos v'''
    parameters = ()
    if batch_id is not None:
        query += ' JOIN aerial.annotation_batch_videos bv ON bv.video_sha256=v.sha256'
        parameters = (batch_id,)
    query += ' WHERE v.deleted_at IS NULL'
    if batch_id is not None:
        query += ' AND bv.batch_id=%s'
    rows = connection.execute(query, parameters).fetchall()
    return [Video(bytes(row[0]), Path(row[1]), float(row[2]) if row[2] is not None else None,
                  row[3], row[4] or Path(row[1]).name,
                  float(row[5]) if row[5] is not None else None,
                  float(row[6]) if row[6] is not None else None) for row in rows]


def discover(connection, videos: list[Video], resolver: CityResolver) -> dict:
    groups = group_videos(videos)
    inserted = 0
    with connection.transaction():
        for folder, members in groups.items():
            cities = Counter(resolver.city(video) for video in members)
            known = [(name, count) for name, count in cities.items()
                     if name not in {'缺少坐标', '边界外或坐标异常'}]
            city = max(known, key=lambda pair: pair[1])[0] if known else cities.most_common(1)[0][0]
            warnings = []
            if sum(video.duration or 0 for video in members) > 3600:
                warnings.append('duration_over_one_hour')
            dates = {video_order_key(video)[0].date() for video in members
                     if video_order_key(video)[0] != datetime.max}
            if len(dates) > 1:
                warnings.append('multiple_collection_dates')
            if any(video.duration is None for video in members):
                warnings.append('unknown_duration')
            if len(known) > 1:
                warnings.append('multiple_cities')
            metadata = {'city': city, 'grouping_warning': warnings}
            row = connection.execute('''INSERT INTO aerial.annotation_batches(source_directory,processing_info)
                VALUES (%s,%s) ON CONFLICT(source_directory) DO NOTHING RETURNING batch_id''',
                (folder, Jsonb({'status': 'queued', 'attempts': 0, **metadata}))).fetchone()
            if row:
                inserted += 1
                batch_id = row[0]
            else:
                batch_id = connection.execute('SELECT batch_id FROM aerial.annotation_batches WHERE source_directory=%s', (folder,)).fetchone()[0]
                connection.execute('UPDATE aerial.annotation_batches SET processing_info=processing_info || %s WHERE batch_id=%s', (Jsonb(metadata), batch_id))
            for video in members:
                connection.execute('''INSERT INTO aerial.annotation_batch_videos(batch_id,video_sha256)
                    VALUES (%s,%s) ON CONFLICT(video_sha256) DO NOTHING''', (batch_id, video.sha256))
        # These failed groups contain no usable frames, so no API requests occur.
        for folder in groups:
            if not os.access(folder, os.R_OK | os.X_OK):
                connection.execute('''UPDATE aerial.annotation_batches SET processing_info=processing_info || %s
                    WHERE source_directory=%s AND processing_info->>'status'='queued'
                    AND review_status <> 'approved' ''',
                    (Jsonb({'status': 'skipped', 'error_code': 'source_unavailable', 'finished_at': utc_now()}), folder))
    result = {'folders': len(groups), 'videos': len(videos), 'new_batches': inserted}
    log('discovery', **result)
    return result


def claim_batch(connection):
    with connection.transaction():
        row = connection.execute('''SELECT batch_id,processing_info FROM aerial.annotation_batches
            WHERE review_status <> 'approved' AND (processing_info->>'attempts')::int < 3 AND (
                processing_info->>'status'='queued' OR (
                    processing_info->>'status'='failed' AND (processing_info->>'attempts')::int < 3
                    AND processing_info->>'error_code'=ANY(%s)
                    AND COALESCE((processing_info->>'retry_at')::timestamptz,'-infinity') <= now()))
            AND EXISTS (SELECT 1 FROM aerial.annotation_batch_videos bv
                JOIN aerial.videos v ON v.sha256=bv.video_sha256
                WHERE bv.batch_id=annotation_batches.batch_id AND v.deleted_at IS NULL)
            ORDER BY created_at,batch_id FOR UPDATE SKIP LOCKED LIMIT 1''', (list(RETRYABLE),)).fetchone()
        if not row:
            return None
        batch_id, info = row
        info = {**info, 'status': 'extracting', 'attempts': info.get('attempts', 0) + 1,
                'started_at': utc_now(), 'heartbeat_at': utc_now()}
        for key in ('error_code', 'error_message', 'retry_at', 'finished_at'):
            info.pop(key, None)
        connection.execute('UPDATE aerial.annotation_batches SET processing_info=%s WHERE batch_id=%s', (Jsonb(info), batch_id))
        return batch_id


def update_info(connection, batch_id, **changes):
    connection.execute('UPDATE aerial.annotation_batches SET processing_info=processing_info || %s WHERE batch_id=%s',
                       (Jsonb({'heartbeat_at': utc_now(), **changes}), batch_id))


class Heartbeat:
    def __init__(self, config, batch_id):
        self.config, self.batch_id, self.done = config, batch_id, threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        try:
            with psycopg.connect(self.config.writer_dsn, autocommit=True, connect_timeout=10) as connection:
                while not self.done.wait(20):
                    update_info(connection, self.batch_id)
        except Exception:
            log('heartbeat_error', code='database_unavailable')

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.done.set()
        self.thread.join(timeout=12)


def probe_duration(path: Path) -> float | None:
    try:
        result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                                 '-of', 'default=noprint_wrappers=1:nokey=1', str(path)],
                                capture_output=True, timeout=25, check=True, text=True)
        duration = float(result.stdout.strip())
        return duration if math.isfinite(duration) and duration > 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def extract_candidate(candidate: FrameCandidate, destination: Path) -> float:
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-threads', '2',
                    '-ss', str(candidate.timestamp_ms / 1000), '-i', str(candidate.video.path),
                    '-frames:v', '1', '-threads', '1', '-q:v', '2', '-y', str(destination)],
                   capture_output=True, timeout=45, check=True)
    with Image.open(destination) as source:
        preview = source.convert('L')
        preview.thumbnail((640, 640))
        # Rank visible structure, not daylight brightness: dark/poor-weather scenes remain eligible.
        edges = preview.filter(ImageFilter.FIND_EDGES)
        return ImageStat.Stat(edges).var[0]


def select_frame(videos: list[Video], directory: Path):
    prepared = [replace(video, duration=probe_duration(video.path))
                if video.duration is None or video.duration <= 0 else video for video in videos]
    candidates = [locate_candidate(prepared, fraction) for fraction in (0.5, 1/6, 5/6)]
    readable = [candidate for candidate in candidates if os.access(candidate.video.path, os.R_OK)]
    eligible = [video for video in prepared if video.duration is not None and video.duration > 0
                and math.isfinite(video.duration) and os.access(video.path, os.R_OK)]
    if not readable and eligible:
        readable = [locate_candidate(eligible, fraction) for fraction in (0.5, 1/6, 5/6)]
    if not readable:
        raise PermissionError('No readable source candidates')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    ranked = []
    with tempfile.TemporaryDirectory(dir=directory, prefix='candidates-') as temporary:
        for index, candidate in enumerate(readable):
            path = Path(temporary) / f'{index}.jpg'
            try:
                score = extract_candidate(candidate, path)
                ranked.append((score, -abs(candidate.fraction - 0.5), candidate, path))
            except (OSError, subprocess.SubprocessError):
                continue
        if not ranked:
            used = {candidate.video.path for candidate in readable}
            for index, video in enumerate([video for video in eligible if video.path not in used][:3]):
                candidate = locate_candidate([video], 0.5)
                path = Path(temporary) / f'fallback-{index}.jpg'
                try:
                    ranked.append((extract_candidate(candidate, path), 0, candidate, path))
                except (OSError, subprocess.SubprocessError):
                    continue
        if not ranked:
            raise RuntimeError('No decodable source candidate')
        score, _, selected, source = max(ranked, key=lambda item: item[:2])
        original_bytes = source.read_bytes()
        original = directory / ('original-' + hashlib.sha256(original_bytes).hexdigest() + '.jpg')
        if not original.exists():
            original.write_bytes(original_bytes)
        with Image.open(original) as image:
            image.thumbnail((1920, 1920), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            image.convert('RGB').save(output, format='JPEG', quality=92)
            frame_bytes = output.getvalue()
        digest = hashlib.sha256(frame_bytes).hexdigest()
        frame = directory / ('frame-' + digest + '.jpg')
        if not frame.exists():
            frame.write_bytes(frame_bytes)
    return selected, frame, digest, round(score, 3)


class APIError(Exception):
    def __init__(self, code, fatal=False):
        super().__init__(code)
        self.code, self.fatal = code, fatal


def parse_model_response(response: dict) -> dict:
    if response.get('model') != MODEL:
        raise ValueError('Unexpected response model')
    choices = response.get('choices')
    if not isinstance(choices, list) or not choices:
        raise ValueError('No response choices')
    choice = choices[0]
    raw = choice.get('message', {}).get('content')
    if choice.get('finish_reason') != 'stop' or not isinstance(raw, str) or not raw.strip():
        raise ValueError('Incomplete model output')
    usage = response.get('usage', {})
    safe_usage = {name: value for name, value in usage.items()
                  if name in {'prompt_tokens', 'completion_tokens', 'total_tokens'}
                  and isinstance(value, int) and value >= 0}
    return {'raw_content': raw, 'model': MODEL, 'usage': safe_usage}


def call_model(config: Config, frame: Path) -> dict:
    encoded = base64.b64encode(frame.read_bytes()).decode('ascii')
    payload = {'model': MODEL, 'messages': [{'role': 'user', 'content': [
        {'type': 'text', 'text': build_annotation_prompt()},
        {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + encoded,
                                         'detail': 'original'}}]}],
        'thinking': {'type': 'disabled'}, 'response_format': {'type': 'json_object'},
        'temperature': 0.1, 'max_tokens': 8192}
    request = urllib.request.Request('https://api.deepseek.com/chat/completions',
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={'Authorization': 'Bearer ' + config.api_key, 'Content-Type': 'application/json'})
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=90) as response:
            body = response.read(8 * 1024 * 1024)
        result = parse_model_response(json.loads(body))
        result['raw_content'] = result['raw_content'].replace(config.api_key, '[redacted]')
        return result
    except urllib.error.HTTPError as error:
        code = {401: 'api_auth', 402: 'api_quota', 429: 'api_rate_limit'}.get(error.code, 'api_unavailable')
        raise APIError(code, fatal=error.code in (401, 402)) from None
    except (TimeoutError, urllib.error.URLError):
        raise APIError('api_timeout') from None
    except (ValueError, TypeError, KeyError):
        raise APIError('invalid_json') from None


def atomic_json(path: Path, value: dict):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def process_batch(config, writer, reader, batch_id):
    started = time.monotonic()
    with Heartbeat(config, batch_id):
        videos = read_videos(reader, batch_id)
        if not videos:
            update_info(writer, batch_id, status='skipped', error_code='source_unavailable', finished_at=utc_now())
            return
        directory = config.output_root / str(batch_id)
        try:
            selected, frame, digest, score = select_frame(videos, directory)
        except PermissionError:
            update_info(writer, batch_id, status='skipped', error_code='source_unavailable', finished_at=utc_now())
            return
        except ValueError:
            update_info(writer, batch_id, status='failed', error_code='no_valid_duration', finished_at=utc_now())
            return
        except (OSError, RuntimeError):
            update_info(writer, batch_id, status='failed', error_code='frame_extract_failed', finished_at=utc_now())
            return
        with writer.transaction():
            captured = writer.execute('''UPDATE aerial.annotation_batches SET representative_video_sha256=%s,
                frame_timestamp_ms=%s,frame_path=%s,selection_reason=%s,
                processing_info=processing_info || %s WHERE batch_id=%s AND review_status='pending'
                RETURNING label_revision ''',
                (selected.video.sha256, selected.timestamp_ms, str(frame),
                 '批次前中后候选中选择道路结构清晰帧；标签仅为代表帧观察',
                 Jsonb({'status': 'labeling', 'heartbeat_at': utc_now(), 'frame_sha256': digest,
                        'quality_score': score, 'selected_fraction': selected.fraction}), batch_id)).fetchone()
            if not captured:
                update_info(writer, batch_id, status='completed', finished_at=utc_now(), preserved_reviewed_result=True)
                return
            evidence_revision = captured[0]
        cache_path = directory / f'model-{PROMPT_VERSION}-{digest}.json'
        cached = None
        if cache_path.exists():
            try:
                candidate = json.loads(cache_path.read_text())
                if (candidate.get('frame_sha256') == digest and candidate.get('prompt_version') == PROMPT_VERSION
                        and candidate.get('schema_version') == SCHEMA_VERSION and candidate.get('model') == MODEL):
                    cached = candidate
            except (OSError, ValueError):
                pass
        response = cached or call_model(config, frame)
        if cached is None:
            atomic_json(cache_path, {**response, 'frame_sha256': digest,
                                    'prompt_version': PROMPT_VERSION, 'schema_version': SCHEMA_VERSION})
        try:
            payload = json.loads(response['raw_content'])
            errors = validate_annotation(payload)
        except ValueError:
            payload, errors = None, ['invalid_json']
        labels = extract_labels(payload) if not errors else []
        status = 'validation_failed' if errors else 'completed'
        info = {'status': status, 'finished_at': utc_now(), 'heartbeat_at': utc_now(),
                'model': MODEL, 'token_usage': response.get('usage', {}),
                'elapsed_seconds': round(time.monotonic() - started, 2),
                'video_count_at_annotation': len(videos), 'cached_response': cached is not None}
        if errors:
            info['error_code'] = 'invalid_labels' if payload is not None else 'invalid_json'
        with writer.transaction():
            row = writer.execute('''SELECT review_status,label_revision,representative_video_sha256,
                frame_timestamp_ms,frame_path FROM aerial.annotation_batches WHERE batch_id=%s FOR UPDATE''', (batch_id,)).fetchone()
            if row[0] in ('approved', 'rejected'):
                update_info(writer, batch_id, status='completed', finished_at=utc_now(), preserved_reviewed_result=True)
                log('reviewed_result_preserved', batch=str(batch_id)[:8])
                return
            if (row[1] != evidence_revision or bytes(row[2] or b'') != selected.video.sha256
                    or row[3] != selected.timestamp_ms or row[4] != str(frame)):
                update_info(writer, batch_id, status='failed', error_code='source_group_changed', finished_at=utc_now())
                log('evidence_change_preserved', batch=str(batch_id)[:8])
                return
            writer.execute('''UPDATE aerial.annotation_batches SET raw_model_response=%s,labels=%s,
                model_name=%s,prompt_version=%s,schema_version=%s,validation_errors=%s,
                processing_info=processing_info || %s WHERE batch_id=%s''',
                (response['raw_content'], Jsonb(labels), MODEL, PROMPT_VERSION, SCHEMA_VERSION,
                 Jsonb(errors), Jsonb(info), batch_id))
            # The evidence-change trigger resets validation, so set the checked verdict separately.
            writer.execute('UPDATE aerial.annotation_batches SET validation_passed=%s WHERE batch_id=%s',
                           (not errors, batch_id))
        log('batch_result', batch=str(batch_id)[:8], status=status, errors=len(errors),
            seconds=info['elapsed_seconds'], tokens=response.get('usage', {}).get('total_tokens', 0))


def run(config: Config, *, once=False, discover_only=False, max_batches=0):
    os.umask(0o077)
    config.output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolver = CityResolver(config.boundary_path)
    with psycopg.connect(config.writer_dsn, autocommit=True, connect_timeout=10) as writer, \
         psycopg.connect(config.reader_dsn, autocommit=True, connect_timeout=10,
                         options='-c default_transaction_read_only=on') as reader:
        if not writer.execute('SELECT pg_try_advisory_lock(%s)', (LOCK_ID,)).fetchone()[0]:
            log('already_running')
            return 0
        if writer.execute("SELECT current_database()").fetchone()[0] != 'aerial_db':
            raise ValueError('Wrong target database')
        # Previous worker no longer owns the advisory lock; interrupted work can resume.
        writer.execute('''UPDATE aerial.annotation_batches SET processing_info=processing_info || %s
            WHERE processing_info->>'status' IN ('extracting','labeling') AND review_status <> 'approved' ''',
            (Jsonb({'status': 'queued', 'error_code': 'interrupted', 'heartbeat_at': utc_now()}),))
        writer.execute('''UPDATE aerial.annotation_batches SET processing_info=processing_info || %s
            WHERE processing_info->>'status'='queued' AND (processing_info->>'attempts')::int >= 3''',
            (Jsonb({'status': 'failed', 'error_code': 'interrupted', 'finished_at': utc_now()}),))
        discover(writer, read_videos(reader), resolver)
        if discover_only:
            return 0
        processed, last_discovery = 0, time.monotonic()
        while not STOP.is_set():
            if time.monotonic() - last_discovery >= config.discovery_seconds:
                discover(writer, read_videos(reader), resolver)
                last_discovery = time.monotonic()
            batch_id = claim_batch(writer)
            if batch_id is None:
                if once or max_batches:
                    break
                STOP.wait(config.poll_seconds)
                continue
            try:
                process_batch(config, writer, reader, batch_id)
            except APIError as error:
                info = {'status': 'failed', 'error_code': error.code, 'finished_at': utc_now(),
                        'retry_at': datetime.fromtimestamp(time.time() + 60, timezone.utc).isoformat()}
                update_info(writer, batch_id, **info)
                log('batch_error', batch=str(batch_id)[:8], code=error.code)
                if error.fatal:
                    log('queue_blocked', code=error.code)
                    return 78
            except Exception:
                update_info(writer, batch_id, status='failed', error_code='worker_exception',
                            finished_at=utc_now(), retry_at=datetime.fromtimestamp(time.time()+60,timezone.utc).isoformat())
                log('batch_error', batch=str(batch_id)[:8], code='worker_exception')
            processed += 1
            if once or max_batches and processed >= max_batches:
                break
        log('worker_stopped', processed=processed)
        return 0


def main():
    parser = argparse.ArgumentParser(description='PG-only batch representative annotation')
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--discover-only', action='store_true')
    parser.add_argument('--max-batches', type=int, default=0)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    try:
        if args.env_file:
            load_env_file(args.env_file)
        return run(Config.environment(), once=args.once, discover_only=args.discover_only,
                   max_batches=args.max_batches)
    except (KeyError, ValueError) as error:
        log('worker_start_failed', error_type=type(error).__name__)
        return 78
    except Exception as error:
        log('worker_start_failed', error_type=type(error).__name__)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
