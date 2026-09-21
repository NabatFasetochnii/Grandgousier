-- MVP5.40.2: dedicated final science-photometry materialization.
--
-- Additive D2 migration.  Legacy final rows in measurements remain intact during
-- the compatibility/shadow phase.  New runs can dual-write reconstructed final
-- scalar photometry here and science readers may prefer this table with fallback.

CREATE TABLE IF NOT EXISTS science_measurements (
    science_measurement_id BIGSERIAL PRIMARY KEY,
    calibration_version_id BIGINT NOT NULL REFERENCES calibration_versions(calibration_version_id) ON DELETE CASCADE,
    source_measurement_id BIGINT REFERENCES measurements(measurement_id) ON DELETE SET NULL,
    base_measurement_id BIGINT REFERENCES measurements(measurement_id) ON DELETE SET NULL,
    object_id BIGINT NOT NULL REFERENCES objects(object_id) ON DELETE CASCADE,
    frame_id BIGINT NOT NULL REFERENCES frames(frame_id) ON DELETE CASCADE,
    x DOUBLE PRECISION,
    y DOUBLE PRECISION,
    flux DOUBLE PRECISION,
    flux_err DOUBLE PRECISION,
    mag_inst DOUBLE PRECISION,
    mag_cal DOUBLE PRECISION,
    mag_err DOUBLE PRECISION,
    mag_err_random DOUBLE PRECISION,
    mag_err_systematic DOUBLE PRECISION,
    aperture_radius_pix DOUBLE PRECISION,
    local_background DOUBLE PRECISION,
    local_zp_correction DOUBLE PRECISION,
    centroid_dx_pix DOUBLE PRECISION,
    centroid_dy_pix DOUBLE PRECISION,
    flags JSONB NOT NULL DEFAULT '{}'::jsonb,
    materialization_source VARCHAR(48) NOT NULL DEFAULT 'RECONSTRUCTED_NORMALIZED',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_science_measurements_version_object_frame
        UNIQUE (calibration_version_id, object_id, frame_id)
);

CREATE INDEX IF NOT EXISTS ix_science_measurements_calibration
    ON science_measurements (calibration_version_id, object_id, frame_id);
CREATE INDEX IF NOT EXISTS ix_science_measurements_object
    ON science_measurements (object_id);
CREATE INDEX IF NOT EXISTS ix_science_measurements_frame
    ON science_measurements (frame_id);
CREATE INDEX IF NOT EXISTS ix_science_measurements_base
    ON science_measurements (base_measurement_id);
