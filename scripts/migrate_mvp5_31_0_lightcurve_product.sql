-- MVP5.31.0: science light-curve product foundation and inter-series offsets.

CREATE TABLE IF NOT EXISTS interseries_calibration_offsets (
    offset_id BIGSERIAL PRIMARY KEY,
    calibration_version_id BIGINT NOT NULL REFERENCES calibration_versions(calibration_version_id) ON DELETE CASCADE,
    series_id BIGINT REFERENCES field_series(series_id) ON DELETE CASCADE,
    instrument_unit_id BIGINT REFERENCES instrument_units(instrument_unit_id) ON DELETE SET NULL,
    filter_name VARCHAR(32),
    algorithm VARCHAR(64) NOT NULL DEFAULT 'interseries_median_v1',
    reference_label VARCHAR(128) NOT NULL DEFAULT 'global',
    offset_mag DOUBLE PRECISION NOT NULL,
    offset_err DOUBLE PRECISION,
    scatter_mag DOUBLE PRECISION,
    n_calibrators INTEGER NOT NULL DEFAULT 0,
    status VARCHAR(32) NOT NULL DEFAULT 'ACTIVE',
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_interseries_offsets_scope
ON interseries_calibration_offsets (
    calibration_version_id,
    series_id,
    algorithm,
    reference_label
);

CREATE INDEX IF NOT EXISTS ix_interseries_offsets_calibration
ON interseries_calibration_offsets (calibration_version_id);

CREATE INDEX IF NOT EXISTS ix_interseries_offsets_series
ON interseries_calibration_offsets (series_id);

CREATE INDEX IF NOT EXISTS ix_interseries_offsets_unit_filter
ON interseries_calibration_offsets (instrument_unit_id, filter_name);

CREATE INDEX IF NOT EXISTS ix_interseries_offsets_status
ON interseries_calibration_offsets (status);
