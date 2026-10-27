# 航测标注结果存储

## 状态

本文件定义用户批准的两表方案。建表迁移是 `migrations/001_aerial_annotations.sql`。
迁移需由 `aerial` schema 所有者或可切换到该所有者的维护账号执行。
真实服务器执行结果以文末部署记录为准。

## 两张表

### `aerial.annotation_batches`

每个电池采集文件夹一个批次；`batch_id` 是 UUID，`source_directory` 是唯一的内部文件夹标识。
目录变更由维护人员显式修正，不能将移动后的目录自动当作另一块电池。

| 字段 | 内容 |
| --- | --- |
| `batch_id`, `source_directory`, `location_name` | 批次、内部文件夹、可空的审核地点名 |
| `representative_video_sha256`, `frame_timestamp_ms`, `frame_path` | 代表帧来源视频、视频内毫秒时间点、服务器图片位置 |
| `frame_roi`, `supplemental_frames`, `selection_reason` | 道路观察区域对象、可选补帧数组、选帧原因 |
| `raw_model_response` | 模型原始响应 TEXT，包括解析失败的响应；不包含 API 密钥 |
| `labels` | 当前整理后的标签 JSONB 数组，初始为空数组 |
| `model_name`, `prompt_version`, `schema_version` | 实际模型、提示词和标签格式版本 |
| `validation_passed`, `validation_errors` | 写入接口格式校验是否通过及错误数组 |
| `review_status`, `reviewer_name`, `reviewed_at`, `review_note` | pending/approved/rejected、审核人、审核时间、备注 |
| `label_revision`, `created_at`, `updated_at` | 当前修订号和记录时间 |

代表帧来源、时间点、位置必须全部为空或完整提供；时间点不能为负。
代表帧源视频必须属于此批次，数据库在提交时检查其关联。
补帧可以包含 `source_video_sha256`（十六进制）、`timestamp_ms`、`image_path`、`reason` 和 ROI。
图片保存在内部服务器；表中只保存其位置。

### `aerial.annotation_batch_videos`

包含 `batch_id` 与 `video_sha256` 两列，分别关联批次与现有 `aerial.videos.sha256`。
一个视频只属于一个电池批次；SHA256 在 PostgreSQL 内存为 32 字节 BYTEA。
删除批次会清理其关联行；存在标注关联的视频不能被硬删除，已有软删除流程保持适用。

## 标签格式

下列数据只说明格式，不能作为真实标注或标准合规结论：

```json
[
  {
    "field": "road.surface.material",
    "state": "observed",
    "values": ["沥青"],
    "scope": "frame",
    "origin": "model",
    "evidence": "representative"
  },
  {
    "field": "atmosphere.weather",
    "state": "unknown",
    "values": [],
    "scope": "frame",
    "origin": "model",
    "reason": "天空不可见"
  }
]
```

完整标签字典由后续标注接口按照 `schema_version` 实现：字段代码与合法值固定，单选数组最多一个值，
多选允许多个值且无重复。`observed`、`not_observed`、`unknown`、`unlabeled`、`not_applicable` 不混用。
`not_observed` 只表达采样帧观察范围，不能推导整批次不存在。

静态继承仍按字段保存，需记录 `origin: inherited`、来源批次与修订号、源帧证据、继承值快照和
适用路段。本帧冲突需复核；天气、路面干湿与动态目标保留本次观察，不能静态继承。
数据库本次检查顶层 JSON 结构；字段值、证据、继承条件、补帧成员和时间点小于视频时长等由后续接口校验。
`validation_passed` 是接口完成完整校验后设置的标记，不能因模型返回合法 JSON 就自动设为 true。

## 审核与更新

