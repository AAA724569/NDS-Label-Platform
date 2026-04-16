"""
ODD 人工标注平台 v3
- 视觉风格：完全对齐「驭研科技大规模自然驾驶数据集统计平台」截图
  · 深蓝渐变 Header（扫光动画 + tech bracket）
  · KPI 卡片对齐截图四色渐变（蓝/靛/青绿/紫）+ 展开条
  · 侧边栏深蓝科技主题
- 功能同 v2，代码优化：
  · 统一 DB 上下文管理器，消除重复连接
  · 修复 time_period 逻辑（原版 h<7 未归入夜间）
  · _field 签名简化，去掉无用参数 schema_dict
  · 去掉未使用的 import shutil / parse_time

运行：conda activate nds && streamlit run dashboard_st.py
"""
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import pandas as pd
import streamlit as st

# ═══════════════════════════════════════════════════════════════════════
# i18n · 中英文切换
# ───────────────────────────────────────────────────────────────────────
# 架构：
#   1. 所有面向用户的中文字符串通过 T("中文") 查找英文翻译，未命中时
#      原样返回（安全降级）。
#   2. 当前语言保存在 st.session_state.lang ∈ {"zh", "en"}，默认 "zh"。
#   3. 顶部 brand header 提供 EN / 中文 切换按钮。
#   4. ODD 分类体系的 dict key（如 "一、道路静态环境"）保持中文不变，
#      仅在显示时通过 T() 翻译；这样数据库路径、schema 查询逻辑全部
#      不受影响。
# ═══════════════════════════════════════════════════════════════════════
I18N_EN = {
    # ── 品牌 / 导航 ───────────────────────────────────────────
    "驭研科技大规模自然驾驶数据集统计平台": "DRIVEResearch · Large-Scale Naturalistic Driving Dataset Platform",
    "📊 统计看板": "📊 Dashboard",
    "🏷️ 地点标注": "🏷️ Location Tagging",
    "📹 视频 / 场景列表": "📹 Videos / Scenes",
    "🖼️ 图片": "🖼️ Photos",
    "📋 原始数据": "📋 Raw Data",

    # ── 平台标题 ──────────────────────────────────────────────
    "驭研科技自然驾驶数据集数据统计平台":
        "DRIVEResearch Naturalistic Driving Dataset Statistics Platform",
    # 兼容旧版文件里可能出现的老标题（docstring / page_title fallback）
    "驭研科技大规模自然驾驶数据集统计平台":
        "DRIVEResearch Naturalistic Driving Dataset Statistics Platform",
    "驭研科技大规模自然驾驶数据集数据管理平台":
        "DRIVEResearch Naturalistic Driving Dataset Statistics Platform",

    # ── 顶部导航（nav）────────────────────────────────────────
    "主站":       "Main site",
    "看板":       "Dashboard",
    "标注":       "Tagging",
    "智能问答":   "AI Q&A",

    # ── AI Q&A 页面 ───────────────────────────────────────────
    "🤖 智能问答":            "🤖 AI Q&A",
    "基于 Claude 的数据集对话助手，可解答数据覆盖范围、ODD 标签分布、采集地点等问题":
        "Claude-powered dataset assistant. Ask about coverage, ODD tag distribution, collection sites, and more.",
    "请输入你的问题…":        "Ask a question about the dataset…",
    "📨 发送":                "📨 Send",
    "🤖 正在思考…":            "🤖 Thinking…",
    "请先在 Streamlit Cloud 的 Secrets 里配置 `ANTHROPIC_API_KEY`":
        "Please configure `ANTHROPIC_API_KEY` in Streamlit Cloud Secrets first.",
    "问题不能为空":            "Question cannot be empty",
    "问题过长（限 500 字）":   "Question too long (max 500 chars)",
    "调用 Claude API 失败": "Claude API call failed",
    "⚠️ 本小时请求已达上限（20 次），请稍后再试":
        "⚠️ Hourly request limit reached (20). Please try again later.",
    "示例问题":              "Example questions",
    "数据集一共覆盖了哪些城市？":        "Which cities does the dataset cover?",
    "长春的数据为什么占比这么高？":      "Why does Changchun make up such a large share?",
    "哪些 ODD 标签维度采样最稀疏？":    "Which ODD tag dimensions have the sparsest samples?",
    "高速公路和城市道路的时长分布如何？":"How is duration split between freeway and urban roads?",

    # ── KPI 卡片 ──────────────────────────────────────────────
    "视频总数": "Total Videos",
    "覆盖城市": "Cities",
    "总时长（小时）": "Total Duration (hours)",
    "总时长": "Total Duration",
    "地点数": "Locations",
    "数据总量": "Dataset Size",
    "图片地点数": "Photo Locations",
    "视频总时长 (分钟)": "Total Video Duration (min)",
    "含图片数据集": "Incl. photo datasets",
    "场景数": "Scenes",

    # ── 筛选栏 ────────────────────────────────────────────────
    "🔄 刷新数据": "🔄 Refresh",
    "🔍 筛选条件": "🔍 Filters",
    "⚙️ 高级筛选": "⚙️ Advanced Filters",
    "🌆 城市过滤": "🌆 City filter",
    "城市": "City",
    "道路主类": "Road category",
    "道路子类": "Road subcategory",
    "主类": "Category",
    "子类": "Subcategory",
    "年份": "Year",
    "来源": "Source",
    "标签维度": "Tag dimension",
    "标签值": "Tag value",
    "图表类型": "Chart type",
    "柱状图": "Bar",
    "饼图": "Pie",

    # ── Section 标题 ──────────────────────────────────────────
    "📍 地域与时间分布": "📍 Geographic & Temporal Distribution",
    "🕐 采集时段分布": "🕐 Collection Time Distribution",
    "🛣️ 道路类型分布": "🛣️ Road Type Distribution",
    "🏷️ 标签分布": "🏷️ Tag Distribution",
    "📈 交叉分析": "📈 Cross Analysis",
    "#### 🌆 各城市概览": "#### 🌆 Cities Overview",
    "#### 📍 地点卡片墙": "#### 📍 Locations Wall",
    "#### 📋 基本信息": "#### 📋 Basic Info",
    "#### 📍 静态标签": "#### 📍 Static Tags",
    "#### 🎬 动态标签": "#### 🎬 Dynamic Tags",
    "#### 📹 视频 / 场景列表": "#### 📹 Videos / Scenes",
    "#### 🖼️ 图片": "#### 🖼️ Photos",
    "#### 🛣️ 道路类型": "#### 🛣️ Road Type",
    "### 🏷️ 标签分布": "### 🏷️ Tag Distribution",
    "### 📈 交叉分析": "### 📈 Cross Analysis",
    "### 📍 地域与时间分布": "### 📍 Geographic & Temporal Distribution",
    "### 🕐 采集时段分布": "### 🕐 Collection Time Distribution",
    "### 🛣️ 道路类型分布": "### 🛣️ Road Type Distribution",

    # ── 图表坐标/标签 ────────────────────────────────────────
    "各城市标注数量": "Records per city",
    "各地点视频总时长 (Top 15)": "Total duration per location (Top 15)",
    "道路类型层级分布": "Hierarchical road-type distribution",
    "采集时段分布": "Collection time distribution",
    "数量": "Count",
    "时长 (h)": "Duration (h)",
    "时长(h)": "Duration (h)",
    "时长": "Duration",
    "时段": "Time period",
    "地点": "Location",
    "维度 1": "Dimension 1",
    "维度 2": "Dimension 2",
    "该维度暂无数据": "No data for this dimension",

    # ── 时段 ──────────────────────────────────────────────────
    "早高峰 (07-09)": "Morning peak (07–09)",
    "晚高峰 (17-19)": "Evening peak (17–19)",
    "夜间 (19-07)": "Night (19–07)",
    "日常规": "Daytime",
    "未知": "Unknown",
    "采集时段": "Time period",
    "采集时间": "Collected at",
    "采集日期": "Collection date",
    "采集小时": "Collection hour",

    # ── 状态 / 提示 ──────────────────────────────────────────
    "✅ 已保存": "✅ Saved",
    "未标注": "Unlabeled",
    "🖼 图片不存在": "🖼 Image not found",
    "← 从左侧列表中选择一个地点开始标注": "← Select a location on the left to begin tagging",
    "数据库暂无数据，请先完成标注": "Database is empty. Complete labeling first.",
    "地点数据不存在": "Location data not found",
    "地点名称不能为空": "Location name cannot be empty",
    "暂无图片地点数据。请先运行 `python step3_photo_label.py` 完成自动标注。":
        "No photo-location data yet. Run `python step3_photo_label.py` first.",

    # ── 按钮 / 控件 ──────────────────────────────────────────
    "▸ 展开详情": "▸ Details",
    "▸ 查看标签": "▸ View tags",
    "▾ 收起": "▾ Collapse",
    "💾 保存": "💾 Save",
    "💾 保存复核": "💾 Save review",
    "💾 保存新地点": "💾 Save new location",
    "手动录入": "Manual entry",
    "手动标注": "Manual label",
    "VLM自动": "VLM auto",
    "上传图片": "Upload image",
    "上传代表图 (JPG/PNG)": "Upload representative frame (JPG/PNG)",
    "查看/编辑标签": "View / edit tags",
    "图片来源": "Image source",
    "图片绝对路径": "Absolute image path",
    "输入路径": "Enter path",
    "地点名称 (英文/编号)": "Location name (English / ID)",
    "估算": "Estimated",
    "备注": "Notes",
    "填写一次后保存到本地点所有代表图。": "Fill once; saves to all frames at this location.",
    "静态标签会自动沿用（请先完成静态标签）。": "Static tags are inherited (complete static tags first).",
    "🎬 动态标签（逐图）": "🎬 Dynamic tags (per frame)",
    "📍 静态标签（整个地点）": "📍 Static tags (whole location)",
    "🛣️ 道路类型": "🛣️ Road type",

    # ── ODD Schema top level ─────────────────────────────────
    "一、道路静态环境": "Ⅰ. Static road environment",
    "二、交通设施": "Ⅱ. Traffic facilities",
    "三、动态目标": "Ⅲ. Dynamic targets",
    "三、动态目标 (路面状况)": "Ⅲ. Dynamic targets (road state)",
    "四、大气环境": "Ⅳ. Atmospheric environment",
    "四、大气环境 & 采集时段": "Ⅳ. Atmospheric environment & time",
    "🛣️ 一、道路静态环境": "🛣️ Ⅰ. Static road environment",
    "🚦 二、交通设施": "🚦 Ⅱ. Traffic facilities",
    "🚗 三、动态目标": "🚗 Ⅲ. Dynamic targets",
    "🌤️ 四、大气环境": "🌤️ Ⅳ. Atmospheric environment",
    "**一、道路静态环境**": "**Ⅰ. Static road environment**",
    "**二、交通设施**": "**Ⅱ. Traffic facilities**",
    "**三、动态目标**": "**Ⅲ. Dynamic targets**",
    "**四、大气环境 & 采集时段**": "**Ⅳ. Atmospheric environment & time**",

    # ── ODD Schema sub-sections ──────────────────────────────
    "1.2 道路表面": "1.2 Road surface",
    "1.3 道路几何": "1.3 Road geometry",
    "1.4 包含车道特征": "1.4 Lane features",
    "1.5 道路边缘": "1.5 Road edge",
    "1.6 道路交叉": "1.6 Road intersection",
    "2.1 交通控制": "2.1 Traffic control",
    "2.2 路侧与周边环境": "2.2 Roadside & surroundings",
    "2.3 特殊设施": "2.3 Special facilities",
    "3.1 机动车": "3.1 Motor vehicle",
    "3.3 动物": "3.3 Animal",
    "3.4 障碍物": "3.4 Obstacle",
    "3.5 事故车辆": "3.5 Accident vehicle",
    "4.1 天气": "4.1 Weather",
    "4.2 颗粒物": "4.2 Particulates",
    "4.3 光照": "4.3 Lighting",
    "4.4 气温": "4.4 Temperature",

    # ── Schema attribute keys ────────────────────────────────
    "表面类型": "Surface type",
    "表面状态": "Surface state",
    "坡度": "Slope",
    "曲率": "Curvature",
    "横坡": "Cross slope",
    "最宽车道数量": "Max lane count",
    "车道数量": "Lane count",
    "车道类型": "Lane type",
    "车道宽度": "Lane width",
    "边缘类型": "Edge type",
    "交叉类型": "Intersection type",
    "信号灯": "Traffic light",
    "标志牌": "Sign",
    "地面标签": "Ground marking",
    "地面标线": "Road marking",
    "设施": "Facilities",
    "类型": "Type",
    "强度": "Intensity",
    "光照强度": "Light intensity",
    "光照来源": "Light source",
    "路面状态": "Road state",
    "路面类型": "Road surface",
    "机动车类型": "Motor vehicle type",
    "VRU 类型": "VRU type",

    # ── Road categories (TOP_LEVEL_CONFIG) ───────────────────
    "区域": "Area",
    "封闭园区": "Closed campus",
    "交通管制区域": "Traffic-controlled area",
    "开放道路": "Open road",
    "城市道路": "Urban road",
    "快速路": "Urban expressway",
    "主干路": "Arterial",
    "次干路": "Sub-arterial",
    "支路": "Local road",
    "街巷": "Alley",
    "公路": "Highway",
    "高速公路": "Freeway",
    "高速路": "Freeway",
    "一级公路": "Class-1 highway",
    "二级公路": "Class-2 highway",
    "三级公路": "Class-3 highway",
    "四级公路": "Class-4 highway",
    "乡村道路": "Rural road",
    "村道": "Village road",
    "其他乡村内部道路": "Other rural road",
    "其他道路": "Other roads",
    "厂矿": "Industrial / mining",
    "林区": "Forest",
    "港口": "Port",
    "专用道路": "Dedicated road",
    "停车区域": "Parking",
    "室内停车场": "Indoor parking lot",
    "室外停车场": "Outdoor parking lot",
    "路侧停车位": "Roadside parking",
    "自动驾驶场景": "AD scenario",
    "封闭场景": "Closed scenario",
    "半封闭场景": "Semi-closed scenario",
    "开放场景": "Open scenario",

    # ── Schema values ────────────────────────────────────────
    "沥青": "Asphalt", "混凝土": "Concrete", "土路": "Dirt",
    "碎石": "Gravel", "冰雪路面": "Snow / ice", "金属板": "Metal plate",
    "平路": "Flat", "上坡": "Uphill", "下坡": "Downhill", "起伏路": "Undulating",
    "直线": "Straight",
    "弯道 (曲率<0.01)": "Curve (κ<0.01)",
    "弯道 (0.01<曲率<0.05)": "Curve (0.01<κ<0.05)",
    "弯道 (曲率>0.05)": "Curve (κ>0.05)",
    "正常排水坡度": "Normal drainage", "反超高": "Reverse superelevation", "无横坡": "None",
    "单车道": "Single lane", "双车道": "Two lanes", "三车道": "Three lanes",
    "四车道及以上": "Four+ lanes",
    "普通车道": "Regular lane", "公交专用道": "Bus lane",
    "潮汐车道": "Reversible lane", "应急车道": "Emergency lane",
    "非机动车道": "Non-motor lane", "人行道": "Sidewalk",
    "汇入匝道": "Merge ramp", "汇出匝道": "Diverge ramp",
    "标准": "Standard", "狭窄": "Narrow", "超宽": "Wide",
    "路缘石": "Curb", "护栏 (金属)": "Barrier (metal)",
    "护栏 (混凝土)": "Barrier (concrete)", "草地/泥土": "Grass / dirt",
    "无物理隔离": "No physical barrier",
    "路段 (无交叉)": "Segment (no intersection)",
    "平面交叉 (十字)": "At-grade (4-way)",
    "平面交叉 (丁字)": "At-grade (T-junction)",
    "平面交叉 (畸形)": "At-grade (irregular)",
    "大型环岛 (出入口数 > 4)": "Roundabout (>4 exits)",
    "小型环岛": "Mini roundabout",
    "立体交叉": "Grade-separated", "立交桥": "Overpass",
    "十字路口": "4-way intersection", "T型路口": "T-junction",
    "合流区": "Merge zone", "环岛+高速路出入口": "Roundabout + ramp",
    "有": "Yes", "无": "None",
    "限速": "Speed limit", "禁止": "Prohibition", "指示": "Direction",
    "警告": "Warning", "施工": "Construction",
    "实线": "Solid line", "虚线": "Dashed line", "双黄线": "Double yellow",
    "导流线": "Channelizing line", "斑马线": "Crosswalk", "标线磨损": "Worn marking",
    "路灯": "Streetlight", "电线杆": "Power pole", "隔音墙": "Sound barrier",
    "路边树木": "Roadside trees", "路边停车位": "Roadside parking",
    "地面停车场出入口": "Parking entrance", "隧道出入口": "Tunnel portal",
    "居民楼": "Residential", "商场": "Mall", "学校": "School",
    "医院": "Hospital", "公园": "Park", "绿化带": "Green belt",
    "收费站": "Toll station", "检查站": "Checkpoint",
    "施工区域围挡": "Construction barrier", "减速带": "Speed bump",
    "轿车": "Car", "客车/巴士": "Bus", "卡车/货车": "Truck",
    "特种车辆 (警)": "Special (police)", "特种车辆(消)": "Special (fire)",
    "特种车辆(救)": "Special (ambulance)", "工程车辆": "Construction vehicle",
    "自行车": "Bicycle", "电动车": "E-bike", "三轮车": "Tricycle", "行人": "Pedestrian",
    "干燥": "Dry", "潮湿": "Wet", "积水": "Puddle",
    "积雪": "Snow", "结冰": "Ice", "泥泞": "Muddy",
    "落石": "Rockfall", "遗洒物": "Debris", "倒伏树木": "Fallen tree", "锥桶": "Cone",
    "晴": "Clear", "多云": "Cloudy", "阴": "Overcast",
    "雨 (小/中/大)": "Rain (light / mod / heavy)", "雪": "Snow",
    "雾": "Fog", "冰雹": "Hail",
    "雾霾": "Haze", "沙尘": "Dust", "烟尘": "Smoke",
    "正常": "Normal", "强光/逆光": "Harsh / backlit",
    "弱光/昏暗": "Low light", "黑暗": "Dark",

    # ── Cities ───────────────────────────────────────────────
    "深圳": "Shenzhen", "长春": "Changchun", "香港": "Hong Kong",
    "北京": "Beijing", "上海": "Shanghai", "广州": "Guangzhou",
    "哈尔滨": "Harbin", "杭州": "Hangzhou", "武汉": "Wuhan",
    "成都": "Chengdu", "十堰": "Shiyan", "安阳": "Anyang",
    "抚州": "Fuzhou", "贵港": "Guigang",

    # ── Misc ─────────────────────────────────────────────────
    "天气": "Weather", "气温": "Temperature", "机动车": "Motor vehicle",
    "障碍物": "Obstacle", "其他": "Other", "特殊设施": "Special facility",
    "道路边缘": "Road edge", "路侧设施": "Roadside facility",
    "拥挤路段": "Congested segment", "工业区支路": "Industrial road",
    "地点列表": "Locations",
}

