"""Strict candidate annotations for one representative aerial frame.

The taxonomy is a safe literal snapshot of ``step2_vlm_label.py`` constants,
extracted with AST/literal_eval; importing this module executes no legacy job.
Single/multiple cardinalities preserve the original taxonomy, with one explicit
v1 change: road.edge.type permits multiple visible boundaries with a reason.
No validator coercion, measured geometry, or batch-wide inference is performed.
"""

from copy import deepcopy
import json


SCHEMA_VERSION = "aerial_annotation_v1"
PROMPT_VERSION = "battery-frame-v3"

TOP_LEVEL_CONFIG = {'区域': ['封闭园区', '交通管制区域', '开放道路'],
 '城市道路': ['快速路', '主干路', '次干路', '支路', '街巷'],
 '公路': ['高速公路', '一级公路', '二级公路', '三级公路', '四级公路'],
 '乡村道路': ['村道', '其他乡村内部道路'],
 '其他道路': ['厂矿', '林区', '港口', '专用道路'],
 '停车区域': ['室内停车场', '室外停车场', '路侧停车位'],
 '自动驾驶场景': ['封闭场景', '半封闭场景', '开放场景']}

FIELD_DEFINITIONS = [{'code': 'top_road_category',
  'name': '道路大类',
  'values': ['区域', '城市道路', '公路', '乡村道路', '其他道路', '停车区域', '自动驾驶场景'],
  'multi': False,
  'unobservable': False},
 {'code': 'top_road_subcategory',
  'name': '道路子类',
  'values': ['封闭园区',
             '交通管制区域',
             '开放道路',
             '快速路',
             '主干路',
             '次干路',
             '支路',
             '街巷',
             '高速公路',
             '一级公路',
             '二级公路',
             '三级公路',
             '四级公路',
             '村道',
             '其他乡村内部道路',
             '厂矿',
             '林区',
             '港口',
             '专用道路',
             '室内停车场',
             '室外停车场',
             '路侧停车位',
             '封闭场景',
             '半封闭场景',
             '开放场景'],
  'multi': False,
  'unobservable': False},
 {'code': 'road.surface.material',
  'name': '表面类型',
  'original_path': ['一、道路静态环境', '1.2 道路表面', '表面类型'],
  'values': ['沥青', '混凝土', '土路', '碎石', '冰雪路面', '金属板'],
  'multi': False,
  'unobservable': False},
 {'code': 'road.surface.condition',
  'name': '表面状态',
  'original_path': ['一、道路静态环境', '1.2 道路表面', '表面状态'],
  'values': ['干燥', '潮湿', '积水', '积雪', '结冰', '泥泞'],
  'multi': False,
  'unobservable': False},
 {'code': 'road.geometry.slope',
  'name': '坡度',
  'original_path': ['一、道路静态环境', '1.3 道路几何', '坡度'],
  'values': ['平路', '上坡', '下坡', '起伏路'],
  'multi': False,
  'unobservable': True},
 {'code': 'road.geometry.curvature',
  'name': '曲率',
  'original_path': ['一、道路静态环境', '1.3 道路几何', '曲率'],
  'values': ['直线', '弯道 (曲率<0.01)', '弯道 (0.01<曲率<0.05)', '弯道 (曲率>0.05)'],
  'multi': True,
  'unobservable': False},
 {'code': 'road.geometry.crossfall',
  'name': '横坡',
  'original_path': ['一、道路静态环境', '1.3 道路几何', '横坡'],
  'values': ['正常排水坡度', '反超高', '无横坡'],
  'multi': False,
  'unobservable': True},
 {'code': 'road.lane.count',
  'name': '最宽车道数量',
  'original_path': ['一、道路静态环境', '1.4 包含车道特征', '最宽车道数量'],
  'values': ['单车道', '双车道', '三车道', '四车道及以上'],
  'multi': False,
  'unobservable': False},
 {'code': 'road.lane.type',
  'name': '车道类型',
  'original_path': ['一、道路静态环境', '1.4 包含车道特征', '车道类型'],
  'values': ['普通车道', '公交专用道', 'HOV车道', '潮汐车道', '应急车道', '非机动车道', '人行道', '汇入匝道', '汇出匝道'],
  'multi': True,
  'unobservable': False},
 {'code': 'road.lane.width',
  'name': '车道宽度',
  'original_path': ['一、道路静态环境', '1.4 包含车道特征', '车道宽度'],
  'values': ['标准', '狭窄', '超宽'],
  'multi': True,
  'unobservable': True},
 {'code': 'road.edge.type',
  'name': '边缘类型',
  'original_path': ['一、道路静态环境', '1.5 道路边缘', '边缘类型'],
  'values': ['路缘石', '护栏 (金属)', '护栏 (混凝土)', '草地/泥土', '无物理隔离'],
  'multi': True,
  'unobservable': False},
 {'code': 'road.intersection.type',
  'name': '交叉类型',
  'original_path': ['一、道路静态环境', '1.6 道路交叉', '交叉类型'],
  'values': ['路段 (无交叉)', '平面交叉 (十字)', '平面交叉 (丁字)', '平面交叉 (畸形)', '大型环岛 (出入口数 > 4)', '小型环岛', '立体交叉'],
  'multi': True,
  'unobservable': False},
 {'code': 'traffic.signal',
  'name': '信号灯',
  'original_path': ['二、交通设施', '2.1 交通控制', '信号灯'],
  'values': ['有', '无'],
  'multi': False,
  'unobservable': False},
 {'code': 'traffic.sign',
  'name': '标志牌',
  'original_path': ['二、交通设施', '2.1 交通控制', '标志牌'],
  'values': ['限速', '禁止', '指示', '警告', '施工', '无'],
  'multi': False,
  'unobservable': False},
 {'code': 'traffic.marking',
  'name': '地面标签',
  'original_path': ['二、交通设施', '2.1 交通控制', '地面标签'],
  'values': ['实线', '虚线', '双黄线', '导流线', '斑马线', '标线磨损'],
  'multi': True,
  'unobservable': False},
 {'code': 'roadside.facility',
  'name': '设施',
  'original_path': ['二、交通设施', '2.2 路侧与周边环境', '设施'],
  'values': ['无',
             '路灯',
             '电线杆',
             '隔音墙',
             '路边树木',
             '路边停车位',
             '地面停车场出入口',
             '隧道出入口',
             '居民楼',
             '商场',
             '学校',
             '医院',
             '公园',
             '绿化带'],
  'multi': True,
  'unobservable': False},
 {'code': 'traffic.special_facility',
  'name': '类型',
  'original_path': ['二、交通设施', '2.3 特殊设施', '类型'],
  'values': ['收费站', '检查站', '施工区域围挡', '减速带', '无'],
  'multi': False,
  'unobservable': False},
 {'code': 'dynamic.motor_vehicle',
  'name': '类型',
  'original_path': ['三、动态目标 (路面状况)', '3.1 机动车', '类型'],
  'values': ['轿车', '客车/巴士', '卡车/货车', '特种车辆 (警)', '特种车辆(消)', '特种车辆(救)', '工程车辆'],
  'multi': True,
  'unobservable': False},
 {'code': 'dynamic.vru',
  'name': '类型',
  'original_path': ['三、动态目标 (路面状况)', '3.2 VRU', '类型'],
  'values': ['自行车', '电动车', '三轮车', '行人', '无'],
  'multi': True,
  'unobservable': False},
 {'code': 'dynamic.animal',
  'name': '类型',
  'original_path': ['三、动态目标 (路面状况)', '3.3 动物', '类型'],
  'values': ['有', '无'],
  'multi': False,
  'unobservable': False},
 {'code': 'dynamic.obstacle',
  'name': '类型',
  'original_path': ['三、动态目标 (路面状况)', '3.4 障碍物', '类型'],
  'values': ['落石', '遗洒物', '倒伏树木', '锥桶', '无'],
  'multi': True,
  'unobservable': False},
 {'code': 'dynamic.accident_vehicle',
  'name': '类型',
  'original_path': ['三、动态目标 (路面状况)', '3.5 事故车辆', '类型'],
  'values': ['有', '无'],
  'multi': False,
  'unobservable': False},
 {'code': 'environment.weather',
  'name': '类型',
  'original_path': ['四、大气环境', '4.1 天气', '类型'],
  'values': ['晴', '多云', '阴', '雨 (小/中/大)', '雪', '雾', '冰雹'],
  'multi': False,
  'unobservable': False},
 {'code': 'environment.particulate',
  'name': '类型',
  'original_path': ['四、大气环境', '4.2 颗粒物', '类型'],
  'values': ['无', '雾霾', '沙尘', '烟尘'],
  'multi': False,
  'unobservable': False},
 {'code': 'environment.light.source',
  'name': '来源',
  'original_path': ['四、大气环境', '4.3 光照', '来源'],
  'values': ['自然光', '人工照明', '混合光'],
  'multi': False,
  'unobservable': False},
 {'code': 'environment.light.intensity',
  'name': '强度',
  'original_path': ['四、大气环境', '4.3 光照', '强度'],
  'values': ['正常', '强光/逆光', '弱光/昏暗', '黑暗'],
  'multi': False,
  'unobservable': False},
 {'code': 'environment.temperature',
  'name': '估算',
  'original_path': ['四、大气环境', '4.4 气温', '估算'],
  'values': ['极寒 (< -20℃)', '寒冷 (-20℃ ~ -10℃)', '舒适 (-10℃ ~ 10℃)', '炎热 (10℃ ~ 20℃)', '极热 (> 20℃)'],
  'multi': False,
  'unobservable': True}]