批次默认 `pending`。`approved` 必须具备完整代表帧、非空标签、通过校验、空错误列表、审核人和审核时间。
标签、帧证据、格式版本、目录或地点变更时，触发器递增 `label_revision`，清除审核人/时间、重置为待审核，
同时把 `validation_passed` 设为 false；`updated_at` 自动更新。
写入接口先保存新标签/证据，再验证同一修订号，人工复核后单独更新审核状态。
人工审核只更新整理标签和审核信息，原始模型响应保留；本方案没有完整的多次修改历史表。

## 创建与权限

迁移只适用于已有 `aerial.videos` 与 `reader` 的 `aerial_db`。
在维护账号正常环境中执行，例如通过已配置的 PostgreSQL service：

```bash
psql -X --set=ON_ERROR_STOP=1 --dbname='service=aerial_maintenance' \
  --file=migrations/001_aerial_annotations.sql
```

`aerial_maintenance` 只是配置名称示例，需要实际维护连接；不要将密码放入 Git、命令行或聊天。
迁移以 schema 的现有所有者创建对象，事务内执行并限制锁等待。
重复执行将因同名表存在而失败并回滚，避免将未知结构当作本次迁移已执行。

`reader` 对两张表获得 SELECT；不获得 INSERT/UPDATE/DELETE。
后续写入服务需要正常配置的专用写入账号，本迁移不创建账号或扩展 reader 为写入账号。
Metabase 同步表结构并确认用户组权限后可以查询；本次建表不自动同步 Metabase。
这些内部表含路径、源哈希、审核人和原始模型输出，公开站适配时只取审核标签和允许公开的城市/地点/帧。

## 查询示例

按批次计算有效视频数和实际时长，未知时长不伪造为零：

```sql
SELECT b.batch_id, b.location_name, b.review_status,
       COUNT(v.sha256) AS active_video_count,
       COUNT(v.duration_seconds) AS duration_known_videos,
       SUM(v.duration_seconds) AS duration_seconds
FROM aerial.annotation_batches AS b
LEFT JOIN aerial.annotation_batch_videos AS bv ON bv.batch_id = b.batch_id
LEFT JOIN aerial.videos AS v ON v.sha256 = bv.video_sha256 AND v.deleted_at IS NULL
GROUP BY b.batch_id;
```

展开已审核的标签供 Metabase 查询（内部查询示例，非公开导出）：

```sql
SELECT b.batch_id, b.location_name,
       label.item->>'field' AS field,
       label.item->>'state' AS observation_state,
       label.item->>'scope' AS evidence_scope,
       label.item->'values' AS label_values
FROM aerial.annotation_batches AS b
CROSS JOIN LATERAL jsonb_array_elements(b.labels) AS label(item)
WHERE b.review_status = 'approved';
```

同一批次补帧、多次推理或修订不增加视频数量；动态观察不扩为批次整段出现时长。
已有统计页面本次保持原有 PostgreSQL 视频统计，需要后续接入这两张表才能显示标签和审核进度。

## 验证

在测试环境安装 `requirements-dev.txt` 后运行 `python -m pytest -q`。
测试启动独立 PostgreSQL 14 的临时 Unix socket 实例，关闭 TCP 监听，退出时清理；不连接真实数据库。

## 部署记录

- 2026-10-14：确认真实服务器现有连接为 reader，schema/videos 所有者为 ad4che。
- 2026-10-14：用户提供维护入口后，使用 ad4che 成功执行迁移；密码仅在执行进程内通过隐藏输入提供，未写入代码或配置。
- 两张表所有者均为 ad4che，建表后均为 0 行；此次没有生成批次或调用标注模型。
- 建表前后 videos 总数均为 26,179，有效记录均为 22,594。
- reader 实际连接可查询两表；UPDATE/DELETE 的零行操作均被数据库拒绝，INSERT/UPDATE/DELETE 权限检查均为 false。
- 独立 PostgreSQL 14 全套测试 70 项通过，包括默认权限写入泄漏的回归验证。
- Metabase 需同步 schema 并按既有用户组权限查看；本次未操作其账号或结构同步入口。