def T(s):
    """Translate a Chinese string to the active language.
    Unknown keys pass through unchanged (safe degradation)."""
    if not isinstance(s, str):
        return s
    lang = st.session_state.get("lang", "zh") if hasattr(st, "session_state") else "zh"
    if lang == "en":
        return I18N_EN.get(s, s)
    return s

def _t_list(values):
    """Translate every item in a sequence."""
    return [T(v) for v in values]

def _t_schema(schema):
    """Return a deep-translated copy of a schema dict (display only).
    Storage keys remain in Chinese — this only affects what users see."""
    lang = st.session_state.get("lang", "zh") if hasattr(st, "session_state") else "zh"
    if lang == "zh":
        return schema
    out = {}
    for k, v in schema.items():
        if isinstance(v, dict):
            out[T(k)] = _t_schema(v)
        elif isinstance(v, list):
            out[T(k)] = _t_list(v)
        else:
            out[T(k)] = T(v) if isinstance(v, str) else v
    return out

def section_header(title_zh: str, title_en: str, sub_zh: str = "", sub_en: str = ""):
    """Render a driveresearch.tech-style section header: clean h2 title +
    optional subtitle, left-aligned, with an accent underline bar. Drops
    emoji to match the main-site's neutral typography."""
    lang = st.session_state.get("lang", "zh")
    title = title_en if lang == "en" else title_zh
    sub   = (sub_en if lang == "en" else sub_zh) or ""
    sub_html = f'<p class="dash-section-sub">{sub}</p>' if sub else ""
    st.markdown(
        f'''<div class="dash-section">
              <h2 class="dash-section-title">{title}</h2>
              {sub_html}
            </div>''',
        unsafe_allow_html=True,
    )

# ─── 路径常量 ─────────────────────────────────────────────────────────
_BASE = Path(__file__).parent
INDEX_FILE   = _BASE / "sessions_index.json"
DB_FILE      = str(_BASE / "label_platform.db")
AUTO_DB_FILE = str(_BASE / "auto_labeled.db")
UPLOAD_DIR   = _BASE / "uploaded_images"

# ─── 配色 ─────────────────────────────────────────────────────────────
BLUE = ["#1a56a8","#059669","#d97706","#7c3aed",
        "#0891b2","#dc2626","#0d9488","#f59e0b","#6366f1","#16a34a"]

# ─── 城市映射 ─────────────────────────────────────────────────────────
CITY_MAP = {
    "ShenZhen":T("深圳"),"Changchun":T("长春"),"Hongkong":T("香港"),
    "Beijing":T("北京"),"Shanghai":T("上海"),"Guangzhou":T("广州"),
    "Haerbin":T("哈尔滨"),"Hangzhou":T("杭州"),"Wuhan":T("武汉"),"Chengdu":T("成都"),
}

# ─── 标签体系 ─────────────────────────────────────────────────────────
TOP_LEVEL_CONFIG = {
    "区域":         ["封闭园区","交通管制区域","开放道路"],
    "城市道路":     ["快速路","主干路","次干路","支路","街巷"],
    "公路":         ["高速公路","一级公路","二级公路","三级公路","四级公路"],
    "乡村道路":     ["村道","其他乡村内部道路"],
    "其他道路":     ["厂矿","林区","港口","专用道路"],
    "停车区域":     ["室内停车场","室外停车场","路侧停车位"],
    "自动驾驶场景": ["封闭场景","半封闭场景","开放场景"],
}

SECONDARY_MULTI = {
    "1.3 道路几何":       ["曲率"],
    "1.4 包含车道特征":   ["车道类型","车道宽度"],
    "1.6 道路交叉":       ["交叉类型"],
    "2.1 交通控制":       ["地面标签"],
    "2.2 路侧与周边环境": ["设施"],
    "3.1 机动车":         ["类型"],
    "3.2 VRU":            ["类型"],
    "3.4 障碍物":         ["类型"],
}

STATIC_SCHEMA = {
    "一、道路静态环境": {
        "1.2 道路表面":     {"表面类型": ["沥青","混凝土","土路","碎石","冰雪路面","金属板"]},
        "1.3 道路几何": {
            "坡度": ["平路","上坡","下坡","起伏路"],
            "曲率": ["直线","弯道 (曲率<0.01)","弯道 (0.01<曲率<0.05)","弯道 (曲率>0.05)"],
            "横坡": ["正常排水坡度","反超高","无横坡"],
        },
        "1.4 包含车道特征": {
            "最宽车道数量": ["单车道","双车道","三车道","四车道及以上"],
            "车道类型":     ["普通车道","公交专用道","HOV车道","潮汐车道","应急车道",
                             "非机动车道","人行道","汇入匝道","汇出匝道"],
            "车道宽度":     ["标准","狭窄","超宽"],
        },
        "1.5 道路边缘":     {"边缘类型": ["路缘石","护栏 (金属)","护栏 (混凝土)","草地/泥土","无物理隔离"]},
        "1.6 道路交叉":     {
            "交叉类型": ["路段 (无交叉)","平面交叉 (十字)","平面交叉 (丁字)",
                         "平面交叉 (畸形)","大型环岛 (出入口数 > 4)","小型环岛","立体交叉"],
        },
    },
    "二、交通设施": {
        "2.1 交通控制": {
            "信号灯":   ["有","无"],
            "标志牌":   ["限速","禁止","指示","警告","施工","无"],
            "地面标签": ["实线","虚线","双黄线","导流线","斑马线","标线磨损"],
        },
        "2.2 路侧与周边环境": {
            "设施": ["无","路灯","电线杆","隔音墙","路边树木","路边停车位",
                     "地面停车场出入口","隧道出入口","居民楼","商场","学校",
                     "医院","公园","绿化带"],
        },
        "2.3 特殊设施": {"类型": ["收费站","检查站","施工区域围挡","减速带","无"]},
    },
}

DYNAMIC_SCHEMA = {
    "三、动态目标 (路面状况)": {
        "1.2 道路表面":  {"表面状态": ["干燥","潮湿","积水","积雪","结冰","泥泞"]},
        "3.1 机动车":    {"类型": ["轿车","客车/巴士","卡车/货车","特种车辆 (警)","特种车辆(消)","特种车辆(救)","工程车辆"]},
        "3.2 VRU":       {"类型": ["自行车","电动车","三轮车","行人","无"]},
        "3.3 动物":      {"类型": ["有","无"]},
        "3.4 障碍物":    {"类型": ["落石","遗洒物","倒伏树木","锥桶","无"]},
        "3.5 事故车辆":  {"类型": ["有","无"]},
    },
    "四、大气环境": {
        "4.1 天气":   {"类型": ["晴","多云","阴","雨 (小/中/大)","雪","雾","冰雹"]},
        "4.2 颗粒物": {"类型": ["无","雾霾","沙尘","烟尘"]},
        "4.3 光照":   {"强度": ["正常","强光/逆光","弱光/昏暗","黑暗"]},
    },
}

TAG_PATHS = [
    ("一、道路静态环境","1.2 道路表面","表面类型"),
    ("一、道路静态环境","1.3 道路几何","坡度"),
    ("一、道路静态环境","1.3 道路几何","曲率"),
    ("一、道路静态环境","1.4 包含车道特征","最宽车道数量"),
    ("一、道路静态环境","1.4 包含车道特征","车道类型"),
    ("一、道路静态环境","1.5 道路边缘","边缘类型"),
    ("一、道路静态环境","1.6 道路交叉","交叉类型"),
    ("二、交通设施","2.1 交通控制","信号灯"),
    ("二、交通设施","2.1 交通控制","地面标签"),
    ("二、交通设施","2.2 路侧与周边环境","设施"),
    ("二、交通设施","2.3 特殊设施","类型"),
    ("三、动态目标 (路面状况)","1.2 道路表面","表面状态"),
    ("三、动态目标 (路面状况)","3.1 机动车","类型"),
    ("三、动态目标 (路面状况)","3.2 VRU","类型"),
    ("三、动态目标 (路面状况)","3.3 动物","类型"),
    ("三、动态目标 (路面状况)","3.4 障碍物","类型"),
    ("三、动态目标 (路面状况)","3.5 事故车辆","类型"),
    ("四、大气环境","4.1 天气","类型"),
    ("四、大气环境","4.2 颗粒物","类型"),
    ("四、大气环境","4.3 光照","强度"),
]

# ─── 工具函数 ─────────────────────────────────────────────────────────
def extract_city(l1: str) -> str:
    m = re.search(r"\d{4}-([A-Za-z]+)-AerialVideo", l1)
    return CITY_MAP.get(m.group(1), m.group(1)) if m else l1

def parse_hour(ct: str) -> int:
    m = re.search(r"\s(\d{1,2}):", ct or "")
    return int(m.group(1)) if m else -1

def time_period(h: int) -> str:
    """修复原版：h<0 未知；0-6 / 19-23 均归夜间"""
    if h < 0:        return T("未知")
    if 7 <= h < 9:   return T("早高峰 (07-09)")
    if 17 <= h < 19: return T("晚高峰 (17-19)")
    if h >= 19 or h < 7: return T("夜间 (19-07)")
    return T("日常规")

def fmt_dur(minutes: float) -> str:
    return f"{minutes/60:.1f}h" if minutes >= 60 else f"{minutes:.0f}min"

def extract_tag(d: dict, sec: str, sub: str, attr: str):
    try:
        v = d[sec][sub][attr]
        return v if isinstance(v, list) else ([v] if v else None)
    except (KeyError, TypeError):
        return None

# ─── 数据库上下文管理器 ───────────────────────────────────────────────
@contextmanager
def db_conn():
    conn = sqlite3.connect(DB_FILE)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

