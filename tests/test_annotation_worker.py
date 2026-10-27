"""Test batch time coverage without touching source inventory or model API."""
from datetime import datetime, timezone
from pathlib import Path
import json

import pytest

from annotation_worker import Video, group_videos, locate_candidate, video_order_key, parse_model_response


def video(name, seconds, parent='/nas/site/battery', day=1):
    return Video(bytes([day])*32, Path(parent)/name, seconds,
                 datetime(2026,10,day,tzinfo=timezone.utc), name, None, None)


def test_parent_folder_groups_all_source_clips():
    rows=[video('a.mp4',100),video('b.mp4',200),video('c.mp4',100,parent='/nas/site/other')]
    groups=group_videos(rows)
    assert sorted(map(len,groups.values()))==[1,2]


def test_cumulative_midpoint_selects_source_clip_and_local_offset():
    rows=[video('a.mp4',100),video('b.mp4',200)]
    candidate=locate_candidate(rows,0.5)
    assert candidate.video.path.name=='b.mp4'
    assert candidate.timestamp_ms==50000


def test_clip_boundary_selection_has_a_small_interior_margin():
    candidate=locate_candidate([video('a.mp4',100),video('b.mp4',100)],0.5)
    assert candidate.video.path.name=='b.mp4'
    assert 0 < candidate.timestamp_ms < 100000


def test_unknown_durations_are_not_guessed_from_file_size():
    candidate=locate_candidate([video('a.mp4',None),video('b.mp4',100)],0.5)
    assert candidate.video.path.name=='b.mp4'
    with pytest.raises(ValueError):
        locate_candidate([video('a.mp4',None)],0.5)


def test_parsed_filename_time_orders_clips_ahead_of_bad_collection_metadata():
    earlier=video('DJI_20261001080000_001_V.MP4',10,day=2)
    later=video('DJI_20261001090000_002_V.MP4',10,day=1)
    assert sorted([later,earlier],key=video_order_key)==[earlier,later]


def test_only_official_model_response_with_complete_content_is_accepted():
    response={'model':'deepseek-flash','choices':[{'finish_reason':'stop','message':{'content':'{"labels": []}'}}],'usage':{'total_tokens':10}}
    assert parse_model_response(response)['raw_content']=='{"labels": []}'
    response['model']='other-provider'
    with pytest.raises(ValueError):
        parse_model_response(response)


@pytest.mark.parametrize('custom', [False, True])
def test_model_transport_sends_selected_prompt_and_response_budget_without_exposing_key(tmp_path, monkeypatch, custom):
    import annotation_worker as worker
    from types import SimpleNamespace
    config = SimpleNamespace(api_key='test-secret')
    frame = tmp_path / 'frame.jpg'
    frame.write_bytes(b'test-jpeg-bytes')
    requests = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit):
            return json.dumps({'model':'deepseek-flash','choices':[{'finish_reason':'stop','message':{'content':'{"labels": []}'}}]}).encode()
    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            return Response()
    monkeypatch.setattr(worker.urllib.request, 'build_opener', lambda *_: Opener())
    result = worker.call_model(config, frame, prompt='two-road-fields-only', max_tokens=1024) if custom else worker.call_model(config, frame)
    payload = json.loads(requests[0].data)
    assert payload['messages'][0]['content'][0]['text'] == ('two-road-fields-only' if custom else worker.build_annotation_prompt())
    assert payload['max_tokens'] == (1024 if custom else 8192)
    assert payload['messages'][0]['content'][1]['image_url']['url'].startswith('data:image/jpeg;base64,')
    assert payload['thinking'] == {'type': 'disabled'}
    assert payload['model'] == 'deepseek-flash'
    assert 'test-secret' not in json.dumps(result)
