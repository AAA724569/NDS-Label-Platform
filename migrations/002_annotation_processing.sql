-- Operational queue state stays in the two existing annotation tables.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
DO $owner$
DECLARE owner_name name;
BEGIN
    IF current_database() <> 'aerial_db' THEN
        RAISE EXCEPTION 'This migration requires aerial_db';
    END IF;
    SELECT pg_get_userbyid(nspowner) INTO owner_name FROM pg_namespace WHERE nspname='aerial';
    EXECUTE format('SET LOCAL ROLE %I', owner_name);
END
$owner$;
ALTER TABLE aerial.annotation_batches
    ADD COLUMN processing_info JSONB NOT NULL DEFAULT '{"status":"queued","attempts":0}'::JSONB,
    ADD CONSTRAINT annotation_batches_processing_state CHECK (
        jsonb_typeof(processing_info) = 'object'
        AND processing_info ? 'status'
        AND jsonb_typeof(processing_info->'status') = 'string'
        AND processing_info->>'status' IN
            ('queued','extracting','labeling','completed','validation_failed','failed','skipped')
        AND processing_info ? 'attempts'
        AND jsonb_typeof(processing_info->'attempts') = 'number'
        AND (processing_info->>'attempts')::NUMERIC >= 0
        AND (processing_info->>'attempts')::NUMERIC = trunc((processing_info->>'attempts')::NUMERIC)
    );
COMMENT ON COLUMN aerial.annotation_batches.processing_info IS
    '后台标注状态/尝试次数/心跳/安全错误码/行政城市；completed 是已校验候选结果，与人工审核状态独立。';
CREATE INDEX annotation_batches_processing_status_idx
    ON aerial.annotation_batches ((processing_info->>'status'));
COMMIT;