# ─── 数据库初始化 ─────────────────────────────────────────────────────
def init_db():
    with db_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dataset (
                id                        INTEGER PRIMARY KEY AUTOINCREMENT,
                video_name                TEXT NOT NULL,
                folder_path               TEXT NOT NULL UNIQUE,
                location_name             TEXT,
                label_time                TIMESTAMP,
                collection_time           TEXT,
                top_road_category         TEXT,
                top_road_subcategory      TEXT,
                secondary_tags_json       TEXT,
                has_dynamic_override      INTEGER DEFAULT 0,
                has_atmosphere_override   INTEGER DEFAULT 0,
                has_road_surface_override INTEGER DEFAULT 0,
                duration                  REAL DEFAULT 0,
                quality_tags              TEXT DEFAULT '',
                comments                  TEXT DEFAULT '',
                image_path                TEXT DEFAULT ''
            )
        """)

# ─── 数据加载（带缓存） ───────────────────────────────────────────────
def _resolve_img(path: str) -> str:
    """将路径解析为基于脚本目录的绝对路径；绝对路径不存在时尝试取文件名在 _BASE 下查找"""
    if not path:
        return path
    p = Path(path)
    if not p.is_absolute():
        return str(_BASE / p)
    if p.exists():
        return str(p)
    # 绝对路径在当前环境不存在，依次在已知子目录下按文件名查找
    for subdir in ("frames", "photo", ""):
        candidate = _BASE / subdir / p.name if subdir else _BASE / p.name
        if candidate.exists():
            return str(candidate)
    return str(p)

@st.cache_data(ttl=600)
def load_location_groups() -> dict:
    if not INDEX_FILE.exists():
        return {}
    with open(INDEX_FILE, encoding="utf-8") as f:
        idx = json.load(f)
    groups: dict = {}
    for ds in idx.get("day_summaries", []):
        l2, l1 = ds["l2"], ds.get("l1", "")
        if l2 not in groups:
            groups[l2] = {"l2":l2,"l1":l1,"city":extract_city(l1),
                          "total_duration":0.0,"days":[],"all_reps":[]}
        groups[l2]["total_duration"] += ds.get("total_duration", 0)
        reps = [{**r, "image_path": _resolve_img(r.get("image_path",""))}
                for r in ds.get("representatives", [])]
        groups[l2]["days"].append({
            "date":         ds.get("date",""),
            "date_display": ds.get("date_display",""),
            "total_duration": ds.get("total_duration",0),
            "session_count":  ds.get("session_count",0),
            "reps": reps,
        })
        groups[l2]["all_reps"].extend(reps)
    for loc in groups.values():
        loc["days"].sort(key=lambda d: d["date"])
    return groups

@st.cache_data(ttl=30)
def load_labeled_fps() -> set:
    fps = set()
    try:
        with db_conn() as conn:
            fps |= {r[0] for r in conn.execute("SELECT folder_path FROM dataset").fetchall()}
    except Exception:
        pass
    try:
        conn2 = sqlite3.connect(AUTO_DB_FILE)
        fps |= {r[0] for r in conn2.execute("SELECT folder_path FROM dataset").fetchall()}
        conn2.close()
    except Exception:
        pass
    return fps

@st.cache_data(ttl=60)
def load_df() -> pd.DataFrame:
    SQL = ("SELECT id,video_name,folder_path,location_name,collection_time,"
           "top_road_category,top_road_subcategory,secondary_tags_json,duration,image_path "
           "FROM dataset")
    frames = []
    try:
        with db_conn() as conn:
            df_manual = pd.read_sql(SQL, conn)
            df_manual["source"] = T("手动标注")
            frames.append(df_manual)
    except Exception:
        pass
    try:
        conn2 = sqlite3.connect(AUTO_DB_FILE)
        df_auto = pd.read_sql(SQL, conn2)
        conn2.close()
        df_auto["source"] = T("VLM自动")
        frames.append(df_auto)
    except Exception:
        pass
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    # 若同一 folder_path 手动和 VLM 都有，优先保留手动标注
    df = df.sort_values("source").drop_duplicates(subset=["folder_path"], keep="first")
    # 用 sessions_index.json 补全缺失的 image_path
    try:
        if INDEX_FILE.exists():
            with open(INDEX_FILE, encoding="utf-8") as _f:
                _idx = json.load(_f)
            _fp2img = {s["folder_path"]: _resolve_img(s["image_path"])
                       for s in _idx.get("sessions", [])
                       if s.get("image_path")}
            _mask = df["image_path"].isna() | (df["image_path"] == "") | \
                    df["image_path"].apply(lambda p: bool(p) and not Path(str(p)).exists())
            df.loc[_mask, "image_path"] = df.loc[_mask, "folder_path"].map(_fp2img)
    except Exception:
        pass
    # 第二层兜底：对所有路径仍不存在的行（含 PHOTO 地点），用 _resolve_img 在 frames/ 下查找
    df["image_path"] = df["image_path"].apply(
        lambda p: _resolve_img(str(p)) if pd.notna(p) and p else p
    )
    def _city(p):
        if not p or "/" not in p: return T("手动录入")
        prefix = p.split("/")[0]
        if prefix in ("MANUAL", "PHOTO"):
            parts = p.split("/")
            return parts[1] if len(parts) > 1 else T("手动录入")
        return extract_city(prefix)
    df["city"] = df["folder_path"].apply(_city)
    df["year"] = df["folder_path"].apply(lambda p: p[:4] if p and p[:4].isdigit() else "")
    df["_tags"] = df["secondary_tags_json"].apply(
        lambda s: json.loads(s) if pd.notna(s) and s else {}
    )
    return df

# ─── DB 读写 ──────────────────────────────────────────────────────────
def get_location_static(l2: str) -> dict:
    try:
        with db_conn() as conn:
            row = conn.execute(
                "SELECT top_road_category,top_road_subcategory,secondary_tags_json "
                "FROM dataset WHERE location_name=? LIMIT 1", (l2,)
            ).fetchone()
        if row:
            tags = json.loads(row[2]) if row[2] else {}
            return {"top_cat": row[0] or "", "top_sub": row[1] or "",
                    "sections": {k: tags[k] for k in STATIC_SCHEMA if k in tags}}
    except Exception:
        pass
    return {}

def get_rep_dynamic(folder_path: str) -> dict:
    try:
        with db_conn() as conn:
            row = conn.execute(
                "SELECT secondary_tags_json FROM dataset WHERE folder_path=?", (folder_path,)
            ).fetchone()
        if row and row[0]:
            tags = json.loads(row[0])
            return {k: tags[k] for k in DYNAMIC_SCHEMA if k in tags}
    except Exception:
        pass
    return {}

_UPSERT_SQL = """
    INSERT OR REPLACE INTO dataset
    (video_name,folder_path,location_name,label_time,collection_time,
     top_road_category,top_road_subcategory,secondary_tags_json,
     has_dynamic_override,has_atmosphere_override,has_road_surface_override,
     duration,quality_tags,comments,image_path)
    VALUES (?,?,?,?,?,?,?,?,0,0,0,?,'',?,?)
