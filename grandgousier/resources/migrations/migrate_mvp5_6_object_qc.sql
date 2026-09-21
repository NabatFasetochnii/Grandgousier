CREATE TABLE IF NOT EXISTS object_qc (
    object_qc_id BIGSERIAL PRIMARY KEY,
    object_id BIGINT NOT NULL REFERENCES objects(object_id) ON DELETE CASCADE,
    calibration_version_id BIGINT REFERENCES calibration_versions(calibration_version_id) ON DELETE SET NULL,
    instrument_unit_id BIGINT REFERENCES instrument_units(instrument_unit_id) ON DELETE SET NULL,
    catalog_name VARCHAR(64),
    algorithm VARCHAR(64) NOT NULL DEFAULT 'object_qc_v1',

    nearest_neighbor_arcsec DOUBLE PRECISION,
    neighbors_within_3arcsec INTEGER NOT NULL DEFAULT 0,
    neighbors_within_5arcsec INTEGER NOT NULL DEFAULT 0,
    neighbors_within_10arcsec INTEGER NOT NULL DEFAULT 0,

    median_x DOUBLE PRECISION,
    median_y DOUBLE PRECISION,
    edge_distance_pix_min DOUBLE PRECISION,

    n_measurements INTEGER NOT NULL DEFAULT 0,
    mag_median DOUBLE PRECISION,
    mag_rms DOUBLE PRECISION,
    mag_mad DOUBLE PRECISION,
    mag_err_median DOUBLE PRECISION,

    flags JSONB NOT NULL DEFAULT '{}'::jsonb,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),

    CONSTRAINT uq_object_qc_identity UNIQUE (
        object_id,
        catalog_name,
        instrument_unit_id,
        calibration_version_id,
        algorithm
    )
);

CREATE INDEX IF NOT EXISTS ix_object_qc_object ON object_qc(object_id);
CREATE INDEX IF NOT EXISTS ix_object_qc_catalog ON object_qc(catalog_name);
CREATE INDEX IF NOT EXISTS ix_object_qc_instrument_unit ON object_qc(instrument_unit_id);
CREATE INDEX IF NOT EXISTS ix_object_qc_flags ON object_qc USING GIN(flags);
CREATE INDEX IF NOT EXISTS ix_object_qc_nn ON object_qc(nearest_neighbor_arcsec);
CREATE INDEX IF NOT EXISTS ix_object_qc_rms ON object_qc(mag_rms);
