-- MVP6.4.3: exact historical SCIENCE flag overrides.
--
-- Normalized reconstruction is authoritative for science scalars.  Historical
-- wide science_measurements rows may contain legacy top-level flag semantics
-- that differ from the current reconstruction.  Preserve only the exact delta:
-- one calibration-wide common patch plus sparse per-BASE residual patches.

CREATE TABLE IF NOT EXISTS science_flag_override_sets (
    calibration_version_id BIGINT PRIMARY KEY
        REFERENCES calibration_versions(calibration_version_id) ON DELETE CASCADE,
    source_row_count BIGINT NOT NULL,
    row_override_count BIGINT NOT NULL DEFAULT 0,
    common_override_flags JSONB NOT NULL DEFAULT '{}'::jsonb,
    common_removed_keys JSONB NOT NULL DEFAULT '[]'::jsonb,
    algorithm_version VARCHAR(48) NOT NULL DEFAULT 'TOPLEVEL_EXACT_V1',
    status VARCHAR(32) NOT NULL DEFAULT 'COMPLETE_EXACT',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS science_flag_overrides (
    calibration_version_id BIGINT NOT NULL
        REFERENCES science_flag_override_sets(calibration_version_id) ON DELETE CASCADE,
    base_measurement_id BIGINT NOT NULL
        REFERENCES measurements(measurement_id) ON DELETE CASCADE,
    override_flags JSONB NOT NULL DEFAULT '{}'::jsonb,
    removed_keys JSONB NOT NULL DEFAULT '[]'::jsonb,
    PRIMARY KEY (calibration_version_id, base_measurement_id)
);