"""

def _upsert_rep(conn, rep: dict, l2: str, top_cat: str, top_sub: str,
                merged_tags: dict, comments: str = ""):
    conn.execute(_UPSERT_SQL, (
        Path(rep["image_path"]).name if rep.get("image_path") else "",
        rep["folder_path"], l2,
        datetime.now().isoformat(),
        rep.get("collection_time",""),
        top_cat, top_sub,
        json.dumps(merged_tags, ensure_ascii=False),
        rep.get("duration", 0),
        comments,
        rep.get("image_path",""),
    ))

def save_static_to_location(l2: str, loc: dict, top_cat: str, top_sub: str, static_sections: dict):
    with db_conn() as conn:
        for rep in loc["all_reps"]:
            existing_dyn = get_rep_dynamic(rep["folder_path"])
            _upsert_rep(conn, rep, l2, top_cat, top_sub, {**static_sections, **existing_dyn})

def save_dynamic_for_rep(rep: dict, l2: str, loc_static: dict,
                         dynamic_sections: dict, comments: str = ""):
    merged = {**loc_static.get("sections", {}), **dynamic_sections}
    with db_conn() as conn:
        _upsert_rep(conn, rep, l2,
                    loc_static.get("top_cat",""), loc_static.get("top_sub",""),
                    merged, comments)

def save_manual_location(folder_path, video_name, location_name, collection_time,
                         top_cat, top_sub, all_tags, duration, image_path, comments):
    with db_conn() as conn:
        conn.execute(_UPSERT_SQL, (
            video_name, folder_path, location_name,
            datetime.now().isoformat(), collection_time,
            top_cat, top_sub,
            json.dumps(all_tags, ensure_ascii=False),
            duration, comments, image_path,
        ))

# ─── 表单渲染 ─────────────────────────────────────────────────────────
def _field(sub_key: str, attr: str, options: list, existing_sec: dict, form_key: str):
    """渲染单个表单字段（单选/多选）—— 显示时翻译标签与选项，存储仍用中文"""
    is_multi = attr in SECONDARY_MULTI.get(sub_key, [])
    ex_val   = existing_sec.get(sub_key, {}).get(attr)
    label    = f"{T(sub_key)} · {T(attr)}"
    # format_func maps option → display text so users see EN while storage
    # still uses the original Chinese tokens
    fmt = T
    if is_multi:
        if isinstance(ex_val, str): ex_val = [ex_val] if ex_val else []
        elif not isinstance(ex_val, list): ex_val = []
        return st.multiselect(label, options, default=[v for v in ex_val if v in options],
                              key=form_key, format_func=fmt)
    else:
        if isinstance(ex_val, list): ex_val = ex_val[0] if ex_val else ""
        return st.selectbox(label, options,
                            index=options.index(ex_val) if ex_val in options else 0,
                            key=form_key, format_func=fmt)

def render_schema_section(schema: dict, existing: dict, key_prefix: str) -> dict:
    """3 列布局渲染整个 schema，返回收集到的标签 dict（key 仍为中文）"""
    result = {}
    for sec, sub_dict in schema.items():
        st.markdown(f"<div class='sec-bar'>{T(sec)}</div>", unsafe_allow_html=True)
        result[sec] = {}
        cols = st.columns(3)
        ci = 0
        for sub_key, attrs in sub_dict.items():
            result[sec][sub_key] = {}
            for attr, options in attrs.items():
                fk = f"{key_prefix}__{sec}__{sub_key}__{attr}"
                with cols[ci % 3]:
                    result[sec][sub_key][attr] = _field(
                        sub_key, attr, options, existing.get(sec, {}), fk
                    )
                ci += 1
    return result

# ─── Plotly 公共布局参数 ──────────────────────────────────────────────
def _cl() -> dict:
    return dict(
        plot_bgcolor="rgba(255,255,255,0)",
        paper_bgcolor="rgba(255,255,255,0.55)",
        title_font=dict(size=14, color="#0d2d5e", family="'Helvetica Neue',Arial,sans-serif"),
        title_x=0.02,
        font=dict(family="'Helvetica Neue','PingFang SC',Arial,sans-serif", size=12, color="#2d3748"),
        xaxis=dict(showgrid=False, showline=False,
                   tickfont=dict(size=11, color="#4a6fa5"), tickcolor="rgba(0,0,0,0)"),
        yaxis=dict(gridcolor="rgba(160,174,192,0.22)", gridwidth=1,
                   showline=False, zeroline=False, tickfont=dict(size=11, color="#4a6fa5")),
        legend=dict(bgcolor="rgba(255,255,255,0.65)",
                    bordercolor="rgba(99,179,237,0.25)", borderwidth=1,
                    font=dict(size=11, color="#2d3748")),
        hoverlabel=dict(bgcolor="rgba(255,255,255,0.92)", font_size=12,
                        font_color="#0d2d5e", bordercolor="rgba(99,179,237,0.4)"),
        coloraxis_showscale=False,
    )

# ─── 页面 1：地点标注 ─────────────────────────────────────────────────
def page_label():
    loc_groups = load_location_groups()
    if not loc_groups:
        st.error(f"未找到索引文件：{INDEX_FILE}"); return

    labeled_fps = load_labeled_fps()

    # ── 顶部控制行：城市过滤 + 进度 ──
    _fc1, _fc2 = st.columns([2, 2])
    with _fc1:
        all_cities = sorted({v["city"] for v in loc_groups.values()})
        sel_city   = st.multiselect(T("🌆 城市过滤"), all_cities, default=all_cities, key="lbl_city")
    filtered     = {_k: _v for _k, _v in sorted(loc_groups.items()) if _v["city"] in sel_city}
    total_reps   = sum(len(_v["all_reps"]) for _v in filtered.values())
    labeled_reps = sum(1 for _v in filtered.values()
                       for _r in _v["all_reps"] if _r["folder_path"] in labeled_fps)
    with _fc2:
        st.markdown(f"**📊 进度：{labeled_reps} / {total_reps} 张**")
        st.progress(labeled_reps / total_reps if total_reps else 0)

    st.markdown("<hr style='margin:6px 0 10px'>", unsafe_allow_html=True)

    # ── 双栏：地点列表（左）| 标注表单（右） ──
    _col_l, _col_r = st.columns([1, 3])

    with _col_l:
        st.markdown("<div class='loc-list-header'>🗺️ 地点列表</div>", unsafe_allow_html=True)
        for _k, _v in filtered.items():
            _nr  = len(_v["all_reps"])
            _nd  = sum(1 for _r in _v["all_reps"] if _r["folder_path"] in labeled_fps)
            _dot = "✅" if _nd == _nr else ("🔶" if _nd > 0 else "⬜")
            if st.button(f"{_dot} {_v['city']} · {_k}  ({_nd}/{_nr})",
                         key=f"loc_{_k}", use_container_width=True):
                st.session_state.sel_loc = _k

    with _col_r:
        if "sel_loc" not in st.session_state:
            st.info(T("← 从左侧列表中选择一个地点开始标注"))
            return

        l2  = st.session_state.sel_loc
        loc = loc_groups.get(l2)
        if not loc:
            st.warning(T("地点数据不存在"))
            return

        n_rep  = len(loc["all_reps"])
        n_done = sum(1 for r in loc["all_reps"] if r["folder_path"] in labeled_fps)
        st.markdown(
            f"<div class='loc-header'>"
            f"<span class='loc-city'>{loc['city']}</span>"
            f"<span class='loc-name'>{l2}</span>"
            f"<span class='loc-stat'>📅 {len(loc['days'])} 天 &nbsp;|&nbsp; "
            f"🖼 {n_rep} 张代表图 &nbsp;|&nbsp; "
            f"⏱ {fmt_dur(loc['total_duration'])} 总时长 &nbsp;|&nbsp; "
            f"{'✅' if n_done==n_rep else '🔶'} {n_done}/{n_rep} 已标注</span>"
            f"</div>", unsafe_allow_html=True
        )

        tab_static, tab_dynamic = st.tabs([T("📍 静态标签（整个地点）"), T("🎬 动态标签（逐图）")])

        # ── Tab1 静态 ──
        with tab_static:
            st.markdown("<div class='tip'>道路几何、车道、设施等物理特征不随时间变化，"
                        "填写一次后保存到本地点所有代表图。</div>", unsafe_allow_html=True)
            ex = get_location_static(l2)
            cats = list(TOP_LEVEL_CONFIG.keys())
            with st.form(key=f"static_{l2}"):
                c1, c2 = st.columns(2)
                with c1:
                    top_cat = st.selectbox(T("道路主类"), cats,
                        index=cats.index(ex.get("top_cat","")) if ex.get("top_cat","") in cats else 0,
                        key=f"scat_{l2}", format_func=T)
                with c2:
                    subs    = TOP_LEVEL_CONFIG[top_cat]
                    top_sub = st.selectbox(T("道路子类"), subs,
                        index=subs.index(ex.get("top_sub","")) if ex.get("top_sub","") in subs else 0,
                        key=f"ssub_{l2}", format_func=T)
                static_tags = render_schema_section(STATIC_SCHEMA, ex.get("sections",{}), f"st_{l2}")
                submitted   = st.form_submit_button(
                    f"💾 保存静态标签 → 应用到本地点全部 {n_rep} 张代表图",
                    type="primary", use_container_width=True)
            if submitted:
                save_static_to_location(l2, loc, top_cat, top_sub, static_tags)
                load_labeled_fps.clear(); load_df.clear()
                st.success(f"✅ 静态标签已保存并应用到 {n_rep} 张图！"); st.rerun()

        # ── Tab2 动态 ──
        with tab_dynamic:
            st.markdown("<div class='tip'>天气、光照、车辆等随时间变化，每张图独立标注。"
                        "静态标签会自动沿用（请先完成静态标签）。</div>", unsafe_allow_html=True)
            loc_static = get_location_static(l2)

            for day in loc["days"]:
                dreps      = day["reps"]
                n_day_done = sum(1 for r in dreps if r["folder_path"] in labeled_fps)
                badge = "✅" if n_day_done == len(dreps) else ("🔶" if n_day_done > 0 else "⬜")

                with st.expander(
                    f"{badge}  {day['date_display']}  ·  {fmt_dur(day['total_duration'])} 总时长"
                    f"  ·  {len(dreps)} 张  ({n_day_done}/{len(dreps)} 已标)",
                    expanded=(n_day_done < len(dreps))
                ):
                    img_cols = st.columns(len(dreps))
                    for ci, rep in enumerate(dreps):
                        with img_cols[ci]:
                            p = Path(rep.get("image_path",""))
                            st.image(str(p), use_container_width=True) if p.exists() else st.markdown(T("🖼 图片不存在"))
                            done_mark = "✅" if rep["folder_path"] in labeled_fps else "⬜"
                            st.markdown(
                                f"<div style='font-size:12px;color:#1565C0;text-align:center'>"
                                f"{done_mark} {rep.get('collection_time','')[-5:]}<br>"
                                f"⏱ {fmt_dur(rep.get('duration',0))}</div>",
                                unsafe_allow_html=True
                            )

                    st.markdown("---")
                    for ri, rep in enumerate(dreps):
                        fp      = rep["folder_path"]
                        is_done = fp in labeled_fps
                        with st.expander(
                            f"{'✅' if is_done else '⬜'} 图{ri+1} · "
                            f"{rep.get('collection_time','')} · {fmt_dur(rep.get('duration',0))}",
                            expanded=not is_done
                        ):
                            existing_dyn = get_rep_dynamic(fp)
                            with st.form(key=f"dyn_{fp.replace('/','_')}"):
                                dyn_tags = render_schema_section(
                                    DYNAMIC_SCHEMA, existing_dyn, f"d_{fp.replace('/','_')}")
                                comments = st.text_input(T("备注"), value="",
                                                         key=f"cmt_{fp.replace('/','_')}")
                                save_btn = st.form_submit_button(T("💾 保存"), type="primary")
                            if save_btn:
                                save_dynamic_for_rep(rep, l2, loc_static, dyn_tags, comments)
                                load_labeled_fps.clear(); load_df.clear()
                                st.success(T("✅ 已保存")); st.rerun()

# ─── 地点详情面板 ────────────────────────────────────────────────────
def _render_loc_detail(df_f: pd.DataFrame, loc_row):
    tags = loc_row.get("_tags") or {}

    def _t(sec, sub, attr):
        v = extract_tag(tags, sec, sub, attr)
        return "、".join(str(x) for x in v) if v else "—"

    st.markdown("<hr style='margin:6px 0 10px'>", unsafe_allow_html=True)

    loc_rows_all = df_f[df_f["location_name"] == loc_row["location_name"]]
    total_dur    = loc_rows_all["duration"].sum()
    _dates      = sorted({str(t)[:10] for t in loc_rows_all["collection_time"].dropna() if t})
    if len(_dates) >= 2:
        collect_str = f"{_dates[0]} ~ {_dates[-1]}"
    elif len(_dates) == 1:
        collect_str = _dates[0]
    else:
        collect_str = "—"

    # ── 批次分组（同一地点多次采集时自动展示） ──
    def _batch_id(fp):
        parts = (fp or "").split("/")
        return parts[1] if len(parts) > 1 else (parts[0] if parts else fp)

    batch_map = {}   # batch_id -> {image, duration, count}
    for _, r in loc_rows_all.iterrows():
        bid = _batch_id(r.get("folder_path", ""))
        if bid not in batch_map:
            img = str(r.get("image_path", "") or "")
            batch_map[bid] = {"image": img if Path(img).exists() else "", "duration": 0, "count": 0}
        batch_map[bid]["duration"] += r.get("duration", 0) or 0
        batch_map[bid]["count"]    += 1

    is_merged = len(batch_map) > 1

    # ── 图片 + 摘要/元数据 ──
    if is_merged:
        # 合并地点：每批次显示一张图
        st.markdown(
            f"<div style='font-size:0.78rem;color:#5a7fae;margin-bottom:6px'>"
            f"🗂️ 该地点包含 <b>{len(batch_map)}</b> 个采集批次</div>",
            unsafe_allow_html=True
        )
        batch_cols = st.columns(len(batch_map))
        for col, (bid, binfo) in zip(batch_cols, batch_map.items()):
            with col:
                if binfo["image"]:
                    st.image(binfo["image"], use_container_width=True)
                else:
                    st.markdown(
                        "<div style='height:90px;background:linear-gradient(135deg,#1a3a6e,#2d6eaa);"
                        "border-radius:8px;display:flex;align-items:center;justify-content:center;"
                        "color:#aac8ff;font-size:1.5rem'>🗺️</div>", unsafe_allow_html=True
                    )
                st.markdown(
                    f"<div style='text-align:center;font-size:0.74rem;color:#0d2d5e;font-weight:700'>{bid}</div>"
                    f"<div style='text-align:center;font-size:0.72rem;color:#6b7280'>"
                    f"⏱ {fmt_dur(binfo['duration'])} &nbsp;·&nbsp; {binfo['count']} 段</div>",
                    unsafe_allow_html=True
                )
        c_info = st.container()
    else:
        img_path = str(loc_row.get("image_path", "") or "")
        has_img  = bool(img_path and Path(img_path).exists())
        if has_img:
            c_img, c_info = st.columns([1, 2])
            with c_img:
                st.image(img_path, use_container_width=True)
        else:
            c_info = st.container()

    summary_items = [
        (T("道路主类"),  loc_row.get("top_road_category")  or "—"),
        (T("道路子类"),  loc_row.get("top_road_subcategory") or "—"),
        (T("交叉类型"),  _t(T("一、道路静态环境"), T("1.6 道路交叉"),     T("交叉类型"))),
        (T("路面状态"),  _t(T("一、道路静态环境"), T("1.2 道路表面"),     T("表面状态"))),
        (T("天气"),      _t(T("四、大气环境"),      T("4.1 天气"),        T("类型"))),
        (T("机动车"),    _t(T("三、动态目标 (路面状况)"), T("3.1 机动车"), T("类型"))),
        ("VRU",       _t(T("三、动态目标 (路面状况)"), "3.2 VRU",    T("类型"))),
    ]
    chips = " &nbsp;".join(
        f"<span style='background:#e8f0fe;color:#1a56a8;border-radius:6px;"
        f"padding:2px 8px;font-size:0.75rem;font-weight:600'>{k}: {v}</span>"
        for k, v in summary_items
    )
    with c_info:
        st.markdown(chips, unsafe_allow_html=True)
        st.markdown(
            f"<div style='display:flex;gap:16px;flex-wrap:wrap;margin-top:8px;"
            f"font-size:0.8rem;color:#4a6fa5'>"
            f"<span>📍 {loc_row['location_name']}</span>"
            f"<span>🏙️ {loc_row['city']}</span>"
            f"<span>⏱ {fmt_dur(total_dur)}</span>"
            f"<span>📅 {collect_str}</span>"
            f"</div>", unsafe_allow_html=True
        )

    # ── 完整 ODD 标签（2列布局） ──
    st.markdown("<div style='margin-top:10px'></div>", unsafe_allow_html=True)
    odd_sections = [
        (T("🛣️ 一、道路静态环境"), [
            (T("1.2 道路表面"),   tags.get(T("一、道路静态环境"), {}).get(T("1.2 道路表面"),   {})),
            (T("1.3 道路几何"),   tags.get(T("一、道路静态环境"), {}).get(T("1.3 道路几何"),   {})),
            (T("1.4 包含车道特征"), tags.get(T("一、道路静态环境"), {}).get(T("1.4 包含车道特征"), {})),
            (T("1.5 道路边缘"),   tags.get(T("一、道路静态环境"), {}).get(T("1.5 道路边缘"),   {})),
            (T("1.6 道路交叉"),   tags.get(T("一、道路静态环境"), {}).get(T("1.6 道路交叉"),   {})),
        ]),
        (T("🚦 二、交通设施"), [
            (T("2.1 交通控制"),       tags.get(T("二、交通设施"), {}).get(T("2.1 交通控制"),       {})),
            (T("2.2 路侧与周边环境"), tags.get(T("二、交通设施"), {}).get(T("2.2 路侧与周边环境"), {})),
            (T("2.3 特殊设施"),       tags.get(T("二、交通设施"), {}).get(T("2.3 特殊设施"),       {})),
        ]),
        (T("🚗 三、动态目标"), [
            (T("3.1 机动车"),   tags.get(T("三、动态目标 (路面状况)"), {}).get(T("3.1 机动车"),   {})),
            ("3.2 VRU",      tags.get(T("三、动态目标 (路面状况)"), {}).get("3.2 VRU",      {})),
            (T("3.3 动物"),     tags.get(T("三、动态目标 (路面状况)"), {}).get(T("3.3 动物"),     {})),
            (T("3.4 障碍物"),   tags.get(T("三、动态目标 (路面状况)"), {}).get(T("3.4 障碍物"),   {})),
            (T("3.5 事故车辆"), tags.get(T("三、动态目标 (路面状况)"), {}).get(T("3.5 事故车辆"), {})),
        ]),
        (T("🌤️ 四、大气环境"), [
            (T("4.1 天气"),   tags.get(T("四、大气环境"), {}).get(T("4.1 天气"),   {})),
            (T("4.2 颗粒物"), tags.get(T("四、大气环境"), {}).get(T("4.2 颗粒物"), {})),
            (T("4.3 光照"),   tags.get(T("四、大气环境"), {}).get(T("4.3 光照"),   {})),
        ]),
    ]
    oc1, oc2 = st.columns(2)
    for si, (sec_title, subsections) in enumerate(odd_sections):
        with (oc1 if si % 2 == 0 else oc2):
            st.markdown(f"<div class='sec-bar'>{sec_title}</div>", unsafe_allow_html=True)
            for sub_name, sub_data in subsections:
                if not sub_data:
                    continue
                lines = []
                for attr, val in sub_data.items():
                    val_str = "、".join(str(v) for v in val) if isinstance(val, list) else (str(val) if val else "—")
                    lines.append(f"<b>{attr}</b>: {val_str}")
                st.markdown(
                    f"<div style='font-size:0.79rem;padding:4px 8px;margin-bottom:4px;"
                    f"background:rgba(26,86,168,0.04);border-radius:6px;"
                    f"border-left:3px solid #3b7dd8'>"
                    f"<span style='color:#5a7fae;font-size:0.71rem'>{sub_name}</span><br>"
                    + " &nbsp;·&nbsp; ".join(lines)
                    + "</div>", unsafe_allow_html=True
                )
    # ── 采集时段分布图 ──────────────────────────────────────────────────
    if "_period" in loc_rows_all.columns:
        import plotly.express as px
        _PO = [T("早高峰 (07-09)"), T("日常规"), T("晚高峰 (17-19)"), T("夜间 (19-07)"), T("未知")]
        pd_data = (loc_rows_all.groupby("_period")["duration"]
                   .sum().reset_index(name="时长_min"))
        pd_data["时长(h)"] = (pd_data["时长_min"] / 60).round(2)
        pd_data["_o"] = pd_data["_period"].apply(lambda x: _PO.index(x) if x in _PO else 99)
        pd_data = pd_data.sort_values("_o").drop(columns=["_o", "时长_min"])
        if not pd_data.empty:
            st.markdown(
                "<div style='margin:10px 0 4px'>"
                "<b style='font-size:0.88rem;color:#0d2d5e'>🕐 采集时段分布</b>"
                "</div>", unsafe_allow_html=True
            )
            fig = px.bar(
                pd_data, x="_period", y="时长(h)", text="时长(h)", color="_period",
                color_discrete_sequence=BLUE, labels={"_period": T("时段"), "时长(h)": T("时长 (h)")},
            )
            fig.update_traces(
                textposition="outside",
                textfont=dict(size=11, color="#0d2d5e"),
                marker_line_width=0, opacity=0.88,
            )
            fig.update_layout(
                plot_bgcolor="rgba(255,255,255,0)",
                paper_bgcolor="rgba(255,255,255,0.55)",
                showlegend=False, height=240,
                margin=dict(t=10, l=0, r=0, b=40),
                xaxis=dict(showgrid=False, tickfont=dict(size=11, color="#4a6fa5"),
                           tickcolor="rgba(0,0,0,0)", title=None),
                yaxis=dict(gridcolor="rgba(160,174,192,0.22)", gridwidth=1,
                           showline=False, zeroline=False,
                           tickfont=dict(size=11, color="#4a6fa5")),
            )
            st.plotly_chart(fig, use_container_width=True)

    st.markdown("<hr style='margin:10px 0'>", unsafe_allow_html=True)


# ─── 地点卡片墙 ──────────────────────────────────────────────────────
def _render_location_wall(df_f: pd.DataFrame):
    # 每个地点取一行代表（优先有图片的）
    loc_df = (df_f.assign(_hi=df_f["image_path"].notna() & (df_f["image_path"] != ""))
              .sort_values(["location_name", "_hi"], ascending=[True, False])
              .drop_duplicates("location_name", keep="first")
              .reset_index(drop=True))
    dur_sum = df_f.groupby("location_name")["duration"].sum()
    loc_df["_total_dur"] = loc_df["location_name"].map(dur_sum).fillna(0)

    if "loc_open" not in st.session_state:
        st.session_state["loc_open"] = None

    for row_start in range(0, len(loc_df), 4):
        chunk = loc_df.iloc[row_start:row_start + 4]
        cols  = st.columns(4)
        opened_row = None

        for col, (_, row) in zip(cols, chunk.iterrows()):
            fp      = row["folder_path"]
            is_open = st.session_state["loc_open"] == fp
            tags    = row.get("_tags") or {}
            cross   = extract_tag(tags, T("一、道路静态环境"), T("1.6 道路交叉"), T("交叉类型"))
            cross_s = "、".join(cross) if cross else ""
            sub_s   = row.get("top_road_subcategory") or ""
            cat_s   = row.get("top_road_category")    or ""

            with col:
                img_path = str(row.get("image_path", "") or "")
                if img_path and Path(img_path).exists():
                    st.image(img_path, use_container_width=True)
                else:
                    st.markdown(
                        "<div style='height:110px;background:linear-gradient(135deg,#1a3a6e,#2d6eaa);"
                        "border-radius:8px;display:flex;align-items:center;justify-content:center;"
                        "color:#aac8ff;font-size:2rem'>🗺️</div>",
                        unsafe_allow_html=True
                    )
                st.markdown(
                    f"<div style='padding:4px 2px 0'>"
                    f"<div style='font-weight:700;font-size:0.88rem;color:#0d2d5e'>{row['location_name']}</div>"
                    f"<div style='font-size:0.76rem;color:#5a7fae'>{row['city']}</div>"
                    f"<div style='font-size:0.73rem;color:#6b7280;margin-top:2px'>"
                    f"⏱ {fmt_dur(row['_total_dur'])}"
                    f"{' &nbsp;|&nbsp; ' + cat_s if cat_s else ''}"
                    f"{' · ' + sub_s if sub_s else ''}</div>"
                    + (f"<div style='font-size:0.70rem;color:#9ca3af'>{cross_s}</div>" if cross_s else "")
                    + "</div>", unsafe_allow_html=True
                )
                if st.button(T("▾ 收起") if is_open else T("▸ 查看标签"),
                             key=f"loc_btn_{fp}", use_container_width=True):
                    st.session_state["loc_open"] = None if is_open else fp
                    st.rerun()
                if is_open:
                    opened_row = row

        if opened_row is not None:
            _render_loc_detail(df_f, opened_row)


# ─── 页面 2：统计看板 ─────────────────────────────────────────────────
def page_dashboard():
    import plotly.express as px

    df = load_df()
    if df.empty:
        st.info(T("数据库暂无数据，请先完成标注")); return

    with st.expander(T("🔍 筛选条件"), expanded=True):
        _dc1, _dc2, _dc3 = st.columns([1, 2, 2])
        with _dc1:
            if st.button(T("🔄 刷新数据"), key="dash_refresh"):
                load_df.clear(); st.rerun()
        with _dc2:
            sel_c = st.multiselect(T("城市"), sorted(df["city"].dropna().unique()),
                                   default=sorted(df["city"].dropna().unique()),
                                   key="dc", format_func=T)
        with _dc3:
            sel_r = st.multiselect(T("道路主类"), sorted(df["top_road_category"].dropna().unique()),
                                   default=sorted(df["top_road_category"].dropna().unique()), key="dr")

    # ── 高级筛选 ──
    def _tag_vals(col_df, sec, sub, attr):
        vals = set()
        for d in col_df["_tags"]:
            v = extract_tag(d, sec, sub, attr)
            if v:
                vals.update(v)
        return sorted(vals)

    PERIOD_ORDER = [T("早高峰 (07-09)"), T("日常规"), T("晚高峰 (17-19)"), T("夜间 (19-07)"), T("未知")]

    with st.expander(T("⚙️ 高级筛选"), expanded=False):
        # ── 一、道路静态环境 ──────────────────────────────────────
        st.markdown(T("**一、道路静态环境**"))
        _r1c1, _r1c2, _r1c3, _r1c4 = st.columns(4)
        _r2c1, _r2c2, _r2c3, _r2c4 = st.columns(4)

        sub_opts    = sorted(df["top_road_subcategory"].dropna().unique())
        cross_opts  = _tag_vals(df, T("一、道路静态环境"), T("1.6 道路交叉"),       T("交叉类型"))
        lane_n_opts = _tag_vals(df, T("一、道路静态环境"), T("1.4 包含车道特征"),   T("最宽车道数量"))
        lane_t_opts = _tag_vals(df, T("一、道路静态环境"), T("1.4 包含车道特征"),   T("车道类型"))
        lane_w_opts = _tag_vals(df, T("一、道路静态环境"), T("1.4 包含车道特征"),   T("车道宽度"))
        surf_opts   = _tag_vals(df, T("一、道路静态环境"), T("1.2 道路表面"),       T("表面类型"))
        surf_s_opts = _tag_vals(df, T("一、道路静态环境"), T("1.2 道路表面"),       T("表面状态"))
        slope_opts  = _tag_vals(df, T("一、道路静态环境"), T("1.3 道路几何"),       T("坡度"))
        curv_opts   = _tag_vals(df, T("一、道路静态环境"), T("1.3 道路几何"),       T("曲率"))
        bank_opts   = _tag_vals(df, T("一、道路静态环境"), T("1.3 道路几何"),       T("横坡"))
        edge_opts   = _tag_vals(df, T("一、道路静态环境"), T("1.5 道路边缘"),       T("边缘类型"))

        with _r1c1: sel_sub      = st.multiselect(T("道路子类"),   sub_opts,    default=sub_opts,    key="adv_sub")
        with _r1c2: sel_cross    = st.multiselect(T("交叉类型"),   cross_opts,  default=cross_opts,  key="adv_cross")
        with _r1c3: sel_lane_n   = st.multiselect(T("车道数量"),   lane_n_opts, default=lane_n_opts, key="adv_lane_n")
        with _r1c4: sel_lane_t   = st.multiselect(T("车道类型"),   lane_t_opts, default=lane_t_opts, key="adv_lane_t")
        with _r2c1: sel_lane_w   = st.multiselect(T("车道宽度"),   lane_w_opts, default=lane_w_opts, key="adv_lane_w")
        with _r2c2: sel_surf     = st.multiselect(T("路面类型"),   surf_opts,   default=surf_opts,   key="adv_surf")
        with _r2c3: sel_surf_s   = st.multiselect(T("路面状态"),   surf_s_opts, default=surf_s_opts, key="adv_surf_s")
        with _r2c4: sel_edge     = st.multiselect(T("道路边缘"),   edge_opts,   default=edge_opts,   key="adv_edge")

        _r3c1, _r3c2, _r3c3 = st.columns(3)
        with _r3c1: sel_slope    = st.multiselect(T("坡度"),       slope_opts,  default=slope_opts,  key="adv_slope")
        with _r3c2: sel_curv     = st.multiselect(T("曲率"),       curv_opts,   default=curv_opts,   key="adv_curv")
        with _r3c3: sel_bank     = st.multiselect(T("横坡"),       bank_opts,   default=bank_opts,   key="adv_bank")

        st.divider()
        # ── 二、交通设施 ──────────────────────────────────────────
        st.markdown(T("**二、交通设施**"))
        _r4c1, _r4c2, _r4c3, _r4c4 = st.columns(4)

        sig_opts    = _tag_vals(df, T("二、交通设施"), T("2.1 交通控制"),       T("信号灯"))
        sign_opts   = _tag_vals(df, T("二、交通设施"), T("2.1 交通控制"),       T("标志牌"))
        mark_opts   = _tag_vals(df, T("二、交通设施"), T("2.1 交通控制"),       T("地面标签"))
        fac_opts    = _tag_vals(df, T("二、交通设施"), T("2.2 路侧与周边环境"), T("设施"))
        spec_opts   = _tag_vals(df, T("二、交通设施"), T("2.3 特殊设施"),       T("类型"))

        with _r4c1: sel_sig      = st.multiselect(T("信号灯"),     sig_opts,   default=sig_opts,   key="adv_sig")
        with _r4c2: sel_sign     = st.multiselect(T("标志牌"),     sign_opts,  default=sign_opts,  key="adv_sign")
        with _r4c3: sel_mark     = st.multiselect(T("地面标线"),   mark_opts,  default=mark_opts,  key="adv_mark")
        with _r4c4: sel_spec     = st.multiselect(T("特殊设施"),   spec_opts,  default=spec_opts,  key="adv_spec")
        sel_fac = st.multiselect(T("路侧设施"), fac_opts, default=fac_opts, key="adv_fac")

        st.divider()
        # ── 三、动态目标 ──────────────────────────────────────────
        st.markdown(T("**三、动态目标**"))
        _r5c1, _r5c2, _r5c3 = st.columns(3)

        mv_opts     = _tag_vals(df, T("三、动态目标 (路面状况)"), T("3.1 机动车"),   T("类型"))
        vru_opts    = _tag_vals(df, T("三、动态目标 (路面状况)"), "3.2 VRU",      T("类型"))
        obs_opts    = _tag_vals(df, T("三、动态目标 (路面状况)"), T("3.4 障碍物"),   T("类型"))

        with _r5c1: sel_mv       = st.multiselect(T("机动车类型"), mv_opts,    default=mv_opts,    key="adv_mv")
        with _r5c2: sel_vru      = st.multiselect(T("VRU 类型"),   vru_opts,   default=vru_opts,   key="adv_vru")
        with _r5c3: sel_obs      = st.multiselect(T("障碍物"),     obs_opts,   default=obs_opts,   key="adv_obs")

        st.divider()
        # ── 四、大气环境 + 时段 ───────────────────────────────────
        st.markdown(T("**四、大气环境 & 采集时段**"))
        _r6c1, _r6c2, _r6c3, _r6c4, _r6c5 = st.columns(5)

        wth_opts    = _tag_vals(df, T("四、大气环境"), T("4.1 天气"),  T("类型"))
        light_opts  = _tag_vals(df, T("四、大气环境"), T("4.3 光照"),  T("来源"))
        lumi_opts   = _tag_vals(df, T("四、大气环境"), T("4.3 光照"),  T("强度"))
        temp_opts   = _tag_vals(df, T("四、大气环境"), T("4.4 气温"),  T("估算"))

        with _r6c1: sel_wth      = st.multiselect(T("天气"),       wth_opts,   default=wth_opts,   key="adv_wth")
        with _r6c2: sel_light    = st.multiselect(T("光照来源"),   light_opts, default=light_opts, key="adv_light")
        with _r6c3: sel_lumi     = st.multiselect(T("光照强度"),   lumi_opts,  default=lumi_opts,  key="adv_lumi")
        with _r6c4: sel_temp     = st.multiselect(T("气温"),       temp_opts,  default=temp_opts,  key="adv_temp")
        with _r6c5: sel_period   = st.multiselect(T("采集时段"),   PERIOD_ORDER, default=PERIOD_ORDER, key="adv_period")

    def _tag_match(tags, sec, sub, attr, allowed):
        v = extract_tag(tags, sec, sub, attr)
        if not v:
            return True   # 无标签的不过滤掉
        return bool(set(v) & set(allowed))

    df["_period"] = df["collection_time"].apply(lambda ct: time_period(parse_hour(ct)))

    df_f = df[
        df["city"].isin(sel_c) &
        df["top_road_category"].isin(sel_r) &
        (df["top_road_subcategory"].isna() | df["top_road_subcategory"].isin(sel_sub)) &
        df["_period"].isin(sel_period) &
        df["_tags"].apply(lambda d: _tag_match(d, T("一、道路静态环境"), T("1.6 道路交叉"),       T("交叉类型"),   sel_cross))  &
        df["_tags"].apply(lambda d: _tag_match(d, T("一、道路静态环境"), T("1.4 包含车道特征"),   T("最宽车道数量"), sel_lane_n)) &
        df["_tags"].apply(lambda d: _tag_match(d, T("一、道路静态环境"), T("1.4 包含车道特征"),   T("车道类型"),   sel_lane_t)) &
        df["_tags"].apply(lambda d: _tag_match(d, T("一、道路静态环境"), T("1.4 包含车道特征"),   T("车道宽度"),   sel_lane_w)) &
        df["_tags"].apply(lambda d: _tag_match(d, T("一、道路静态环境"), T("1.2 道路表面"),       T("表面类型"),   sel_surf))   &
        df["_tags"].apply(lambda d: _tag_match(d, T("一、道路静态环境"), T("1.2 道路表面"),       T("表面状态"),   sel_surf_s)) &
        df["_tags"].apply(lambda d: _tag_match(d, T("一、道路静态环境"), T("1.3 道路几何"),       T("坡度"),       sel_slope))  &
        df["_tags"].apply(lambda d: _tag_match(d, T("一、道路静态环境"), T("1.3 道路几何"),       T("曲率"),       sel_curv))   &
        df["_tags"].apply(lambda d: _tag_match(d, T("一、道路静态环境"), T("1.3 道路几何"),       T("横坡"),       sel_bank))   &
        df["_tags"].apply(lambda d: _tag_match(d, T("一、道路静态环境"), T("1.5 道路边缘"),       T("边缘类型"),   sel_edge))   &
        df["_tags"].apply(lambda d: _tag_match(d, T("二、交通设施"),     T("2.1 交通控制"),       T("信号灯"),     sel_sig))    &
        df["_tags"].apply(lambda d: _tag_match(d, T("二、交通设施"),     T("2.1 交通控制"),       T("标志牌"),     sel_sign))   &
        df["_tags"].apply(lambda d: _tag_match(d, T("二、交通设施"),     T("2.1 交通控制"),       T("地面标签"),   sel_mark))   &
        df["_tags"].apply(lambda d: _tag_match(d, T("二、交通设施"),     T("2.2 路侧与周边环境"), T("设施"),       sel_fac))    &
        df["_tags"].apply(lambda d: _tag_match(d, T("二、交通设施"),     T("2.3 特殊设施"),       T("类型"),       sel_spec))   &
        df["_tags"].apply(lambda d: _tag_match(d, T("三、动态目标 (路面状况)"), T("3.1 机动车"),   T("类型"),       sel_mv))     &
        df["_tags"].apply(lambda d: _tag_match(d, T("三、动态目标 (路面状况)"), "3.2 VRU",      T("类型"),       sel_vru))    &
        df["_tags"].apply(lambda d: _tag_match(d, T("三、动态目标 (路面状况)"), T("3.4 障碍物"),   T("类型"),       sel_obs))    &
        df["_tags"].apply(lambda d: _tag_match(d, T("四、大气环境"),     T("4.1 天气"),           T("类型"),       sel_wth))    &
        df["_tags"].apply(lambda d: _tag_match(d, T("四、大气环境"),     T("4.3 光照"),           T("来源"),       sel_light))  &
        df["_tags"].apply(lambda d: _tag_match(d, T("四、大气环境"),     T("4.3 光照"),           T("强度"),       sel_lumi))   &
        df["_tags"].apply(lambda d: _tag_match(d, T("四、大气环境"),     T("4.4 气温"),           T("估算"),       sel_temp))
    ].copy()
    df_f["duration"] = df_f["duration"].fillna(0)

    # ── KPI 卡片 ──────────────────────────────────────────────────────
    n_labeled = len(df_f)
    n_cities  = df_f["city"].nunique()
    n_locs    = df_f["location_name"].nunique()
    hrs       = df_f["duration"].sum() / 60

    if "kpi_open" not in st.session_state:
        st.session_state["kpi_open"] = None

    kpi_defs = [
        ("kpi-blue",   "📹", f"{n_labeled:,}", T("视频总数"),      "videos"),
        ("kpi-indigo", "🌆", str(n_cities),    T("覆盖城市"),      "cities"),
        ("kpi-teal",   "⏱️",  f"{hrs:.1f}",    T("总时长（小时）"), None),
        ("kpi-purple", "📍", str(n_locs),      T("地点数"),        "locations"),
    ]
    kpi_cols = st.columns(4)
    for col, (cls, icon, num, lbl, key) in zip(kpi_cols, kpi_defs):
        with col:
            st.markdown(
                f"<div class='kpi-card {cls}'>"
                f"<div class='kpi-icon'>{icon}</div>"
                f"<div class='kpi-num'>{num}</div>"
                f"<div class='kpi-lbl'>{lbl}</div>"
                f"</div>", unsafe_allow_html=True
            )
            if key:
                is_open = st.session_state["kpi_open"] == key
                if st.button(T("▾ 收起") if is_open else T("▸ 展开详情"),
                             key=f"kpi_btn_{key}", use_container_width=True):
                    st.session_state["kpi_open"] = None if is_open else key
                    st.rerun()
            else:
                st.markdown(
                    f"<p style='text-align:center;font-size:0.76rem;color:#888;margin:6px 0'>{T('含图片数据集')}</p>",
                    unsafe_allow_html=True
                )

    # ── 展开面板 ──────────────────────────────────────────────────────
    _open = st.session_state["kpi_open"]

    if _open == "videos":
        st.markdown(T("#### 📹 视频 / 场景列表"))
        _cols = ["city", "location_name", "top_road_category", "top_road_subcategory",
                 "duration", "collection_time", "source"]
        disp = df_f[[c for c in _cols if c in df_f.columns]].copy()
        disp["duration"] = disp["duration"].apply(fmt_dur)
        disp = disp.rename(columns={
            "city": T("城市"), "location_name": T("地点"),
            "top_road_category": T("道路主类"), "top_road_subcategory": T("道路子类"),
            "duration": T("时长"), "collection_time": T("采集时间"), "source": T("来源")
        })
        st.dataframe(disp, use_container_width=True, height=400)

    elif _open == "cities":
        st.markdown(T("#### 🌆 各城市概览"))
        city_sum = (df_f.groupby("city")
                    .agg(场景数=("id", "count"), 总时长=("duration", "sum"))
                    .reset_index()
                    .assign(时长_h=lambda x: (x["总时长"] / 60).round(1))
                    .sort_values("场景数", ascending=False))
        n_c = min(4, len(city_sum))
        if n_c:
            c_cols = st.columns(n_c)
            for i, (_, r) in enumerate(city_sum.iterrows()):
                with c_cols[i % 4]:
                    st.markdown(
                        f"<div style='background:linear-gradient(135deg,#1a56a8,#0891b2);"
                        f"border-radius:12px;padding:16px 12px;color:#fff;"
                        f"margin-bottom:10px;text-align:center'>"
                        f"<div style='font-size:1.1rem;font-weight:800;margin-bottom:6px'>{r['city']}</div>"
                        f"<div style='font-size:0.82rem;opacity:.9'>⏱ {r['时长_h']}h</div>"
                        f"<div style='font-size:0.82rem;opacity:.9'>📹 {r['场景数']} 场景</div>"
                        f"</div>", unsafe_allow_html=True
                    )

    elif _open == "locations":
        st.markdown(T("#### 📍 地点卡片墙"))
        _render_location_wall(df_f)

    st.divider()

    # ── 地域与时间分布 ──
    section_header(
        "地域与时间分布", "Geographic & Temporal Distribution",
        "覆盖 8 座城市、5 个采集时段的分布概览",
        "Coverage across 8 cities and 5 collection-time windows",
    )
    c1, c2 = st.columns(2)
    with c1:
        cd = df_f.groupby("city").agg(数量=("id","count")).reset_index()
        cd["city_display"] = cd["city"].apply(T)  # translate tick labels at plot time
        if not cd.empty:
            fig = px.bar(cd, x="city_display", y="数量", text="数量", color="city_display",
                         color_discrete_sequence=BLUE, title=T("各城市标注数量"),
                         labels={"city_display": T("城市"), "数量": T("数量")})
            fig.update_traces(textposition="outside", textfont=dict(size=11, color="#0d2d5e"),
                              marker_line_width=0, opacity=0.88)
            fig.update_layout(**_cl(), showlegend=False)
            st.plotly_chart(fig, use_container_width=True)
    with c2:
        ld = (df_f.groupby("location_name").agg(时长=("duration","sum")).reset_index()
              .assign(**{"时长(h)": lambda x: x["时长"] / 60})
              .sort_values("时长(h)", ascending=False).head(15))
        if not ld.empty:
            fig = px.bar(ld, x="location_name", y="时长(h)", color="location_name",
                         color_discrete_sequence=BLUE, title=T("各地点视频总时长 (Top 15)"),
                         labels={"location_name":T("地点"),"时长(h)":T("时长(h)")})
            fig.update_traces(marker_line_width=0, opacity=0.88)
            fig.update_layout(**_cl(), showlegend=False, xaxis_tickangle=-45)
            st.plotly_chart(fig, use_container_width=True)

    # ── 采集时段 ──
    section_header(
        "采集时段分布", "Collection Time Distribution",
        "按早高峰 / 日常 / 晚高峰 / 夜间 切分",
        "Split by morning peak / daytime / evening peak / night windows",
    )
    df_f["period"] = df_f["_period"]
    pd_df = df_f.groupby("period").size().reset_index(name="数量")
    pd_df["_o"] = pd_df["period"].apply(lambda x: PERIOD_ORDER.index(x) if x in PERIOD_ORDER else 99)
    pd_df = pd_df.sort_values("_o").drop(columns=["_o"])
    pd_df["period_display"] = pd_df["period"].apply(T)
    if not pd_df.empty:
        fig = px.bar(pd_df, x="period_display", y="数量", text="数量", color="period_display",
                     color_discrete_sequence=BLUE, title=T("采集时段分布"),
                     labels={"period_display": T("时段"), "数量": T("数量")},
                     category_orders={"period_display": pd_df["period_display"].tolist()})
        fig.update_traces(textposition="outside", textfont=dict(size=11, color="#0d2d5e"),
                          marker_line_width=0, opacity=0.88)
        fig.update_layout(**_cl(), showlegend=False)
        st.plotly_chart(fig, use_container_width=True)

    # ── 道路类型旭日图 ──
    section_header(
        "道路类型分布", "Road Type Distribution",
        "对齐 GB/T 45312-2025 ODD 三级分类的数据覆盖",
        "GB/T 45312-2025 three-level road-type coverage",
    )
    top_df = df_f[df_f["top_road_category"].notna() & (df_f["top_road_category"] != "")]
    if not top_df.empty:
        rows = []
        for _, row in top_df.iterrows():
            vals = extract_tag(row["_tags"],T("一、道路静态环境"),T("1.6 道路交叉"),T("交叉类型"))
            for ct in (vals or [T("未标注")]):
                rows.append({"主类":row["top_road_category"],
                             "子类":row.get("top_road_subcategory",""),"交叉类型":ct})
        if rows:
            sb = pd.DataFrame(rows).groupby(["主类","子类","交叉类型"]).size().reset_index(name="数量")
            # Pre-translate ring labels so EN mode shows English in every segment
            for _col in ("主类", "子类", "交叉类型"):
                sb[_col] = sb[_col].apply(T)
            fig = px.sunburst(sb, path=["主类","子类","交叉类型"], values="数量",
                              title=T("道路类型层级分布"), color_discrete_sequence=BLUE)
            fig.update_traces(textfont=dict(size=11))
            fig.update_layout(**_cl(), margin=dict(t=52,l=0,r=0,b=0))
            st.plotly_chart(fig, use_container_width=True)

    # ── 标签分布 ──
    section_header(
        "标签分布", "Tag Distribution",
        "按任意 ODD 标签维度查看数据密度",
        "Data density across any ODD tag dimension",
    )
    # Build option labels with T() per component; indices in TAG_PATHS are preserved
    tag_opts = [f"{T(sub)} · {T(attr)}" for _, sub, attr in TAG_PATHS]
    sel_tag  = st.selectbox(T("标签维度"), tag_opts, key="dash_tag")
    sec, sub, attr = TAG_PATHS[tag_opts.index(sel_tag)]
    items = []
    for d in df_f["_tags"]:
        v = extract_tag(d, sec, sub, attr)
        if v: items.extend(v)
    if items:
        cnt = pd.Series(items).value_counts().reset_index()
        # Keep internal column names stable (English) — px.bar/px.pie reference
        # these directly. Display labels are set via layout.xaxis_title etc.
        cnt.columns = ["tag_value", "count"]
        chart_types = [T("柱状图"), T("饼图")]
        ct = st.radio(T("图表类型"), chart_types, horizontal=True, key="dchart")
        title_txt = f"{T(sub)} · {T(attr)}"
        if ct == chart_types[1]:  # 饼图 / Pie
            fig = px.pie(cnt, values="count", names="tag_value",
                         title=title_txt, color_discrete_sequence=BLUE, hole=0.38)
            fig.update_traces(texttemplate="%{label}<br>%{percent:.1%}",
                              textposition="outside", textfont=dict(size=11), pull=[0.03]*len(cnt))
        else:  # 柱状图 / Bar
            fig = px.bar(cnt, x="tag_value", y="count", text="count", color="tag_value",
                         color_discrete_sequence=BLUE, title=title_txt)
            fig.update_traces(textposition="outside", textfont=dict(size=11, color="#0d2d5e"),
                              marker_line_width=0, opacity=0.88)
            fig.update_layout(**_cl(), showlegend=False, xaxis_tickangle=-30,
                              xaxis_title=T("标签值"), yaxis_title=T("数量"))
        fig.update_layout(**_cl())
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info(T("该维度暂无数据"))

    # ── 交叉分析 ──
    section_header(
        "交叉分析", "Cross Analysis",
        "两维标签交叉聚合，快速定位样本稀疏的场景组合",
        "Two-dimension tag cross-aggregation; spot scenarios with sparse samples",
    )
    DIMS = [("city",T("城市")),("top_road_category",T("道路主类")),
            ("top_road_subcategory",T("道路子类")),("period",T("时段"))]
    lbls, keys = [v for _,v in DIMS], [k for k,_ in DIMS]
    dc1, dc2 = st.columns(2)
    with dc1: s1 = st.selectbox(T("维度 1"), lbls, key="cx1")
    with dc2: s2 = st.selectbox(T("维度 2"), lbls, index=1, key="cx2")
    k1, k2 = keys[lbls.index(s1)], keys[lbls.index(s2)]
    if k1 != k2 and all(k in df_f.columns for k in (k1, k2)):
        pivot = df_f.groupby([k1, k2]).size().unstack(fill_value=0)
        st.dataframe(pivot.style.background_gradient(cmap="Blues"), use_container_width=True)

    with st.expander(T("📋 原始数据")):
        cols_show = ["source","folder_path","city","location_name","top_road_category",
                     "top_road_subcategory","collection_time","duration"]
        st.dataframe(df_f[[c for c in cols_show if c in df_f.columns]].head(200),
                     use_container_width=True)

# ─── 页面 3：新增地点 ─────────────────────────────────────────────────
_PHOTO_META = {
    "PHOTO/十堰/麻安高速":     {"scene_type":T("环岛+高速路出入口"),"flight_height":200,  "data_size_gb":20.42},
    "PHOTO/十堰/园林路":       {"scene_type":T("T型路口"),         "flight_height":100,  "data_size_gb":16.62},
    "PHOTO/抚州/旧小区":       {"scene_type":T("T型路口"),         "flight_height":100,  "data_size_gb":30.0},
    "PHOTO/抚州/龙山大道":     {"scene_type":T("十字路口"),        "flight_height":120,  "data_size_gb":25.0},
    "PHOTO/抚州/街心花园":     {"scene_type":T("十字路口"),        "flight_height":100,  "data_size_gb":25.0},
    "PHOTO/贵港/金港大道":     {"scene_type":T("拥挤路段"),        "flight_height":120,  "data_size_gb":10.0},
    "PHOTO/贵港/北环金港立交": {"scene_type":T("立交桥"),          "flight_height":300,  "data_size_gb":10.0},
    "PHOTO/贵港/工园支路":     {"scene_type":T("工业区支路"),      "flight_height":120,  "data_size_gb":10.0},
    "PHOTO/贵港/北环金港匝道": {"scene_type":T("合流区"),          "flight_height":120,  "data_size_gb":30.0},
    "PHOTO/安阳/集市路":       {"scene_type":T("拥挤路段"),        "flight_height":100,  "data_size_gb":4.0},
    "PHOTO/安阳/台辉高速":     {"scene_type":T("高速路"),          "flight_height":120,  "data_size_gb":0.45},
}

def page_photo():
    st.markdown("<div class='tip'>📸 以下地点仅有代表图片（无上传视频），由 VLM 自动打标签存入数据库。可在此查看标注结果并进行人工复核。</div>",
                unsafe_allow_html=True)
    try:
        with db_conn() as conn:
            df_photo = pd.read_sql(
                "SELECT * FROM dataset WHERE folder_path LIKE 'PHOTO/%' ORDER BY folder_path", conn
            )
    except Exception:
        df_photo = pd.DataFrame()

    if df_photo.empty:
        st.warning(T("暂无图片地点数据。请先运行 `python step3_photo_label.py` 完成自动标注。"))
        st.code("conda activate label\npython step3_photo_label.py", language="bash")
        return

    # KPI
    n_locs   = len(df_photo)
    cities_n = df_photo["folder_path"].apply(lambda p: p.split("/")[1] if "/" in p else "").nunique()
    total_gb = sum(_PHOTO_META.get(fp, {}).get("data_size_gb", 0) for fp in df_photo["folder_path"])
    k1, k2, k3 = st.columns(3)
    for col, icon, val, lbl in [
        (k1, "📍", n_locs,           T("图片地点数")),
        (k2, "🌆", cities_n,         T("覆盖城市")),
        (k3, "💾", f"{total_gb:.2f} GB", T("数据总量")),
    ]:
        with col:
            st.markdown(f"""<div class="kpi-card" style="background:linear-gradient(135deg,#1a56a8,#0891b2);
                border-radius:14px;padding:18px 20px;text-align:center;color:#fff;margin-bottom:8px">
                <div style="font-size:1.6rem">{icon}</div>
                <div style="font-size:1.5rem;font-weight:800">{val}</div>
                <div style="font-size:0.8rem;opacity:.8">{lbl}</div>
            </div>""", unsafe_allow_html=True)
    st.divider()

    # 地点卡片（每行2个）
    rows_data = [df_photo.iloc[i:i+2] for i in range(0, len(df_photo), 2)]
    for row_df in rows_data:
        cols = st.columns(2)
        for ci, (_, row) in enumerate(row_df.iterrows()):
            fp   = row["folder_path"]
            city = fp.split("/")[1] if fp.count("/") >= 1 else "?"
            meta = _PHOTO_META.get(fp, {})
            with cols[ci]:
                img_p = Path(row.get("image_path") or "")
                if img_p.exists():
                    st.image(str(img_p), use_container_width=True)
                else:
                    st.markdown("<div style='background:#f0f4fa;border-radius:8px;padding:30px;"
                                "text-align:center;color:#8899aa;border:2px dashed #c5d5e8'>"
                                "🖼 图片不存在</div>", unsafe_allow_html=True)

                st.markdown(f"""<div style="background:linear-gradient(90deg,#1a56a8,#0891b2);
                    border-radius:8px;padding:10px 14px;margin:6px 0 4px">
                    <div style="font-size:1rem;font-weight:700;color:#fff">{city} · {row['location_name']}</div>
                    <div style="font-size:0.78rem;color:rgba(255,255,255,0.75);margin-top:2px">
                        {meta.get('scene_type','—')} &nbsp;|&nbsp; 高度 {meta.get('flight_height','?')}m
                        &nbsp;|&nbsp; 数据 {meta.get('data_size_gb','?')} GB
                    </div></div>""", unsafe_allow_html=True)

                top_cat = row.get("top_road_category") or "—"
                top_sub = row.get("top_road_subcategory") or "—"
                st.markdown(f"<div style='font-size:0.82rem;color:#1a56a8;margin:2px 0'>🛣️ <b>{top_cat}</b> / {top_sub}</div>",
                            unsafe_allow_html=True)

                tags = json.loads(row["secondary_tags_json"]) if row.get("secondary_tags_json") else {}
                with st.expander(T("查看/编辑标签")):
                    for sec, sub, attr in [
                        (T("一、道路静态环境"),T("1.2 道路表面"),T("表面类型")),
                        (T("一、道路静态环境"),T("1.3 道路几何"),T("坡度")),
                        (T("一、道路静态环境"),T("1.4 包含车道特征"),T("最宽车道数量")),
                        (T("一、道路静态环境"),T("1.6 道路交叉"),T("交叉类型")),
                        (T("二、交通设施"),T("2.1 交通控制"),T("信号灯")),
                        (T("二、交通设施"),T("2.2 路侧与周边环境"),T("设施")),
                        (T("三、动态目标 (路面状况)"),T("3.1 机动车"),T("类型")),
                        (T("三、动态目标 (路面状况)"),"3.2 VRU",T("类型")),
                        (T("四、大气环境"),T("4.1 天气"),T("类型")),
                        (T("四、大气环境"),T("4.3 光照"),T("强度")),
                    ]:
                        val = tags.get(sec, {}).get(sub, {}).get(attr)
                        if val:
                            disp = "、".join(val) if isinstance(val, list) else val
                            st.markdown(f"<div style='font-size:0.78rem;line-height:1.7'>"
                                        f"<span style='color:#8899aa'>{sub}·{attr}</span>: "
                                        f"<b style='color:#0d2d5e'>{disp}</b></div>",
                                        unsafe_allow_html=True)
                    st.markdown("---")
                    with st.form(key=f"ph_{fp.replace('/','_')}"):
                        cats = list(TOP_LEVEL_CONFIG.keys())
                        pc1, pc2 = st.columns(2)
                        with pc1:
                            new_cat = st.selectbox(T("道路主类"), cats,
                                index=cats.index(top_cat) if top_cat in cats else 0,
                                key=f"phc_{fp}", format_func=T)
                        with pc2:
                            subs = TOP_LEVEL_CONFIG.get(new_cat, [])
                            new_sub = st.selectbox(T("道路子类"), subs,
                                index=subs.index(top_sub) if top_sub in subs else 0,
                                key=f"phs_{fp}", format_func=T)
                        new_cmt = st.text_input(T("备注"), value=row.get("comments","") or "", key=f"phm_{fp}")
                        if st.form_submit_button(T("💾 保存复核"), type="primary"):
                            with db_conn() as conn:
                                conn.execute(
                                    "UPDATE dataset SET top_road_category=?,top_road_subcategory=?,"
                                    "comments=?,label_time=? WHERE folder_path=?",
                                    (new_cat, new_sub, new_cmt, datetime.now().isoformat(), fp)
                                )
                            load_df.clear()
                            st.success(T("✅ 已保存")); st.rerun()


def page_add():
    st.markdown("<div class='tip'>用于没有视频文件但需要录入标注数据的地点，"
                "或补录已采集地点的额外信息。</div>", unsafe_allow_html=True)

    with st.form("add_loc_form"):
        st.markdown(T("#### 📋 基本信息"))
        r1c1, r1c2, r1c3 = st.columns(3)
        with r1c1: city_input = st.selectbox(T("城市"), list(CITY_MAP.values())+[T("其他")], key="add_city")
        with r1c2: year_input = st.text_input(T("年份"), value=str(datetime.now().year), key="add_year")
        with r1c3: loc_name   = st.text_input(T("地点名称 (英文/编号)"), key="add_loc")

        r2c1, r2c2 = st.columns(2)
        with r2c1: coll_date  = st.date_input(T("采集日期"), key="add_date")
        with r2c2: coll_hour  = st.slider("采集小时", 0, 23, 9, key="add_hour")

        duration_min   = st.number_input(T("视频总时长 (分钟)"), min_value=0.0, step=1.0, key="add_dur")
        comments       = st.text_area(T("备注"), height=60, key="add_cmt")

        st.markdown(T("#### 🖼️ 图片"))
        img_mode       = st.radio(T("图片来源"), [T("上传图片"),T("输入路径")], horizontal=True, key="add_imgmode")
        uploaded_file  = None
        img_path_input = ""
        if img_mode == "上传图片":
            uploaded_file = st.file_uploader("上传代表图 (JPG/PNG)", type=["jpg","jpeg","png"], key="add_file")
        else:
            img_path_input = st.text_input(T("图片绝对路径"), key="add_imgpath")

        st.markdown(T("#### 🛣️ 道路类型"))
        cats = list(TOP_LEVEL_CONFIG.keys())
        ac1, ac2 = st.columns(2)
        with ac1: top_cat = st.selectbox(T("道路主类"), cats, key="add_cat", format_func=T)
        with ac2: top_sub = st.selectbox(T("道路子类"), TOP_LEVEL_CONFIG[top_cat], key="add_sub", format_func=T)

        st.markdown(T("#### 📍 静态标签"))
        static_tags  = render_schema_section(STATIC_SCHEMA, {}, "add_st")
        st.markdown(T("#### 🎬 动态标签"))
        dynamic_tags = render_schema_section(DYNAMIC_SCHEMA, {}, "add_dy")

        submitted = st.form_submit_button(T("💾 保存新地点"), type="primary", use_container_width=True)

    if submitted:
        if not loc_name.strip():
            st.error(T("地点名称不能为空")); return

        final_img_path = ""
        if img_mode == T("上传图片") and uploaded_file:
            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            save_path = UPLOAD_DIR / f"{loc_name}_{uploaded_file.name}"
            save_path.write_bytes(uploaded_file.read())
            final_img_path = str(save_path)
        elif img_path_input:
            final_img_path = img_path_input

        save_manual_location(
            folder_path     = f"MANUAL/{city_input}/{year_input}/{loc_name}",
            video_name      = "MANUAL",
            location_name   = loc_name,
            collection_time = f"{coll_date.year}年{coll_date.month}月{coll_date.day}日 {coll_hour:02d}:00",
            top_cat         = top_cat,
            top_sub         = top_sub,
            all_tags        = {**static_tags, **dynamic_tags},
            duration        = float(duration_min),
            image_path      = final_img_path,
            comments        = comments,
        )
        load_df.clear()
        st.success(f"✅ 新地点「{loc_name}」已保存！")

# ─── CSS（完全对齐截图平台视觉风格） ────────────────────────────────
CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

/* ══════════════════════════════════════════════════════
   Design tokens — sourced from driveresearch.tech/styles.css
     --navy        #0f1923    深蓝（nav / dark sections）
     --navy-light  #1a2636
     --blue        #3d6b8e    主品牌蓝
     --blue-light  #4d7fa6
     --accent      #c85a3a    橙红强调（CTA / active）
     --white       #faf9f5    暖白
     --gray-50/100/200/400/600/800  灰阶
   字体：Inter（EN）+ PingFang SC / Noto Sans SC（ZH）
══════════════════════════════════════════════════════ */

/* ── 全局字体（对齐 driveresearch.tech 的 Inter + PingFang SC fallback）── */
html, body, [class*="css"], .stApp, .block-container,
/* 强制 Streamlit 所有子组件继承 Inter，覆盖默认的 Source Sans Pro */
h1, h2, h3, h4, h5, h6, p, div, a, li, ul,
button, input, select, textarea, label,
.stMarkdown, .stTabs, .stButton, .stRadio, .stSelectbox, .stMultiSelect,
[data-testid="stMarkdownContainer"] {
    font-family: 'Inter','PingFang SC','Noto Sans SC','Microsoft YaHei',
                 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif !important;
}
/* 例外：Material Symbols icons — Streamlit 用这个字体画下拉箭头、
   展开标记等 UI 图标，强制覆盖会让它们变成文字如 "keyboard_arrow_down" */
[data-testid="stIconMaterial"],
[data-testid*="stIcon"],
span.material-symbols-outlined,
span.material-icons,
[class*="material-symbols"] {
    font-family: 'Material Symbols Rounded','Material Symbols Outlined',
                 'Material Icons',sans-serif !important;
}
html, body, .stApp {
    font-size: 16px;
    line-height: 1.6;
    color: #2a2925;
    -webkit-font-smoothing: antialiased;
}

/* ── 隐藏 Streamlit 原生 chrome ── */
header[data-testid="stHeader"]       { display:none !important; height:0 !important; }
[data-testid="stToolbar"]            { display:none !important; }
[data-testid="stDecoration"]         { display:none !important; }
#MainMenu                            { display:none !important; }
footer                               { display:none !important; }
section[data-testid="stMain"]        { padding-top: 0 !important; }

/* ── 全局背景 ── */
.stApp { background: #faf9f5 !important; }

/* ── 主内容容器（扁平化、去掉卡片感） ── */
.block-container {
    background: transparent !important;
    /* 96px = 72 nav + 24 breathing; leaves room for the fixed nav. */
    padding: 96px 24px 64px !important;
    max-width: 1160px !important;
    box-shadow: none !important;
    border: none !important;
}

/* ══════════════════════════════════════════════════════
   Top nav — 对齐 driveresearch.tech 的 hero nav
══════════════════════════════════════════════════════ */
.drh-nav {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    width: 100%;
    z-index: 900;
    background: #0f1923;
    border-bottom: 1px solid rgba(255,255,255,0.08);
}
.drh-nav-inner {
    max-width: 1160px;
    margin: 0 auto;
    display: flex;
    align-items: center;
    justify-content: space-between;
    height: 72px;
    padding: 0 24px;
    gap: 28px;
}
.drh-nav-logo img {
    height: 38px;
    width: auto;
    display: block;
}
.drh-nav-links {
    display: flex;
    gap: 32px;
    list-style: none;
    margin: 0;
    padding: 0;
    flex: 1;
    justify-content: center;
}
.drh-nav-links a {
    color: #faf9f5;
    text-decoration: none;
    font-size: 15px;
    font-weight: 500;
    letter-spacing: 0.3px;
    opacity: 0.85;
    transition: opacity 0.2s, color 0.2s;
}
.drh-nav-links a:hover  { opacity: 1; color: #faf9f5; }
.drh-nav-links a.active { opacity: 1; color: #e8a08c; }
.drh-nav-lang-slot      { width: 110px; }

/* ── 平台全名标题条 ───────────────────────────────────────── */
.platform-title {
    text-align: center;
    padding: 18px 20px 14px;
    margin: 0 0 24px;
    border-bottom: 1px solid #eae9e4;
    color: #0f1923;
    font-size: clamp(15px, 2.2vw, 20px);
    font-weight: 600;
    letter-spacing: 0.4px;
    line-height: 1.45;
}
@media (max-width: 640px) {
    .platform-title { font-size: 13px; padding: 12px 16px 10px; }
}

/* ── AI Q&A 页面 ──────────────────────────────────────────── */
.qa-hero {
    padding: 8px 0 18px;
    border-bottom: 1px solid #eae9e4;
    margin-bottom: 24px;
}
.qa-hero h2 {
    font-size: clamp(22px, 3vw, 30px);
    font-weight: 700;
    margin: 0 0 6px;
    color: #0f1923;
    letter-spacing: -0.5px;
}
.qa-hero p {
    font-size: 14px;
    color: #5a5850;
    margin: 0;
    line-height: 1.55;
}
.qa-answer {
    background: #f4f3ef;
    border: 1px solid #eae9e4;
    border-left: 3px solid #c85a3a;
    border-radius: 8px;
    padding: 18px 22px;
    color: #2a2925;
    line-height: 1.7;
    font-size: 15px;
    margin-top: 18px;
    white-space: pre-wrap;
}
.qa-example-card {
    display: inline-block;
    padding: 8px 14px;
    margin: 4px 6px 4px 0;
    background: #faf9f5;
    border: 1px solid #e0ded6;
    border-radius: 999px;
    color: #5a5850;
    font-size: 13px;
    cursor: pointer;
}

/* ── 响应式 · 平板 ── */
@media (max-width: 900px) {
    .drh-nav-inner { padding: 0 16px; gap: 12px; }
    .drh-nav-links { gap: 20px; }
    .drh-nav-links a { font-size: 14px; }
    .drh-nav-logo img { height: 32px; }
}
/* ── 响应式 · 手机 ── */
@media (max-width: 640px) {
    .drh-nav { height: 56px; }
    .drh-nav-inner { height: 56px; padding: 0 12px; gap: 8px; }
    .drh-nav-logo img { height: 26px; }
    /* 手机上隐藏中部链接，只保留 logo + 语言切换，避免挤爆 */
    .drh-nav-links { display: none; }
    .drh-nav-lang-slot { width: auto; }
    .st-key-_lang_picker { top: 10px !important; right: 12px !important; }
    .st-key-_lang_picker [role="radiogroup"] > label { padding: 2px 9px !important; }
    .st-key-_lang_picker [role="radiogroup"] > label p { font-size: 11px !important; }
    /* Content 区再紧凑一些 */
    .block-container { padding: 72px 16px 48px !important; }
}

/* 语言切换 pill — Streamlit 给 key=_lang_picker 的 container 加了
   `.st-key-_lang_picker`；我们 fixed 到 nav 右上，风格对齐官网 pill */
.st-key-_lang_picker {
    position: fixed !important;
    top: 18px;
    right: 28px;
    z-index: 1001;
    width: auto !important;
    margin: 0 !important;
    padding: 0 !important;
}
.st-key-_lang_picker [data-testid="stRadio"] > label { display: none !important; }
.st-key-_lang_picker [role="radiogroup"] {
    flex-direction: row !important;
    background: rgba(255,255,255,0.08);
    border: 1px solid rgba(255,255,255,0.28);
    border-radius: 20px;
    padding: 2px;
    gap: 0 !important;
}
.st-key-_lang_picker [role="radiogroup"] > label {
    padding: 3px 12px !important;
    margin: 0 !important;
    border-radius: 14px;
    cursor: pointer;
    background: transparent !important;
}
.st-key-_lang_picker [role="radiogroup"] > label p {
    font-size: 12px !important;
    font-weight: 600 !important;
    color: rgba(255,255,255,0.75) !important;
    margin: 0 !important;
}
.st-key-_lang_picker [role="radiogroup"] > label:has(input:checked) {
    background: rgba(255,255,255,0.22) !important;
}
.st-key-_lang_picker [role="radiogroup"] > label:has(input:checked) p {
    color: #fff !important;
}
/* Hide Streamlit's radio dot */
.st-key-_lang_picker [role="radiogroup"] > label > div:first-child {
    display: none !important;
}

/* ══════════════════════════════════════════════════════
   响应式：让 st.columns 在手机上换行（默认是 flex nowrap）
══════════════════════════════════════════════════════ */
@media (max-width: 640px) {
    [data-testid="stHorizontalBlock"] {
        flex-wrap: wrap !important;
    }
    [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
        min-width: 45% !important;   /* 2 卡/行 */
        flex: 1 1 45% !important;
    }
    .kpi-card { padding: 28px 12px 22px; min-height: 120px; }
    .kpi-num  { font-size: 32px; }
    .kpi-lbl  { font-size: 12px; }
    .kpi-icon { font-size: 1.2rem; margin-bottom: 6px; }
    h2 { font-size: 20px !important; margin: 32px 0 12px !important; }
    h3 { font-size: 17px !important; margin: 24px 0 10px !important; }
    .stTabs [data-baseweb="tab"] { padding: 10px 12px !important; font-size: 14px !important; }
}

/* ══════════════════════════════════════════════════════
   Tabs — flat underline like drh nav-links
══════════════════════════════════════════════════════ */
.stTabs [data-baseweb="tab-list"] {
    gap: 4px;
    border-bottom: 1px solid #e0ded6;
    background: transparent;
    padding: 0;
    margin-bottom: 28px;
}
.stTabs [data-baseweb="tab"] {
    background: transparent !important;
    border-radius: 0 !important;
    border-bottom: 2px solid transparent !important;
    padding: 12px 20px !important;
    font-weight: 500 !important;
    font-size: 15px !important;
    color: #5a5850 !important;
    letter-spacing: 0.3px !important;
    transition: color 0.2s, border-color 0.2s;
}
.stTabs [data-baseweb="tab"]:hover { color: #0f1923 !important; }
.stTabs [aria-selected="true"] {
    color: #0f1923 !important;
    border-bottom: 2px solid #c85a3a !important;
    background: transparent !important;
    box-shadow: none !important;
}

/* ══════════════════════════════════════════════════════
   Headings
══════════════════════════════════════════════════════ */
h1, h2, h3, h4 {
    color: #0f1923 !important;
    font-weight: 700 !important;
    letter-spacing: -0.2px;
}
h2 { font-size: clamp(22px, 3vw, 32px) !important; margin: 48px 0 16px !important; padding-bottom: 0 !important; border-bottom: none !important; letter-spacing: -0.3px; }
h3 { font-size: 20px !important; margin: 32px 0 12px !important; }
h4 { font-size: 16px !important; margin: 24px 0 10px !important; }
hr { border-color: #eae9e4 !important; margin: 24px 0 !important; }

/* ══════════════════════════════════════════════════════
   Dashboard section title (drh-style: clean h2 + subtitle)
══════════════════════════════════════════════════════ */
.dash-section {
    margin: 72px 0 24px;
    padding-top: 8px;
    border-top: 1px solid #eae9e4;
}
.dash-section:first-of-type { border-top: none; padding-top: 0; margin-top: 40px; }
.dash-section-title {
    font-size: clamp(22px, 2.6vw, 28px) !important;
    font-weight: 700 !important;
    color: #0f1923 !important;
    letter-spacing: -0.3px;
    margin: 0 0 8px !important;
    padding: 0 !important;
    border: none !important;
    line-height: 1.25;
}
.dash-section-sub {
    font-size: 14px;
    color: #5a5850;
    line-height: 1.6;
    margin: 0 0 24px;
    max-width: 720px;
    letter-spacing: 0.1px;
}
@media (max-width: 640px) {
    .dash-section { margin: 48px 0 16px; }
    .dash-section-title { font-size: 20px !important; }
    .dash-section-sub { font-size: 13px; margin-bottom: 18px; }
}

/* ══════════════════════════════════════════════════════
   KPI cards — drh stat-card style (flat / bordered)
══════════════════════════════════════════════════════ */
.kpi-card {
    background: #fff;
    border: 1px solid #e0ded6;
    border-radius: 12px;
    padding: 36px 16px 28px;
    text-align: center;
    color: #2a2925;
    min-height: 140px;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    transition: transform 0.3s, box-shadow 0.3s;
    position: relative;
    overflow: hidden;
}
.kpi-card:hover {
    transform: translateY(-4px);
    box-shadow: 0 12px 40px rgba(0,0,0,0.08);
}
.kpi-card::after, .kpi-card::before { display: none; }
.kpi-icon {
    font-size: 1.5rem;
    color: #3d6b8e;
    margin-bottom: 10px;
}
.kpi-num {
    font-size: 40px;
    font-weight: 700;
    color: #0f1923;
    line-height: 1.1;
    letter-spacing: -0.5px;
    margin-bottom: 6px;
}
.kpi-lbl {
    font-size: 14px;
    font-weight: 600;
    color: #3d6b8e;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    opacity: 1;
}
/* Flatten the legacy gradient variants; locations keeps an accent top-border */
.kpi-blue, .kpi-indigo, .kpi-teal, .kpi-purple {
    background: #fff !important;
    box-shadow: none !important;
}
.kpi-purple { border-top: 3px solid #c85a3a !important; }
.kpi-expand-bar { display: none; }  /* drh has no expand rail */

/* ══════════════════════════════════════════════════════
   Buttons
══════════════════════════════════════════════════════ */
.stButton>button,
[data-testid="stMain"] .stButton>button {
    border-radius: 6px !important;
    border: 1px solid #e0ded6 !important;
    background: #fff !important;
    color: #2a2925 !important;
    font-weight: 600 !important;
    font-size: 15px !important;
    min-height: 36px !important;
    padding: 0.3rem 0.95rem !important;
    letter-spacing: 0.2px !important;
    transition: all 0.2s !important;
    box-shadow: none !important;
}
.stButton>button:hover,
[data-testid="stMain"] .stButton>button:hover {
    border-color: #3d6b8e !important;
    color: #3d6b8e !important;
    transform: none !important;
}
.stButton>button[kind="primary"] {
    background: #c85a3a !important;
    color: #fff !important;
    border-color: #c85a3a !important;
}
.stButton>button[kind="primary"]:hover {
    background: #b04e30 !important;
    border-color: #b04e30 !important;
    color: #fff !important;
    transform: translateY(-1px) !important;
}

/* ══════════════════════════════════════════════════════
   Expander / form / dataframe / metric
══════════════════════════════════════════════════════ */
details summary {
    background: #f4f3ef !important;
    border-radius: 6px !important;
    padding: 10px 14px !important;
    font-size: 14px !important;
    font-weight: 500 !important;
    color: #2a2925 !important;
    border: 1px solid #eae9e4 !important;
}
details[open] summary { margin-bottom: 8px; }

[data-testid="stForm"] {
    border: 1px solid #e0ded6;
    border-radius: 12px;
    padding: 24px;
    background: #fff;
    box-shadow: none;
}
[data-testid="stVerticalBlockBorderWrapper"] {
    background: #fff !important;
    border: 1px solid #e0ded6 !important;
    border-radius: 12px !important;
}
[data-testid="stDataFrame"] {
    border-radius: 8px !important;
    overflow: hidden;
    border: 1px solid #e0ded6 !important;
}
[data-testid="stMetric"] {
    background: #fff;
    border-radius: 12px;
    padding: 20px 24px;
    border: 1px solid #e0ded6;
    box-shadow: none;
}
[data-testid="stMetricLabel"] p {
    color: #3d6b8e !important;
    font-weight: 600;
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}
[data-testid="stMetricValue"] {
    color: #0f1923 !important;
    font-weight: 700;
}
[data-testid="stProgressBar"]>div { background: #c85a3a !important; }

/* Multiselect / selectbox tokens */
[data-testid="stMultiSelect"] [data-baseweb="tag"] {
    background: #eae9e4 !important;
    border: 1px solid #e0ded6 !important;
    color: #2a2925 !important;
    border-radius: 4px !important;
    font-size: 12px !important;
}
[data-testid="stMultiSelect"] label,
[data-testid="stSelectbox"] label {
    font-size: 13px !important;
    font-weight: 600 !important;
    color: #5a5850 !important;
    text-transform: uppercase;
    letter-spacing: 0.4px;
}

/* ══════════════════════════════════════════════════════
   Schema section bar / location cards / tip box
══════════════════════════════════════════════════════ */
.sec-bar {
    background: transparent;
    color: #0f1923;
    padding: 0 0 8px;
    border-bottom: 2px solid #c85a3a;
    font-size: 15px;
    font-weight: 700;
    margin: 24px 0 12px;
    display: inline-block;
}
.loc-list-header {
    background: #0f1923;
    color: #faf9f5;
    padding: 10px 16px;
    border-radius: 8px;
    font-weight: 600;
    font-size: 13px;
    margin-bottom: 14px;
    letter-spacing: 0.3px;
}
.loc-header {
    background: #0f1923;
    color: #faf9f5;
    border-radius: 12px;
    padding: 20px 24px;
    margin-bottom: 24px;
    display: flex;
    align-items: center;
    gap: 16px;
    flex-wrap: wrap;
    border: none;
    box-shadow: none;
}
.loc-header::before, .loc-header::after { display: none; }
.loc-city {
    font-size: 20px;
    font-weight: 700;
    color: #faf9f5;
    background: none;
    -webkit-text-fill-color: initial;
    letter-spacing: 0;
}
.loc-name { font-size: 15px; opacity: 0.75; font-weight: 500; }
.loc-stat { font-size: 13px; opacity: 0.65; margin-left: auto; }

.tip {
    background: #f4f3ef;
    border: 1px solid #eae9e4;
    border-left: 3px solid #c85a3a;
    border-radius: 6px;
    padding: 10px 14px;
    font-size: 13px;
    color: #5a5850;
    margin-bottom: 14px;
}

.img-card-meta { padding: 6px 10px 8px; font-size: 12px; color: #5a5850; line-height: 1.55; }
.img-card-meta b { color: #0f1923; font-weight: 600; }

.stCaption { color: #5a5850 !important; }

/* Remove the brand-header / tc-tl / tc-br leftovers from v3 template */
.brand-header, .brand-logo, .brand-text, .brand-title, .brand-sub, .brand-badge,
.tc-tl, .tc-br { display: none !important; }
@keyframes header-sweep { 0%{} 100%{} }
@keyframes title-glow   { 0%{} 100%{} }
@keyframes pulse-badge  { 0%{} 100%{} }
</style>
"""

