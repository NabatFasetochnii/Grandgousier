-- MVP5.26: persistent sky fields and observing series.

CREATE TABLE IF NOT EXISTS fields (
    field_id BIGSERIAL PRIMARY KEY,
    field_uid VARCHAR(96) NOT NULL UNIQUE,
    field_name TEXT,
    primary_object_name TEXT,
    ra_center_deg DOUBLE PRECISION NOT NULL,
    dec_center_deg DOUBLE PRECISION NOT NULL,
    radius_arcmin DOUBLE PRECISION NOT NULL DEFAULT 30.0,
    reference_catalog_name VARCHAR(64),
    status VARCHAR(32) NOT NULL DEFAULT 'READY',
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_fields_sky ON fields (ra_center_deg, dec_center_deg);
CREATE INDEX IF NOT EXISTS ix_fields_status ON fields (status);
CREATE INDEX IF NOT EXISTS ix_fields_name ON fields (field_name);

CREATE TABLE IF NOT EXISTS field_series (
    series_id BIGSERIAL PRIMARY KEY,
    field_id BIGINT NOT NULL REFERENCES fields(field_id) ON DELETE CASCADE,
    series_uid VARCHAR(96) NOT NULL UNIQUE,
    instrument_unit_id BIGINT REFERENCES instrument_units(instrument_unit_id) ON DELETE SET NULL,
    filter_name VARCHAR(32),
    time_start TIMESTAMPTZ,
    time_end TIMESTAMPTZ,
    mjd_start DOUBLE PRECISION,
    mjd_end DOUBLE PRECISION,
    ra_center_deg DOUBLE PRECISION,
    dec_center_deg DOUBLE PRECISION,
    n_frames INTEGER NOT NULL DEFAULT 0,
    grouping_method VARCHAR(64) NOT NULL DEFAULT 'center_time_v1',
    status VARCHAR(32) NOT NULL DEFAULT 'READY',
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_field_series_field ON field_series (field_id);
CREATE INDEX IF NOT EXISTS ix_field_series_instrument_unit ON field_series (instrument_unit_id);
CREATE INDEX IF NOT EXISTS ix_field_series_status ON field_series (status);
CREATE INDEX IF NOT EXISTS ix_field_series_time ON field_series (time_start, time_end);
CREATE INDEX IF NOT EXISTS ix_field_series_sky ON field_series (ra_center_deg, dec_center_deg);

CREATE TABLE IF NOT EXISTS field_series_frames (
    series_frame_id BIGSERIAL PRIMARY KEY,
    series_id BIGINT NOT NULL REFERENCES field_series(series_id) ON DELETE CASCADE,
    frame_id BIGINT NOT NULL REFERENCES frames(frame_id) ON DELETE CASCADE,
    order_index INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (series_id, frame_id),
    UNIQUE (frame_id)
);

CREATE INDEX IF NOT EXISTS ix_field_series_frames_series ON field_series_frames (series_id, order_index);
CREATE INDEX IF NOT EXISTS ix_field_series_frames_frame ON field_series_frames (frame_id);

ALTER TABLE pipeline_runs
    ADD COLUMN IF NOT EXISTS field_id BIGINT REFERENCES fields(field_id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS series_id BIGINT REFERENCES field_series(series_id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS ix_pipeline_runs_field ON pipeline_runs (field_id);
CREATE INDEX IF NOT EXISTS ix_pipeline_runs_series ON pipeline_runs (series_id);
