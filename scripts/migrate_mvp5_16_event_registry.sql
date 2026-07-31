-- Grandgousier MVP5.16 generic event registry / coverage index
-- Safe to run more than once.

CREATE TABLE IF NOT EXISTS event_registry (
    event_id BIGSERIAL PRIMARY KEY,
    event_uid TEXT NOT NULL UNIQUE,
    event_type VARCHAR(64) NOT NULL,
    event_subtype VARCHAR(64),
    source_catalog VARCHAR(64),
    source_event_id TEXT,
    external_catalog VARCHAR(64),
    external_id TEXT,
    target_name TEXT,
    ra_deg DOUBLE PRECISION,
    dec_deg DOUBLE PRECISION,
    position_error_arcsec DOUBLE PRECISION,
    object_id BIGINT REFERENCES objects(object_id) ON DELETE SET NULL,
    t_start_bjd_tdb DOUBLE PRECISION,
    t_peak_bjd_tdb DOUBLE PRECISION,
    t_end_bjd_tdb DOUBLE PRECISION,
    t0_bjd_tdb DOUBLE PRECISION,
    period_days DOUBLE PRECISION,
    duration_hours DOUBLE PRECISION,
    expected_sign VARCHAR(32),
    expected_depth_ppt DOUBLE PRECISION,
    expected_amplitude_mag DOUBLE PRECISION,
    priority DOUBLE PRECISION,
    status VARCHAR(32) NOT NULL DEFAULT 'NEW',
    event_model_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_event_registry_type ON event_registry(event_type);
CREATE INDEX IF NOT EXISTS ix_event_registry_source ON event_registry(source_catalog, source_event_id);
CREATE INDEX IF NOT EXISTS ix_event_registry_external ON event_registry(external_catalog, external_id);
CREATE INDEX IF NOT EXISTS ix_event_registry_sky ON event_registry(ra_deg, dec_deg);
CREATE INDEX IF NOT EXISTS ix_event_registry_time_start ON event_registry(t_start_bjd_tdb);
CREATE INDEX IF NOT EXISTS ix_event_registry_time_peak ON event_registry(t_peak_bjd_tdb);
CREATE INDEX IF NOT EXISTS ix_event_registry_object ON event_registry(object_id);

CREATE TABLE IF NOT EXISTS event_object_links (
    event_object_link_id BIGSERIAL PRIMARY KEY,
    event_id BIGINT NOT NULL REFERENCES event_registry(event_id) ON DELETE CASCADE,
    object_id BIGINT NOT NULL REFERENCES objects(object_id) ON DELETE CASCADE,
    match_type VARCHAR(64) NOT NULL,
    separation_arcsec DOUBLE PRECISION,
    confidence DOUBLE PRECISION,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_event_object_link UNIQUE(event_id, object_id, match_type)
);

CREATE INDEX IF NOT EXISTS ix_event_object_links_event ON event_object_links(event_id);
CREATE INDEX IF NOT EXISTS ix_event_object_links_object ON event_object_links(object_id);

CREATE TABLE IF NOT EXISTS event_frame_coverage (
    event_frame_coverage_id BIGSERIAL PRIMARY KEY,
    event_id BIGINT NOT NULL REFERENCES event_registry(event_id) ON DELETE CASCADE,
    frame_id BIGINT NOT NULL REFERENCES frames(frame_id) ON DELETE CASCADE,
    object_id BIGINT REFERENCES objects(object_id) ON DELETE CASCADE,
    calibration_version_id BIGINT REFERENCES calibration_versions(calibration_version_id) ON DELETE SET NULL,
    instrument_unit_id BIGINT REFERENCES instrument_units(instrument_unit_id) ON DELETE SET NULL,
    time_relation VARCHAR(32),
    frame_time_jd_approx DOUBLE PRECISION,
    measurement_id BIGINT REFERENCES measurements(measurement_id) ON DELETE SET NULL,
    mag_cal DOUBLE PRECISION,
    mag_err DOUBLE PRECISION,
    frame_contains_position BOOLEAN,
    distance_to_frame_edge_pix DOUBLE PRECISION,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_event_frame_coverage UNIQUE(event_id, frame_id, object_id, calibration_version_id)
);

CREATE INDEX IF NOT EXISTS ix_event_frame_coverage_event ON event_frame_coverage(event_id);
CREATE INDEX IF NOT EXISTS ix_event_frame_coverage_frame ON event_frame_coverage(frame_id);
CREATE INDEX IF NOT EXISTS ix_event_frame_coverage_object ON event_frame_coverage(object_id);
CREATE INDEX IF NOT EXISTS ix_event_frame_coverage_time_relation ON event_frame_coverage(time_relation);
