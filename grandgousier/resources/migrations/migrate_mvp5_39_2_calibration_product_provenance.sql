-- MVP5.39.2: typed calibration/science product provenance foundation.
--
-- This migration is additive and idempotent. Existing calibration measurement
-- layers remain compatibility-mutable until the later base+final photometry
-- cutover.

ALTER TABLE calibration_versions
    ADD COLUMN IF NOT EXISTS pipeline_run_id BIGINT,
    ADD COLUMN IF NOT EXISTS field_id BIGINT,
    ADD COLUMN IF NOT EXISTS series_id BIGINT,
    ADD COLUMN IF NOT EXISTS supersedes_calibration_version_id BIGINT,
    ADD COLUMN IF NOT EXISTS product_kind VARCHAR(48) NOT NULL DEFAULT 'LEGACY_UNCLASSIFIED',
    ADD COLUMN IF NOT EXISTS product_role VARCHAR(32) NOT NULL DEFAULT 'INTERMEDIATE',
    ADD COLUMN IF NOT EXISTS materialization_kind VARCHAR(48) NOT NULL DEFAULT 'FULL_MEASUREMENTS',
    ADD COLUMN IF NOT EXISTS lifecycle_status VARCHAR(32) NOT NULL DEFAULT 'LEGACY_MUTABLE',
    ADD COLUMN IF NOT EXISTS definition_fingerprint_sha256 VARCHAR(64),
    ADD COLUMN IF NOT EXISTS input_fingerprint_sha256 VARCHAR(64),
    ADD COLUMN IF NOT EXISTS provenance_fingerprint_sha256 VARCHAR(64),
    ADD COLUMN IF NOT EXISTS materialized_at TIMESTAMPTZ;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_calibration_versions_pipeline_run') THEN
        ALTER TABLE calibration_versions
            ADD CONSTRAINT fk_calibration_versions_pipeline_run
            FOREIGN KEY (pipeline_run_id) REFERENCES pipeline_runs(pipeline_run_id) ON DELETE SET NULL;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_calibration_versions_field') THEN
        ALTER TABLE calibration_versions
            ADD CONSTRAINT fk_calibration_versions_field
            FOREIGN KEY (field_id) REFERENCES fields(field_id) ON DELETE SET NULL;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_calibration_versions_series') THEN
        ALTER TABLE calibration_versions
            ADD CONSTRAINT fk_calibration_versions_series
            FOREIGN KEY (series_id) REFERENCES field_series(series_id) ON DELETE SET NULL;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_calibration_versions_supersedes') THEN
        ALTER TABLE calibration_versions
            ADD CONSTRAINT fk_calibration_versions_supersedes
            FOREIGN KEY (supersedes_calibration_version_id)
            REFERENCES calibration_versions(calibration_version_id) ON DELETE SET NULL;
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS ix_calibration_versions_pipeline_run
    ON calibration_versions (pipeline_run_id);
CREATE INDEX IF NOT EXISTS ix_calibration_versions_field_series
    ON calibration_versions (field_id, series_id);
CREATE INDEX IF NOT EXISTS ix_calibration_versions_product_kind
    ON calibration_versions (product_kind, product_role);
CREATE INDEX IF NOT EXISTS ix_calibration_versions_lifecycle
    ON calibration_versions (lifecycle_status);
CREATE INDEX IF NOT EXISTS ix_calibration_versions_supersedes
    ON calibration_versions (supersedes_calibration_version_id);
CREATE INDEX IF NOT EXISTS ix_calibration_versions_provenance_fingerprint
    ON calibration_versions (provenance_fingerprint_sha256);

-- Classify legacy rows conservatively. Scope/run fingerprints are not guessed.
UPDATE calibration_versions
SET product_kind = CASE
        WHEN model_name LIKE 'forced_aperture_%' THEN 'BASE_PHOTOMETRY'
        WHEN model_name = 'spatial_aperture_correction_v2' THEN 'APERTURE_CORRECTION'
        WHEN model_name = 'aperture_zp_calibration_v1' THEN 'APERTURE_ZP'
        WHEN model_name = 'ensemble_differential_v1' THEN 'ENSEMBLE'
        WHEN model_name = 'ensemble_preserving_reference_transform_v3' THEN 'COLOR_CALIBRATION'
        WHEN model_name = 'preliminary_frame_qc_zero_point_v1' THEN 'PRELIMINARY_ZP'
        ELSE 'LEGACY_UNCLASSIFIED'
    END,
    product_role = CASE
        WHEN model_name LIKE 'forced_aperture_%' THEN 'BASE'
        ELSE 'INTERMEDIATE'
    END,
    lifecycle_status = 'LEGACY_MUTABLE',
    materialization_kind = 'FULL_MEASUREMENTS'
WHERE lifecycle_status = 'LEGACY_MUTABLE';
