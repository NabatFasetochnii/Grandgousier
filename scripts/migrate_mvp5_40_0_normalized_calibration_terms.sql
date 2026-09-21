-- MVP5.40.0: normalized calibration iterations and compact frame/object terms.
--
-- Additive dual-write foundation. Existing full intermediate measurement layers
-- remain intact until parity is verified on production datasets.

CREATE TABLE IF NOT EXISTS calibration_iterations (
    calibration_iteration_id BIGSERIAL PRIMARY KEY,
    calibration_version_id BIGINT NOT NULL REFERENCES calibration_versions(calibration_version_id) ON DELETE CASCADE,
    source_calibration_version_id BIGINT REFERENCES calibration_versions(calibration_version_id) ON DELETE SET NULL,
    pipeline_run_id BIGINT REFERENCES pipeline_runs(pipeline_run_id) ON DELETE SET NULL,
    field_id BIGINT REFERENCES fields(field_id) ON DELETE SET NULL,
    series_id BIGINT REFERENCES field_series(series_id) ON DELETE SET NULL,
    iteration_kind VARCHAR(48) NOT NULL,
    iteration_no INTEGER NOT NULL DEFAULT 1,
    algorithm VARCHAR(128) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'COMPLETE',
    convergence_status VARCHAR(48),
    convergence_reason TEXT,
    is_final BOOLEAN NOT NULL DEFAULT TRUE,
    parameters JSONB NOT NULL DEFAULT '{}'::jsonb,
    metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_calibration_iterations_version_kind_no
        UNIQUE (calibration_version_id, iteration_kind, iteration_no)
);

CREATE INDEX IF NOT EXISTS ix_calibration_iterations_version
    ON calibration_iterations (calibration_version_id, iteration_no);
CREATE INDEX IF NOT EXISTS ix_calibration_iterations_run
    ON calibration_iterations (pipeline_run_id);
CREATE INDEX IF NOT EXISTS ix_calibration_iterations_series
    ON calibration_iterations (series_id);
CREATE INDEX IF NOT EXISTS ix_calibration_iterations_kind
    ON calibration_iterations (iteration_kind, status);

CREATE TABLE IF NOT EXISTS calibration_frame_terms (
    calibration_frame_term_id BIGSERIAL PRIMARY KEY,
    calibration_iteration_id BIGINT NOT NULL REFERENCES calibration_iterations(calibration_iteration_id) ON DELETE CASCADE,
    calibration_version_id BIGINT NOT NULL REFERENCES calibration_versions(calibration_version_id) ON DELETE CASCADE,
    frame_id BIGINT NOT NULL REFERENCES frames(frame_id) ON DELETE CASCADE,
    term_kind VARCHAR(48) NOT NULL,
    model_name VARCHAR(64),
    coefficient_0 DOUBLE PRECISION,
    coefficient_x DOUBLE PRECISION,
    coefficient_y DOUBLE PRECISION,
    offset_mag DOUBLE PRECISION,
    offset_err DOUBLE PRECISION,
    scatter_mad DOUBLE PRECISION,
    scatter_rms DOUBLE PRECISION,
    n_candidates INTEGER,
    n_used INTEGER,
    n_reference INTEGER,
    accepted BOOLEAN NOT NULL DEFAULT TRUE,
    quality_status VARCHAR(48),
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_calibration_frame_terms_iteration_frame_kind
        UNIQUE (calibration_iteration_id, frame_id, term_kind)
);

CREATE INDEX IF NOT EXISTS ix_calibration_frame_terms_version_frame
    ON calibration_frame_terms (calibration_version_id, frame_id);
CREATE INDEX IF NOT EXISTS ix_calibration_frame_terms_iteration
    ON calibration_frame_terms (calibration_iteration_id);
CREATE INDEX IF NOT EXISTS ix_calibration_frame_terms_frame
    ON calibration_frame_terms (frame_id);
CREATE INDEX IF NOT EXISTS ix_calibration_frame_terms_kind
    ON calibration_frame_terms (term_kind);

CREATE TABLE IF NOT EXISTS calibration_iteration_objects (
    calibration_iteration_object_id BIGSERIAL PRIMARY KEY,
    calibration_iteration_id BIGINT NOT NULL REFERENCES calibration_iterations(calibration_iteration_id) ON DELETE CASCADE,
    calibration_version_id BIGINT NOT NULL REFERENCES calibration_versions(calibration_version_id) ON DELETE CASCADE,
    object_id BIGINT NOT NULL REFERENCES objects(object_id) ON DELETE CASCADE,
    role VARCHAR(48) NOT NULL DEFAULT 'NON_REFERENCE',
    is_reference BOOLEAN NOT NULL DEFAULT FALSE,
    requested_exclusion BOOLEAN NOT NULL DEFAULT FALSE,
    effective_exclusion BOOLEAN NOT NULL DEFAULT FALSE,
    n_measurements INTEGER,
    median_mag DOUBLE PRECISION,
    rms DOUBLE PRECISION,
    mad DOUBLE PRECISION,
    status VARCHAR(48),
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_calibration_iteration_objects_iteration_object
        UNIQUE (calibration_iteration_id, object_id)
);

CREATE INDEX IF NOT EXISTS ix_calibration_iteration_objects_version_object
    ON calibration_iteration_objects (calibration_version_id, object_id);
CREATE INDEX IF NOT EXISTS ix_calibration_iteration_objects_iteration
    ON calibration_iteration_objects (calibration_iteration_id);
CREATE INDEX IF NOT EXISTS ix_calibration_iteration_objects_object
    ON calibration_iteration_objects (object_id);
CREATE INDEX IF NOT EXISTS ix_calibration_iteration_objects_reference
    ON calibration_iteration_objects (calibration_iteration_id, is_reference);
CREATE INDEX IF NOT EXISTS ix_calibration_iteration_objects_excluded
    ON calibration_iteration_objects (calibration_iteration_id, effective_exclusion);
