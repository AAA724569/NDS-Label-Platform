# NDS Label Platform · 驭研科技 ODD 标注 / 统计平台

> 面向自动驾驶功能安全（SOTIF）与运行设计域（ODD）研究的航测自然驾驶数据标注与可视化平台。

**Live demo**: <https://driveresearchdata.streamlit.app/>
**Parent project**: [driveresearch.tech](https://driveresearch.tech/) · **Standard reference**: GB/T 45312-2025

---

## 1 · Overview (EN)

This repository hosts the internal data management platform for the **DRIVEResearch** aerial naturalistic driving dataset. It combines three capabilities:

1. **VLM-assisted labeling pipeline** — extract frames → VLM auto-label → human review.
2. **ODD labeling UI** — a Streamlit tool for researchers to annotate videos under the GB/T 45312-2025 operational design domain taxonomy.
3. **Statistics dashboard** — a public-facing dashboard that summarizes the dataset by city, road type, time of day, and tag distribution.

The dashboard is the `dashboard_st.py` entry point deployed on Streamlit Community Cloud; the labeling UI is `label_st.py`.

## 2 · 项目简介（中文）

本仓库是驭研科技（DRIVEResearch）航测自然驾驶数据集的内部数据管理平台，覆盖三条能力：

1. **VLM 辅助标注流水线** — 视频抽帧 → 视觉大模型自动标注 → 人工复核
2. **ODD 标注工具** — 面向研究生 / 标注员的 Streamlit 标注界面，标签体系对齐 GB/T 45312-2025
3. **统计看板** — 对外展示数据规模、城市 / 路型 / 时段 / 标签分布的可视化仪表盘

---

## 3 · Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Pipeline (offline)                                         │
│                                                             │
│  step1_extract_frames.py  ──►  frames/*.jpg                 │
│  step2_vlm_label.py       ──►  auto_labeled.db              │
│  step3_photo_label.py     ──►  photo_vlm_cache.json         │
└─────────────────────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│  Streamlit apps (online)                                    │
│                                                             │
│  dashboard_st.py   ◄── sessions_index.json                  │
│                    ◄── label_platform.db                    │
│                    ◄── auto_labeled.db                      │
│                                                             │
│  label_st.py       (manual annotation UI)                   │
└─────────────────────────────────────────────────────────────┘
```

### Key files

| File | Purpose |
|------|---------|
| `dashboard_st.py` | Public statistics dashboard (entry point on Streamlit Cloud) |
| `label_st.py` | Human annotation interface |
| `label_platform.py` | Legacy platform (pre-refactor) |
| `step1_extract_frames.py` | Extract representative frames from video sessions |
| `step2_vlm_label.py` | VLM-driven automatic ODD tagging |
| `step3_photo_label.py` | Photo-based location labeling |
| `sessions_index.json` | Index of all recording sessions |
| `label_platform.db` | SQLite store for manual annotations |
| `auto_labeled.db` | SQLite store for VLM auto-annotations |
| `frames/` | Representative frame images used by the dashboard preview |

---

## 4 · Quick start

### Requirements

- Python 3.11
- See [`requirements.txt`](requirements.txt)

### Local run

```bash
# create environment
conda create -n nds python=3.11 -y
conda activate nds
pip install -r requirements.txt

# launch dashboard
streamlit run dashboard_st.py

# launch annotation UI
streamlit run label_st.py
```

### Deploy to Streamlit Community Cloud

1. Fork this repo.
2. Connect the fork on <https://share.streamlit.io/>.
3. Set **main module** to `dashboard_st.py`, branch to `main`.
4. The `runtime.txt` pins Python to 3.11.

---

## 5 · Data snapshot

| Metric | Value |
|--------|-------|
| Videos | 1,744 |
| Cities covered | 8 |
| Total duration | 803.8 h |
| Location points | 38 |
| Primary coverage | Changchun (98%, seed city) |
| Other cities | Shenzhen, Hong Kong, Harbin, Anyang, Fuzhou, Guigang, Shiyan |

Data snapshot as of the most recent commit. The live numbers on the dashboard reflect the latest state of the underlying databases.

---

## 6 · Standard reference

Labeling follows **GB/T 45312-2025** — the Chinese national standard for ODD (Operational Design Domain) classification. The taxonomy is embedded in `dashboard_st.py` under `STATIC_SCHEMA` and `DYNAMIC_SCHEMA`.

The original standard PDF is not tracked in this repository. Contact the maintainers or the national standards portal for the authoritative version.

---

## 7 · Relation to driveresearch.tech

This platform is the data backbone of the [DRIVEResearch](https://driveresearch.tech/) public website. The website surfaces aggregated statistics; this repository produces them.

Roadmap for integration: align visual styling with the main site, expose the dashboard under `data.driveresearch.tech`, and sync the top-level metrics across both surfaces.

---

## 8 · Contributors

- **Development** · 研究生团队 (AD Safety Joint Lab, Jilin University)
- **Data** · DRIVEResearch (750h+ aerial NDS dataset)
- **Advisor** · 张玉新 · 吉林大学 · 自动驾驶安全联合实验室

---

## 9 · License

MIT License. See [`LICENSE`](LICENSE).