# ─── 页面 4：AI 智能问答（Claude API） ────────────────────────────────
def _build_qa_system_prompt() -> str:
    """Build a compact system prompt with dataset schema + live stats.
    Cached at session level to avoid regenerating on every rerun."""
    if "_qa_sys" in st.session_state:
        return st.session_state["_qa_sys"]
    try:
        df = load_df()
    except Exception:
        df = pd.DataFrame()
    try:
        total_videos = int(len(df))
        total_hours  = float(df["duration"].sum() / 60) if "duration" in df.columns else 0.0
        city_counts  = df["city"].value_counts().to_dict() if "city" in df.columns else {}
        n_locs       = int(df["location_name"].nunique()) if "location_name" in df.columns else 0
    except Exception:
        total_videos, total_hours, city_counts, n_locs = 0, 0.0, {}, 0
    cities_str = ", ".join(f"{c}: {n}" for c, n in sorted(city_counts.items(), key=lambda x: -x[1]))
    tag_dims = "; ".join(f"{sub}·{attr}" for _, sub, attr in TAG_PATHS)
    prompt = f"""You are the dataset assistant for DRIVEResearch's Large-Scale Naturalistic Driving Dataset Management Platform.

The dataset is built from aerial (UAV) naturalistic driving video collected across Chinese cities. It follows the ISO-aligned ODD (Operational Design Domain) schema defined in GB/T 45312-2025.

Current live statistics (computed just now from the live SQLite DB):
- Total videos: {total_videos}
- Total duration: {total_hours:.1f} hours
- Distinct locations: {n_locs}
- Per-city video counts: {cities_str}

ODD tag dimensions available for analysis (sub-category · attribute):
{tag_dims}

Answer concisely and in the language the user asked in (Chinese or English). When quoting numbers, use the live statistics above. When users ask open-ended research/methodology questions (e.g. "Is this enough data for XX?", "Which scenarios are missing?"), give reasoned judgment grounded in the statistics above — do not refuse."""
    st.session_state["_qa_sys"] = prompt
    return prompt


