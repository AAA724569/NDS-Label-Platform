-- Approved two-table annotation storage for PostgreSQL 14+.
-- Execute with ON_ERROR_STOP using an authorized aerial schema maintenance role.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
SELECT pg_advisory_xact_lock(hashtextextended('aerial:001:annotation-storage', 0));

-- Keep ownership consistent with the existing video schema.
DO $preflight$
DECLARE
    schema_owner name;
BEGIN
    IF current_database() <> 'aerial_db' THEN
        RAISE EXCEPTION 'This migration requires the aerial_db database';
    END IF;
    SELECT pg_get_userbyid(nspowner) INTO schema_owner
      FROM pg_namespace WHERE nspname = 'aerial';
    IF schema_owner IS NULL OR to_regclass('aerial.videos') IS NULL THEN
        RAISE EXCEPTION 'Existing aerial schema and aerial.videos are required';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'reader') THEN
        RAISE EXCEPTION 'Existing reader role is required';
    END IF;
    EXECUTE format('SET LOCAL ROLE %I', schema_owner);
END
$preflight$;

CREATE TABLE aerial.annotation_batches (
    batch_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_directory TEXT NOT NULL UNIQUE
        CHECK (btrim(source_directory) <> ''),
    location_name TEXT CHECK (location_name IS NULL OR btrim(location_name) <> ''),
    representative_video_sha256 BYTEA
        CHECK (representative_video_sha256 IS NULL OR octet_length(representative_video_sha256) = 32),
    frame_timestamp_ms BIGINT,
    frame_path TEXT,
    frame_roi JSONB NOT NULL DEFAULT '{}'::JSONB
        CHECK (jsonb_typeof(frame_roi) = 'object'),
    supplemental_frames JSONB NOT NULL DEFAULT '[]'::JSONB
        CHECK (jsonb_typeof(supplemental_frames) = 'array'),
    selection_reason TEXT,
    raw_model_response TEXT,
    labels JSONB NOT NULL DEFAULT '[]'::JSONB
        CHECK (jsonb_typeof(labels) = 'array'),
    model_name TEXT,
    prompt_version TEXT,
    schema_version TEXT NOT NULL DEFAULT 'aerial_annotation_v1'
        CHECK (btrim(schema_version) <> ''),
    validation_passed BOOLEAN NOT NULL DEFAULT FALSE,
    validation_errors JSONB NOT NULL DEFAULT '[]'::JSONB
        CHECK (jsonb_typeof(validation_errors) = 'array'),
    review_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (review_status IN ('pending', 'approved', 'rejected')),
    reviewer_name TEXT,
    reviewed_at TIMESTAMPTZ,
    review_note TEXT,
    label_revision INTEGER NOT NULL DEFAULT 1 CHECK (label_revision > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT annotation_batches_complete_frame CHECK (
        (representative_video_sha256 IS NULL AND frame_timestamp_ms IS NULL AND frame_path IS NULL)
        OR
        (representative_video_sha256 IS NOT NULL AND frame_timestamp_ms IS NOT NULL
         AND frame_timestamp_ms >= 0 AND frame_path IS NOT NULL AND btrim(frame_path) <> '')
    ),
    CONSTRAINT annotation_batches_approved_evidence CHECK (
        review_status <> 'approved'
        OR
        (validation_passed AND validation_errors = '[]'::JSONB AND labels <> '[]'::JSONB
         AND representative_video_sha256 IS NOT NULL
         AND reviewer_name IS NOT NULL AND btrim(reviewer_name) <> '' AND reviewed_at IS NOT NULL)
    )
);

CREATE TABLE aerial.annotation_batch_videos (
    batch_id UUID NOT NULL REFERENCES aerial.annotation_batches(batch_id) ON DELETE CASCADE,
    video_sha256 BYTEA NOT NULL REFERENCES aerial.videos(sha256) ON DELETE RESTRICT
        CHECK (octet_length(video_sha256) = 32),
    PRIMARY KEY (batch_id, video_sha256),
    CONSTRAINT annotation_batch_videos_single_batch UNIQUE (video_sha256)
);

ALTER TABLE aerial.annotation_batches
    ADD CONSTRAINT annotation_batches_representative_member
    FOREIGN KEY (batch_id, representative_video_sha256)
    REFERENCES aerial.annotation_batch_videos(batch_id, video_sha256)
    DEFERRABLE INITIALLY DEFERRED;

-- Changing reviewed evidence requires validation and review of the new revision.
CREATE FUNCTION aerial.refresh_annotation_batch_state()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $function$
BEGIN
    IF ROW(NEW.source_directory, NEW.location_name, NEW.representative_video_sha256,
           NEW.frame_timestamp_ms, NEW.frame_path, NEW.frame_roi, NEW.supplemental_frames,
           NEW.labels, NEW.schema_version)
       IS DISTINCT FROM
       ROW(OLD.source_directory, OLD.location_name, OLD.representative_video_sha256,
           OLD.frame_timestamp_ms, OLD.frame_path, OLD.frame_roi, OLD.supplemental_frames,
           OLD.labels, OLD.schema_version)
    THEN
        NEW.label_revision := OLD.label_revision + 1;
        NEW.validation_passed := FALSE;
        NEW.review_status := 'pending';
        NEW.reviewer_name := NULL;
        NEW.reviewed_at := NULL;
    END IF;
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END
$function$;

CREATE TRIGGER annotation_batches_refresh_state
    BEFORE UPDATE ON aerial.annotation_batches
    FOR EACH ROW EXECUTE FUNCTION aerial.refresh_annotation_batch_state();

CREATE INDEX annotation_batches_review_status_idx
    ON aerial.annotation_batches(review_status);

COMMENT ON TABLE aerial.annotation_batches IS
    '每个电池采集文件夹一行：代表帧证据、模型原文、当前标签与审核；内部数据，公开统计需字段白名单。';
COMMENT ON COLUMN aerial.annotation_batches.source_directory IS '内部采集文件夹唯一标识；不作为公开地点名称。';
COMMENT ON COLUMN aerial.annotation_batches.location_name IS '经审核可使用的地点名称；缺少地点审核时为空。';
COMMENT ON COLUMN aerial.annotation_batches.representative_video_sha256 IS '代表帧源视频内容哈希，必须属于本批次。';
COMMENT ON COLUMN aerial.annotation_batches.frame_timestamp_ms IS '代表帧在源视频中的毫秒时间点；视频时长上限由写入接口验证。';
COMMENT ON COLUMN aerial.annotation_batches.frame_path IS '内部服务器图片位置；公开页面使用另行审核的图片资源。';
COMMENT ON COLUMN aerial.annotation_batches.frame_roi IS '观察道路区域对象；主路、辅路及单方向车道分别取证。';
COMMENT ON COLUMN aerial.annotation_batches.supplemental_frames IS '补帧数组：源视频哈希、视频内时间、图片位置、ROI 和补帧原因。';
COMMENT ON COLUMN aerial.annotation_batches.raw_model_response IS '原始模型响应 TEXT，可保存无效 JSON；不得存 API 密钥。';
COMMENT ON COLUMN aerial.annotation_batches.labels IS '当前标签数组：field/state/values/scope/origin/evidence；静态继承逐字段记录来源和快照。';
COMMENT ON COLUMN aerial.annotation_batches.model_name IS '实际返回的模型名称，例如 deepseek-flash。';
COMMENT ON COLUMN aerial.annotation_batches.validation_passed IS '写入接口完成完整标签与证据校验后的标记；合法 JSON 不等于标签校验通过。';
COMMENT ON COLUMN aerial.annotation_batches.label_revision IS '当前修订号；标签或证据变化自动递增。本方案不保存完整修订历史。';
COMMENT ON TABLE aerial.annotation_batch_videos IS '批次与原始视频关联；每个视频属于一个批次，视频数量和时长从 aerial.videos 计算。';

REVOKE ALL ON TABLE aerial.annotation_batches, aerial.annotation_batch_videos FROM PUBLIC;
REVOKE ALL ON TABLE aerial.annotation_batches, aerial.annotation_batch_videos FROM reader;
REVOKE ALL ON FUNCTION aerial.refresh_annotation_batch_state() FROM PUBLIC;
GRANT SELECT ON TABLE aerial.annotation_batches, aerial.annotation_batch_videos TO reader;

-- Fail the whole migration if role inheritance still gives reader write access.
DO $permissions$
DECLARE
    annotation_table text;
    privilege_name text;
BEGIN
    FOREACH annotation_table IN ARRAY ARRAY['aerial.annotation_batches', 'aerial.annotation_batch_videos']
    LOOP
        FOREACH privilege_name IN ARRAY ARRAY['INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER']
        LOOP
            IF has_table_privilege('reader', annotation_table, privilege_name) THEN
                RAISE EXCEPTION 'reader must be read-only on %; inherited % privilege remains',
                    annotation_table, privilege_name;
            END IF;
        END LOOP;
    END LOOP;
END
$permissions$;
COMMIT;