STATES = ("observed", "not_observed", "unknown", "unlabeled", "not_applicable")
_FIELDS_BY_CODE = {definition["code"]: definition for definition in FIELD_DEFINITIONS}
_REQUIRED_PROPERTIES = {"field", "state", "values", "scope", "origin", "evidence"}
_ALLOWED_PROPERTIES = _REQUIRED_PROPERTIES | {"reason"}


def _labels_from_payload(payload):
    if isinstance(payload, list):
        return payload, []
    if not isinstance(payload, dict):
        return None, ["annotation must be an object containing labels or a labels array"]
    errors = []
    if set(payload) != {"labels"}:
        errors.append("annotation object must contain exactly the labels property")
    labels = payload.get("labels")
    if not isinstance(labels, list):
        errors.append("labels must be an array")
        return None, errors
    return labels, errors


def validate_annotation(payload: dict | list) -> list[str]:
    """Return every detected contract error without modifying or coercing input.

    Accept either the model JSON envelope ``{"labels": [...]}`` or its labels
    array as stored in JSONB. Only an empty error list means the candidate has
    passed format validation; it still requires human review.
    """
    labels, errors = _labels_from_payload(payload)
    if labels is None:
        return errors
    seen = set()
    by_code = {}
    for index, item in enumerate(labels):
        prefix = f"labels[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix}: label must be an object")
            continue
        missing = _REQUIRED_PROPERTIES - set(item)
        extras = set(item) - _ALLOWED_PROPERTIES
        if missing:
            errors.append(f"{prefix}: missing properties {', '.join(sorted(missing))}")
        if extras:
            errors.append(f"{prefix}: unknown properties {', '.join(sorted(map(str, extras)))}")
        code = item.get("field")
        if not isinstance(code, str) or code not in _FIELDS_BY_CODE:
            errors.append(f"{prefix}: unknown field {code!r}")
            definition = None
        else:
            prefix += f" ({code})"
            definition = _FIELDS_BY_CODE[code]
            if code in seen:
                errors.append(f"{prefix}: duplicate field")
            else:
                by_code[code] = item
            seen.add(code)
        state = item.get("state")
        if not isinstance(state, str) or state not in STATES:
            errors.append(f"{prefix}: state must be one of {', '.join(STATES)}")
        for key, expected in (
            ("scope", "frame"), ("origin", "model"), ("evidence", "representative")
        ):
            if item.get(key) != expected:
                errors.append(f"{prefix}: {key} must be {expected!r}")
        reason = item.get("reason")
        valid_reason = isinstance(reason, str) and bool(reason.strip())
        if "reason" in item and not valid_reason:
            errors.append(f"{prefix}: reason must be a nonempty string when provided")
        if state in ("unknown", "not_applicable") and not valid_reason:
            errors.append(f"{prefix}: {state} requires a reason")
        values = item.get("values")
        if not isinstance(values, list):
            errors.append(f"{prefix}: values must be an array")
        else:
            if state == "observed" and not values:
                errors.append(f"{prefix}: observed requires at least one value")
            elif state in STATES and state != "observed" and values:
                errors.append(f"{prefix}: {state} requires empty values")
            if definition is not None:
                if not definition["multi"] and len(values) > 1:
                    errors.append(f"{prefix}: single selection permits at most one value")
                selected = set()
                for value in values:
                    if not isinstance(value, str) or value not in definition["values"]:
                        errors.append(f"{prefix}: invalid enum value {value!r}")
                        continue
                    if value in selected:
                        errors.append(f"{prefix}: duplicate value {value!r}")
                    selected.add(value)
                    # Original absence enums remain documented in the taxonomy,
                    # while normalized absence is represented by state + [].
                    if value == "无":
                        errors.append(f"{prefix}: absence must use not_observed with empty values")
                if code == "road.edge.type" and len(values) > 1 and not valid_reason:
                    errors.append(f"{prefix}: multiple boundaries require a reason")
        if definition is not None and definition["unobservable"]:
            if state != "unknown":
                errors.append(f"{prefix}: uncalibrated field must be unknown")
            if not valid_reason:
                errors.append(f"{prefix}: uncalibrated unknown requires a reason")
        if code == "road.geometry.curvature":
            if state not in ("observed", "unknown"):
                errors.append(f"{prefix}: curvature must be observed straight or unknown")
            if state == "observed" and values != ["直线"]:
                errors.append(f"{prefix}: only visual straight is observable; measured curvature bins require unknown")
            if state == "unknown" and not valid_reason:
                errors.append(f"{prefix}: unmeasured curvature requires a reason")

    for code in _FIELDS_BY_CODE:
        if code not in seen:
            errors.append(f"missing field: {code}")

    category = by_code.get("top_road_category", {})
    subcategory = by_code.get("top_road_subcategory", {})
    if subcategory.get("state") == "observed":
        category_values = category.get("values")
        subcategory_values = subcategory.get("values")
        parent_is_valid = (
            category.get("state") == "observed"
            and isinstance(category_values, list)
            and len(category_values) == 1
            and isinstance(category_values[0], str)
            and category_values[0] in TOP_LEVEL_CONFIG
        )
        if not parent_is_valid:
            errors.append("top_road_subcategory: observed subtype requires an observed parent category")
        elif isinstance(subcategory_values, list):
            for value in subcategory_values:
                if value not in TOP_LEVEL_CONFIG[category_values[0]]:
                    errors.append("top_road_subcategory: value does not belong to selected top_road_category")
    return errors