def page_qa():
    # Hero
    st.markdown(f"""<div class="qa-hero">
  <h2>{T("🤖 智能问答")}</h2>
  <p>{T("基于 Claude 的数据集对话助手，可解答数据覆盖范围、ODD 标签分布、采集地点等问题")}</p>
</div>""", unsafe_allow_html=True)

    # API key gate — wrap in try/except because accessing st.secrets when
    # no secrets.toml exists at all raises StreamlitSecretNotFoundError.
    api_key = None
    try:
        api_key = st.secrets.get("ANTHROPIC_API_KEY")
    except Exception:
        api_key = os.environ.get("ANTHROPIC_API_KEY")  # dev fallback
    if not api_key:
        st.warning(T("请先在 Streamlit Cloud 的 Secrets 里配置 `ANTHROPIC_API_KEY`"))
        st.code('ANTHROPIC_API_KEY = "sk-ant-..."', language="toml")
        return

    # Simple in-session rate limit (20 requests/session)
    if "_qa_count" not in st.session_state:
        st.session_state["_qa_count"] = 0

    # Example chips
    examples = [
        T("数据集一共覆盖了哪些城市？"),
        T("长春的数据为什么占比这么高？"),
        T("哪些 ODD 标签维度采样最稀疏？"),
        T("高速公路和城市道路的时长分布如何？"),
    ]
    st.caption(T("示例问题"))
    ex_cols = st.columns(len(examples))
    picked_example = None
    for i, (c, q) in enumerate(zip(ex_cols, examples)):
        with c:
            if st.button(q, key=f"qa_ex_{i}", use_container_width=True):
                picked_example = q

    # Input area
    default_q = picked_example or st.session_state.get("_qa_pending", "")
    q = st.text_area(T("请输入你的问题…"),
                     value=default_q, height=90, key="_qa_input",
                     label_visibility="collapsed",
                     placeholder=T("请输入你的问题…"))
    col_a, col_b = st.columns([1, 5])
    with col_a:
        submit = st.button(T("📨 发送"), type="primary", use_container_width=True)

    # Handle submit
    if submit or picked_example:
        question = (picked_example or q or "").strip()
        if not question:
            st.error(T("问题不能为空")); return
        if len(question) > 500:
            st.error(T("问题过长（限 500 字）")); return
        if st.session_state["_qa_count"] >= 20:
            st.error(T("⚠️ 本小时请求已达上限（20 次），请稍后再试")); return

        with st.spinner(T("🤖 正在思考…")):
            try:
                from anthropic import Anthropic
                client = Anthropic(api_key=api_key)
                msg = client.messages.create(
                    model="claude-sonnet-4-5",
                    max_tokens=1024,
                    system=_build_qa_system_prompt(),
                    messages=[{"role": "user", "content": question}],
                )
                answer = msg.content[0].text if msg.content and msg.content[0].type == "text" else ""
                st.session_state["_qa_count"] += 1
                st.markdown(f'<div class="qa-answer">{answer}</div>', unsafe_allow_html=True)
            except Exception as e:
                st.error(f"{T('调用 Claude API 失败')}: {e}")


