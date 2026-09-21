-- MVP6.4.0: compact rebuildable SCIENCE hot-query cache.
--
-- Authoritative final science state remains BASE_PHOTOMETRY plus normalized
-- calibration iterations/frame/object terms.  This cache stores only values
-- needed by current Stage-3 SQL hot paths.  It is deliberately not a durable
-- provenance layer and may be rebuilt or dropped without scientific loss.

CREATE TABLE IF NOT EXISTS science_photometry_cache (
    calibration_version_id BIGINT NOT NULL
        REFERENCES calibration_versions(calibration_version_id) ON DELETE CASCADE,
    base_measurement_id BIGINT NOT NULL
        REFERENCES measurements(measurement_id) ON DELETE CASCADE,
    mag_cal DOUBLE PRECISION,
    mag_err_random DOUBLE PRECISION,
    PRIMARY KEY (calibration_version_id, base_measurement_id)
);
