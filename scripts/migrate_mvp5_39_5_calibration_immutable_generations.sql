-- MVP5.39.5: immutable calibration generations + stable logical alias/head.
--
-- Compatibility design:
-- * calibration_versions.name remains the current publication/head name so
--   existing readers keep working without a mass query rewrite;
-- * logical_name + generation_no identify durable generations;
-- * when a new generation becomes current, the old head is renamed to a
--   deterministic archival physical name and never materialized into again;
-- * calibration_product_aliases is the canonical explicit head registry for
--   future readers once legacy name-based lookup is retired.

ALTER TABLE calibration_versions
    ADD COLUMN IF NOT EXISTS logical_name VARCHAR(128),
    ADD COLUMN IF NOT EXISTS generation_no INTEGER,
    ADD COLUMN IF NOT EXISTS finalized_at TIMESTAMPTZ;

UPDATE calibration_versions
SET logical_name = COALESCE(logical_name, name),
    generation_no = COALESCE(generation_no, 1)
WHERE logical_name IS NULL OR generation_no IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_calibration_versions_logical_generation
    ON calibration_versions (logical_name, generation_no)
    WHERE logical_name IS NOT NULL AND generation_no IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_calibration_versions_logical_name
    ON calibration_versions (logical_name);

CREATE TABLE IF NOT EXISTS calibration_product_aliases (
    alias_id BIGSERIAL PRIMARY KEY,
    logical_name VARCHAR(128) NOT NULL UNIQUE,
    head_calibration_version_id BIGINT NULL,
    head_generation_no INTEGER NOT NULL DEFAULT 0,
    updated_by_pipeline_run_id BIGINT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_calibration_product_aliases_head
        FOREIGN KEY (head_calibration_version_id)
        REFERENCES calibration_versions(calibration_version_id)
        ON DELETE SET NULL,
    CONSTRAINT fk_calibration_product_aliases_pipeline_run
        FOREIGN KEY (updated_by_pipeline_run_id)
        REFERENCES pipeline_runs(pipeline_run_id)
        ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS ix_calibration_product_aliases_head
    ON calibration_product_aliases (head_calibration_version_id);
CREATE INDEX IF NOT EXISTS ix_calibration_product_aliases_pipeline_run
    ON calibration_product_aliases (updated_by_pipeline_run_id);

-- Existing rows are compatibility heads. This is metadata-only: no measurement
-- rows are copied or rewritten by the migration.
INSERT INTO calibration_product_aliases
    (logical_name, head_calibration_version_id, head_generation_no, updated_by_pipeline_run_id)
SELECT
    cv.logical_name,
    cv.calibration_version_id,
    COALESCE(cv.generation_no, 1),
    cv.pipeline_run_id
FROM calibration_versions AS cv
WHERE cv.logical_name IS NOT NULL
ON CONFLICT (logical_name) DO NOTHING;