# ─── 主入口 ──────────────────────────────────────────────────────────
# URL-driven pages (via st.query_params):
#   ?page=dashboard  (default)
#   ?page=tagging
#   ?page=qa
# Top-nav links set `?page=…`, clearing Streamlit's hash-based state so
# navigation actually switches pages rather than no-op'ing.
PAGES = {
    "dashboard": {"zh": "看板",      "en": "Dashboard", "render": lambda: page_dashboard()},
    "tagging":   {"zh": "标注",      "en": "Tagging",   "render": lambda: page_label()},
    "qa":        {"zh": "智能问答",  "en": "AI Q&A",    "render": lambda: page_qa()},
}


def main():
    # ── 语言默认值 + URL 覆盖（必须在 set_page_config 之前，否则浏览器
    #    tab 标题永远用 session 默认 lang） ──
    if "lang" not in st.session_state:
        st.session_state.lang = "zh"
    qp = st.query_params
    qp_lang = qp.get("lang")
    if qp_lang in ("zh", "en") and st.session_state.lang != qp_lang:
        st.session_state.lang = qp_lang
    active = qp.get("page", "dashboard")
    if active not in PAGES:
        active = "dashboard"

    st.set_page_config(
        page_title=T("驭研科技自然驾驶数据集数据统计平台"),
        # Favicon — hosted on driveresearch.tech for visual consistency
        # with the main website tab icon.
        page_icon="https://driveresearch.tech/images/logo-square.png",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    st.markdown(CSS, unsafe_allow_html=True)
    init_db()

    # ══════════════════════════════════════════════════════
    # Nav — 视觉与 driveresearch.tech 对齐
    #   · 固定顶栏，solid navy
    #   · 左上：官网 logo-white.png（点击回官网）
    #   · 中：导航链接（Main site / Dashboard / Tagging / AI Q&A / GitHub）
    #   · 右：语言切换 pill（EN / 中文）
    # ══════════════════════════════════════════════════════
    lang = st.session_state.lang
    main_site_label = "Main site" if lang == "en" else "主站"
    gh_label        = "GitHub"

    # Build nav items from PAGES registry so adding a page updates nav automatically.
    # Encode lang in URL so a hard reload (nav link click) preserves language.
    nav_li = "".join(
        f'<li><a href="?page={key}&lang={lang}" target="_self" '
        f'class="{"active" if key == active else ""}">{meta[lang]}</a></li>'
        for key, meta in PAGES.items()
    )

    st.markdown(f"""
<nav class="drh-nav">
  <div class="drh-nav-inner">
    <a href="https://driveresearch.tech/" target="_blank" rel="noopener" class="drh-nav-logo">
      <img src="https://driveresearch.tech/images/logo-white.png" alt="DRIVEResearch">
    </a>
    <ul class="drh-nav-links">
      <li><a href="https://driveresearch.tech/" target="_blank" rel="noopener">{main_site_label}</a></li>
      {nav_li}
      <li><a href="https://github.com/AutoZYX/NDS-Label-Platform" target="_blank" rel="noopener">{gh_label}</a></li>
    </ul>
    <div class="drh-nav-lang-slot"></div>
  </div>
</nav>
""", unsafe_allow_html=True)

    # Language toggle — 通过 key="_lang_picker" 让 Streamlit 生成
    # `.st-key-_lang_picker` class，再在 CSS 里 fixed 到 nav 右上角。
    new_lang = st.radio(
        " ",
        options=["zh", "en"],
        format_func=lambda x: "中文" if x == "zh" else "EN",
        index=0 if st.session_state.lang == "zh" else 1,
        horizontal=True,
        label_visibility="collapsed",
        key="_lang_picker",
    )
    if new_lang != st.session_state.lang:
        st.session_state.lang = new_lang
        # Persist lang into URL so nav clicks on hard-reload keep it
        st.query_params["lang"] = new_lang
        st.query_params["page"] = active
        st.rerun()

    # ── 平台全名 · 中英双语（按当前 lang 显示）──
    st.markdown(
        f'<div class="platform-title">{T("驭研科技自然驾驶数据集数据统计平台")}</div>',
        unsafe_allow_html=True,
    )

    # ── Render active page ──
    PAGES[active]["render"]()


if __name__ == "__main__":
    main()