def extract_labels(payload: dict | list) -> list[dict]:
    """Return independent label objects only after strict validation.

    Raises ValueError with the detected errors instead of returning or silently
    repairing malformed model output. The raw model response should be retained
    separately by the caller regardless of validation status.
    """
    errors = validate_annotation(payload)
    if errors:
        raise ValueError("Invalid annotation: " + "; ".join(errors))
    labels = payload["labels"] if isinstance(payload, dict) else payload
    return deepcopy(labels)


def build_annotation_prompt() -> str:
    """Build the fixed taxonomy and frame-only rules for one battery candidate."""
    # Preserve the original literal taxonomy above, but send the normalized
    # positive choices to the model so absence is never an observed enum value.
    definitions = deepcopy(FIELD_DEFINITIONS)
    for definition in definitions:
        if "无" in definition["values"]:
            definition["values"] = [value for value in definition["values"] if value != "无"]
            definition["absence_state"] = "not_observed"
    absence_examples = [
        {
            "field": code, "state": "not_observed", "values": [],
            "scope": "frame", "origin": "model", "evidence": "representative",
        }
        for code in ("environment.particulate", "traffic.signal", "dynamic.motor_vehicle")
    ]
    positive_examples = [
        {
            "field": "top_road_category", "state": "observed", "values": ["城市道路"],
            "scope": "frame", "origin": "model", "evidence": "representative",
            "reason": "主体道路位于可辨认的城市建成区",
        },
        {
            "field": "top_road_subcategory", "state": "unknown", "values": [],
            "scope": "frame", "origin": "model", "evidence": "representative",
            "reason": "单帧无法核实主干路、次干路等道路功能等级",
        },
        {
            "field": "road.surface.condition", "state": "observed", "values": ["干燥"],
            "scope": "frame", "origin": "model", "evidence": "representative",
            "reason": "可见路面干燥，未见可辨认的积水或潮湿痕迹",
        },
    ]
    template = {"labels": [
        {
            "field": definition["code"], "state": "unknown", "values": [],
            "scope": "frame", "origin": "model", "evidence": "representative",
            "reason": "当前代表帧无法可靠判断",
        }
        for definition in FIELD_DEFINITIONS
    ]}
    rules = """你是一名无人机航拍道路图像标注员。输入是一块电池采集文件夹的一张代表帧。
只标注这张代表帧中可见的事实，生成待人工审核的候选结果。仅输出 JSON 对象，禁止 Markdown、解释和额外全局字段。

【固定格式与状态】
1. 输出 {"labels": [...]}，必须恰好包含以下全部 27 个字段，每个字段一次，不得遗漏或重复。
2. 每项仅允许 field、state、values、scope、origin、evidence 和可选 reason。field 使用稳定 code，values 始终为数组。
3. state 仅允许 observed、not_observed、unknown、unlabeled、not_applicable。
   state 始终填写这五个英文状态码之一，绝不能填写任何中文枚举值。
   “城市道路”“主干路”“沥青”“干燥”等是 values 的选项，不能写成 state；有可见依据时使用 state="observed" 并将中文值放进 values 数组。
   observed：本帧确实可见，values 非空；只能选字段定义里的中文值。multi=false 只选一值，multi=true 可选多值。
   not_observed：相关区域清楚可见，确认本帧未看到该目标或设施，values=[]。原始选项“无”统一使用此状态，不放入 values。
   unknown：遮挡、分辨率不足、视角限制或无可靠依据，values=[]，用 reason 说明原因；无法判断不能写成未出现。
   unlabeled：明确未完成该项标注，values=[]；不能用它代替 unknown。
   not_applicable：该字段对本帧道路场景不适用，values=[]，用 reason 说明。
4. scope 必须为 frame，origin 必须为 model，evidence 必须为 representative。
   unknown 和 not_applicable 均必须提供非空 reason，其他状态的 reason 可选，填写时也必须为非空字符串。
   不输出 confidence、certainty、时间范围、虚构视频/补帧/位置/测量证据等其他字段。

【观察边界】
5. 不把一张代表帧的动态目标、天气或路面状态扩展为整个电池批次，也不能判断全批次的出现次数、持续时间或覆盖范围。
6. 选择图像主体道路及其主要行驶方向作为车道判断对象。最宽车道数量只统计该方向可见道路最宽断面；不把对向车道、辅路、匝道或邻近道路相加。
   车道类别只在可辨认的标线、标志或结构支持时选择；不能由位置猜测公交、HOV、潮汐、应急等用途。
7. top_road_category 与 top_road_subcategory 均为单选，子类必须属于所选大类；大类 unknown 时子类不得 observed。
   无法凭本帧确认道路等级、用途或管制属性时用 unknown，不凭车道数量猜测高速公路、主干路等法定等级。
   如果可确认“城市道路”大类，却无法核实主干路/次干路等功能等级，则大类 observed + ["城市道路"]，子类 unknown + [] 并提供 reason。
   所有 observed 子类必须从下方父类映射中对应所选大类的列表选择；“开放道路”属于“区域”，不得与“城市道路”大类组合。
8. 坡度 road.geometry.slope、横坡 road.geometry.crossfall、车道宽度 road.lane.width、气温 environment.temperature
   本流程没有标定、实测、定位或传感器数据，必须 state=unknown、values=[] 且提供 reason。
   禁止从透视、植被、衣着、积雪、光照等估算温度或测量几何参数，也不能凭看起来平坦选择平路或无横坡。
9. 曲率 road.geometry.curvature：可见道路明确直线时允许 observed + ["直线"]；弯曲或无法判断时用 unknown + [] 并说明无实测曲率。
   禁止选择任何数值曲率区间。保留原始多选定义，但本流程可观察值仅“直线”。
10. 道路边缘 road.edge.type 在本版固定为多选：只有主体道路多种边界均清楚可见才选多值，并用 reason 指明各边界观察依据。
    其他字段的单选/多选规则严格按定义，不把单选自动改为多选，不替换枚举、不修正模型输出。

【正向枚举与未观察到】
字段定义的 values 仅列出正向可观察选项，不包含原始“无”选项；absence_state 元数据指明未看到时应使用 not_observed。
例如确认本帧没有可见颗粒物、信号灯或机动车时，输出以下 state 和空 values；禁止 observed + ["无"]。
"""
    return (
        rules
        + "\n【未观察到示例 JSON】\n"
        + json.dumps(absence_examples, ensure_ascii=False, indent=2)
        + "\n\n以下正向示例仅演示英文 state 与中文 values 的位置；只有图像支持时才能使用示例的值。"
        + "\n【可观察正向示例 JSON】\n"
        + json.dumps(positive_examples, ensure_ascii=False, indent=2)
        + "\n\n以下映射给出各道路大类允许的子类；不能确认等级或属性时使用 unknown，不选最接近的子类。"
        + "\n【道路大类与允许子类 JSON】\n"
        + json.dumps(TOP_LEVEL_CONFIG, ensure_ascii=False, indent=2)
        + "\n\n【字段定义 JSON】\n"
        + json.dumps(definitions, ensure_ascii=False, indent=2)
        + "\n\n【完整输出结构 JSON】\n"
        + json.dumps(template, ensure_ascii=False, indent=2)
        + "\n以上为完整结构模板。依据本帧更新各项状态、值和原因；保留全部字段和固定来源，返回单个 JSON 对象。"
    )
