"""Pure label-contract tests; no model calls, databases, or application settings."""

import ast
from copy import deepcopy
from functools import lru_cache
import importlib.util
import json
from pathlib import Path

import pytest


PROJECT = Path(__file__).resolve().parents[1]
FIELD_CODES = [
    "top_road_category", "top_road_subcategory",
    "road.surface.material", "road.surface.condition",
    "road.geometry.slope", "road.geometry.curvature", "road.geometry.crossfall",
    "road.lane.count", "road.lane.type", "road.lane.width", "road.edge.type",
    "road.intersection.type", "traffic.signal", "traffic.sign", "traffic.marking",
    "roadside.facility", "traffic.special_facility", "dynamic.motor_vehicle",
    "dynamic.vru", "dynamic.animal", "dynamic.obstacle", "dynamic.accident_vehicle",
    "environment.weather", "environment.particulate", "environment.light.source",
    "environment.light.intensity", "environment.temperature",
]
UNCALIBRATED = [
    "road.geometry.slope", "road.geometry.crossfall", "road.lane.width",
    "environment.temperature",
]


@lru_cache(maxsize=1)
def contract():
    path = PROJECT / "annotation_schema.py"
    assert path.is_file(), "Missing normalized annotation contract"
    spec = importlib.util.spec_from_file_location("annotation_schema", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def unknown_frame():
    """A hand-enumerated complete fixture, independent of schema definitions."""
    return {"labels": [
        {"field": code, "state": "unknown", "values": [], "scope": "frame",
         "origin": "model", "evidence": "representative", "reason": "单帧无法判断"}
        for code in FIELD_CODES
    ]}


def label(payload, code):
    return next(item for item in payload["labels"] if item["field"] == code)


def observe(payload, code, values, **extras):
    item = label(payload, code)
    item.update(state="observed", values=values, **extras)
    item.pop("reason", None)
    if "reason" in extras:
        item["reason"] = extras["reason"]
    return item


def test_complete_unknown_frame_is_valid_and_extracts_all_27_fields():
    schema = contract()
    payload = unknown_frame()
    assert schema.validate_annotation(payload) == []
    extracted = schema.extract_labels(payload)
    assert [item["field"] for item in extracted] == FIELD_CODES
    assert len(extracted) == 27
    assert schema.validate_annotation(extracted) == []


def test_extraction_rejects_invalid_payload_and_returns_independent_values():
    schema = contract()
    payload = unknown_frame()
    observe(payload, "road.surface.material", ["沥青"])
    extracted = schema.extract_labels(payload)
    label({"labels": extracted}, "road.surface.material")["values"].append("混凝土")
    assert label(payload, "road.surface.material")["values"] == ["沥青"]
    payload["labels"].pop()
    with pytest.raises(ValueError, match="environment.temperature"):
        schema.extract_labels(payload)


def test_taxonomy_retains_original_leaf_values_and_cardinality():
    schema = contract()
    # Literal evaluation reads only safe constants, never executes the old job.
    tree = ast.parse((PROJECT / "step2_vlm_label.py").read_text(encoding="utf-8"))
    source_constants = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {
                    "TOP_LEVEL_CONFIG", "LABEL_SCHEMA", "MULTI_SELECT_FIELDS"
                }:
                    source_constants[target.id] = ast.literal_eval(node.value)
    definitions = schema.FIELD_DEFINITIONS
    json.dumps(definitions, ensure_ascii=False)
    assert [field["code"] for field in definitions] == FIELD_CODES
    leaves = [
        (section, group, name, values)
        for section, groups in source_constants["LABEL_SCHEMA"].items()
        for group, fields in groups.items()
        for name, values in fields.items()
    ]
    assert len(leaves) == 25
    for field, (section, group, name, values) in zip(definitions[2:], leaves):
        assert field["original_path"] == [section, group, name]
        assert field["values"] == values
        expected_multi = (group, name) in source_constants["MULTI_SELECT_FIELDS"]
        if field["code"] == "road.edge.type":
            expected_multi = True  # Explicit new schema policy for visible boundaries.
        assert field["multi"] is expected_multi
    assert definitions[0]["values"] == list(source_constants["TOP_LEVEL_CONFIG"])
    assert schema.TOP_LEVEL_CONFIG == source_constants["TOP_LEVEL_CONFIG"]
    assert {field["code"] for field in definitions if field["unobservable"]} == set(UNCALIBRATED)


@pytest.mark.parametrize("payload", [None, "labels", 1, True, {}, {"labels": {}}, {"labels": "[]"}])
def test_wrong_root_shape_is_rejected_without_crashing(payload):
    assert contract().validate_annotation(payload)


def test_missing_duplicate_and_unknown_fields_are_rejected():
    schema = contract()
    payload = unknown_frame()
    payload["labels"].pop()
    assert any("environment.temperature" in error for error in schema.validate_annotation(payload))
    payload = unknown_frame()
    payload["labels"].append(deepcopy(payload["labels"][0]))
    assert any("duplicate" in error.lower() for error in schema.validate_annotation(payload))
    payload = unknown_frame()
    payload["labels"][0]["field"] = "invented.field"
    assert any("invented.field" in error for error in schema.validate_annotation(payload))


@pytest.mark.parametrize("replacement", [None, "沥青", [], 1])
def test_label_entry_must_be_an_object(replacement):
    payload = unknown_frame()
    payload["labels"][0] = replacement
    assert contract().validate_annotation(payload)


@pytest.mark.parametrize("key", ["field", "state", "values", "scope", "origin", "evidence"])
def test_label_mandatory_property_cannot_be_missing(key):
    payload = unknown_frame()
    payload["labels"][0].pop(key)
    assert contract().validate_annotation(payload)


@pytest.mark.parametrize("key,value", [
    ("values", "沥青"), ("values", None), ("values", {}),
    ("values", ["invented"]), ("values", [1]), ("values", [[]]),
    ("state", "likely"), ("state", []), ("field", []),
    ("scope", "batch"), ("origin", "human"), ("evidence", "all_videos"),
    ("reason", 0), ("reason", ""),
])
def test_invalid_types_enums_and_provenance_are_rejected(key, value):
    payload = unknown_frame()
    item = observe(payload, "road.surface.material", ["沥青"])
    item[key] = value
    assert contract().validate_annotation(payload)


def test_unknown_extra_properties_are_rejected_at_both_levels():
    payload = unknown_frame()
    payload["confidence"] = 0.99
    assert contract().validate_annotation(payload)
    payload = unknown_frame()
    payload["labels"][0]["certainty"] = "certain"
    assert contract().validate_annotation(payload)


def test_single_selection_and_duplicate_values_are_rejected():
    payload = unknown_frame()
    observe(payload, "road.surface.material", ["沥青", "混凝土"])
    assert contract().validate_annotation(payload)
    payload = unknown_frame()
    observe(payload, "road.lane.type", ["普通车道", "普通车道"])
    assert contract().validate_annotation(payload)


def test_original_multi_selection_accepts_visible_multiple_types():
    payload = unknown_frame()
    observe(payload, "road.lane.type", ["普通车道", "非机动车道"])
    observe(payload, "traffic.marking", ["实线", "虚线"])
    observe(payload, "dynamic.motor_vehicle", ["轿车", "卡车/货车"])
    assert contract().validate_annotation(payload) == []


def test_road_edges_accept_multiple_boundaries_only_with_a_reason():
    payload = unknown_frame()
    item = observe(payload, "road.edge.type", ["路缘石", "护栏 (金属)"])
    assert contract().validate_annotation(payload)
    item["reason"] = "主道路两侧分别可见路缘石和金属护栏"
    assert contract().validate_annotation(payload) == []


@pytest.mark.parametrize("code", UNCALIBRATED)
@pytest.mark.parametrize("state", ["observed", "not_observed", "unlabeled", "not_applicable"])
def test_uncalibrated_fields_must_be_unknown(code, state):
    payload = unknown_frame()
    item = label(payload, code)
    item["state"] = state
    item["values"] = []
    if state == "observed":
        item["values"] = {
            "road.geometry.slope": ["平路"], "road.geometry.crossfall": ["无横坡"],
            "road.lane.width": ["标准"], "environment.temperature": ["极热 (> 20℃)"],
        }[code]
    assert contract().validate_annotation(payload)


@pytest.mark.parametrize("code", [*UNCALIBRATED, "road.geometry.curvature"])
def test_unmeasurable_unknown_fields_require_reason(code):
    payload = unknown_frame()
    label(payload, code).pop("reason")
    assert contract().validate_annotation(payload)


def test_curvature_accepts_visual_straight_but_rejects_measurement_bins():
    payload = unknown_frame()
    observe(payload, "road.geometry.curvature", ["直线"])
    assert contract().validate_annotation(payload) == []
    observe(payload, "road.geometry.curvature", ["弯道 (曲率<0.01)"])
    assert contract().validate_annotation(payload)


@pytest.mark.parametrize("state", ["not_observed", "unlabeled", "not_applicable"])
def test_curvature_cannot_report_missing_measurement_as_absence(state):
    payload = unknown_frame()
    label(payload, "road.geometry.curvature")["state"] = state
    assert contract().validate_annotation(payload)


@pytest.mark.parametrize("state", ["unknown", "not_observed", "unlabeled", "not_applicable"])
def test_non_observed_states_cannot_carry_values(state):
    payload = unknown_frame()
    label(payload, "dynamic.motor_vehicle").update(state=state, values=["轿车"])
    assert contract().validate_annotation(payload)


def test_observed_requires_values_and_absence_uses_not_observed():
    payload = unknown_frame()
    label(payload, "traffic.signal").update(state="observed", values=[])
    assert contract().validate_annotation(payload)
    observe(payload, "traffic.signal", ["无"])
    assert contract().validate_annotation(payload)
    label(payload, "traffic.signal").update(state="not_observed", values=[])
    assert contract().validate_annotation(payload) == []


def test_subcategory_requires_its_observed_parent_category():
    payload = unknown_frame()
    observe(payload, "top_road_subcategory", ["主干路"])
    assert contract().validate_annotation(payload)
    observe(payload, "top_road_category", ["公路"])
    assert contract().validate_annotation(payload)
    observe(payload, "top_road_category", ["城市道路"])
    assert contract().validate_annotation(payload) == []


def test_prompt_supplies_full_taxonomy_and_a_valid_full_response_shape():
    schema = contract()
    prompt = schema.build_annotation_prompt()
    decoder = json.JSONDecoder()
    definitions, _ = decoder.raw_decode(prompt.split("【字段定义 JSON】\n", 1)[1])
    template, _ = decoder.raw_decode(prompt.split("【完整输出结构 JSON】\n", 1)[1])
    assert [item["code"] for item in definitions] == FIELD_CODES
    assert len(template["labels"]) == 27
    assert schema.validate_annotation(template) == []


def test_prompt_excludes_legacy_absence_values_without_changing_original_taxonomy():
    schema = contract()
    original_definitions = deepcopy(schema.FIELD_DEFINITIONS)
    prompt = schema.build_annotation_prompt()
    definitions, _ = json.JSONDecoder().raw_decode(
        prompt.split("【字段定义 JSON】\n", 1)[1]
    )
    assert any("无" in item["values"] for item in original_definitions)
    assert all("无" not in item["values"] for item in definitions)
    assert schema.FIELD_DEFINITIONS == original_definitions
    assert schema.PROMPT_VERSION == "battery-frame-v4"


def test_prompt_absence_examples_validate_with_empty_values():
    schema = contract()
    prompt = schema.build_annotation_prompt()
    examples, _ = json.JSONDecoder().raw_decode(
        prompt.split("【未观察到示例 JSON】\n", 1)[1]
    )
    assert [item["field"] for item in examples] == [
        "environment.particulate", "traffic.signal", "dynamic.motor_vehicle",
    ]
    payload = unknown_frame()
    for example in examples:
        assert example["state"] == "not_observed"
        assert example["values"] == []
        label(payload, example["field"]).update(example)
    assert schema.validate_annotation(payload) == []


def test_prompt_positive_examples_validate_with_the_full_template():
    schema = contract()
    prompt = schema.build_annotation_prompt()
    decoder = json.JSONDecoder()
    examples, _ = decoder.raw_decode(prompt.split("【可观察正向示例 JSON】\n", 1)[1])
    payload, _ = decoder.raw_decode(prompt.split("【完整输出结构 JSON】\n", 1)[1])
    assert [item["field"] for item in examples] == [
        "top_road_category", "top_road_subcategory", "road.surface.condition",
    ]
    for example in examples:
        label(payload, example["field"]).update(example)
    assert label(payload, "top_road_category")["state"] == "observed"
    assert label(payload, "top_road_category")["values"] == ["城市道路"]
    assert label(payload, "top_road_subcategory")["state"] == "observed"
    assert label(payload, "top_road_subcategory")["values"] == ["主干路"]
    assert label(payload, "top_road_subcategory")["reason"].startswith('视觉推断：')
    assert label(payload, "road.surface.condition")["state"] == "observed"
    assert label(payload, "road.surface.condition")["values"] == ["干燥"]
    assert schema.validate_annotation(payload) == []


def test_prompt_provides_complete_parent_mapping_for_subcategory_selection():
    schema = contract()
    prompt = schema.build_annotation_prompt()
    decoder = json.JSONDecoder()
    parents, _ = decoder.raw_decode(prompt.split("【道路大类与允许子类 JSON】\n", 1)[1])
    assert parents == schema.TOP_LEVEL_CONFIG
    assert parents["城市道路"] == ["快速路", "主干路", "次干路", "支路", "街巷"]
    assert "开放道路" in parents["区域"]
    assert "开放道路" not in parents["城市道路"]
    payload, _ = decoder.raw_decode(prompt.split("【完整输出结构 JSON】\n", 1)[1])
    observe(payload, "top_road_category", ["城市道路"])
    observe(payload, "top_road_subcategory", ["开放道路"])
    assert schema.validate_annotation(payload)
    observe(payload, "top_road_subcategory", ["主干路"])
    assert schema.validate_annotation(payload) == []


@pytest.mark.parametrize('parent_state,parent_values', [('observed', ['城市道路']), ('unknown', [])])
def test_subtype_prompt_limits_output_to_road_pair_and_preserves_existing_parent_context(parent_state, parent_values):
    schema = contract()
    parent = {'field': 'top_road_category', 'state': parent_state, 'values': parent_values,
              'scope': 'frame', 'origin': 'model', 'evidence': 'representative', 'reason': '原始道路依据'}
    original = deepcopy(parent)
    prompt = schema.build_road_subtype_prompt(parent)
    decoder = json.JSONDecoder()
    context, _ = decoder.raw_decode(prompt.split('【现有道路大类 JSON】\n', 1)[1])
    response, _ = decoder.raw_decode(prompt.split('【输出结构 JSON】\n', 1)[1])
    allowed, _ = decoder.raw_decode(prompt.split('【道路大类与允许子类 JSON】\n', 1)[1])
    assert parent == original == context
    assert [x['field'] for x in response['labels']] == ['top_road_category', 'top_road_subcategory']
    assert response['labels'][0] == parent
    assert allowed['城市道路'] == ['快速路', '主干路', '次干路', '支路', '街巷']
    assert schema.ROAD_SUBTYPE_PROMPT_VERSION == 'road-subtype-visual-v1'
    full = unknown_frame()
    for entry in response['labels']:
        label(full, entry['field']).update(entry)
    assert schema.validate_annotation(full) == []


@pytest.mark.parametrize('parent', [None, {}, {'field': 'top_road_category', 'state': 'observed', 'values': ['invented']}])
def test_subtype_prompt_rejects_unsupported_parent_context(parent):
    with pytest.raises(ValueError):
        contract().build_road_subtype_prompt(parent)


@pytest.mark.parametrize("code", FIELD_CODES)
def test_unknown_requires_reason_for_every_field(code):
    payload = unknown_frame()
    label(payload, code).pop("reason")
    assert contract().validate_annotation(payload)


@pytest.mark.parametrize("code", [
    "top_road_category", "road.surface.material", "dynamic.motor_vehicle",
    "environment.weather",
])
def test_not_applicable_requires_reason_for_any_field(code):
    payload = unknown_frame()
    item = label(payload, code)
    item.update(state="not_applicable")
    item.pop("reason")
    assert contract().validate_annotation(payload)
    item["reason"] = "本帧没有适用的道路场景"
    assert contract().validate_annotation(payload) == []
