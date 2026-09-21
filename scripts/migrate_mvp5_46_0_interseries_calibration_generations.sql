-- MVP5.46.0: immutable Stage-3/M4 inter-series calibration generations.
--
-- The pre-existing interseries_calibration_offsets table is intentionally
-- retained as a legacy compatibility surface.  M4 uses a generation-level
-- provenance model so one graph solution can reference exact immutable
-- Stage-2 SCIENCE products and preserve graph reliability/closure metadata.

CREATE TABLE IF NOT EXISTS interseries_calibration_generations (
    interseries_generation_id BIGSERIAL PRIMARY KEY,
    generation_uid VARCHAR(96) NOT NULL UNIQUE,
    fingerprint_sha256 VARCHAR(64) NOT NULL UNIQUE,
    field_id BIGINT NOT NULL REFERENCES fields(field_id) ON DELETE CASCADE,
    instrument_unit_id BIGINT REFERENCES instrument_units(instrument_unit_id) ON DELETE SET NULL,
    filter_name VARCHAR(32),
    catalog_name VARCHAR(64),
    analysis_algorithm VARCHAR(64) NOT NULL,
    sign_convention TEXT NOT NULL,
    reference_series_id BIGINT NOT NULL REFERENCES field_series(series_id) ON DELETE CASCADE,
    analysis_status VARCHAR(48) NOT NULL,
    graph_status VARCHAR(48) NOT NULL,
    reliability_status VARCHAR(48) NOT NULL,
    n_series INTEGER NOT NULL,
    n_edges INTEGER NOT NULL,
    graph_dof INTEGER NOT NULL,
    chi2 DOUBLE PRECISION NOT NULL DEFAULT 0,
    reduced_chi2 DOUBLE PRECISION,
    max_abs_standardized_residual DOUBLE PRECISION,
    min_points_per_object INTEGER NOT NULL,
    min_common_objects INTEGER NOT NULL,
    clip_sigma DOUBLE PRECISION NOT NULL,
    max_clip_iterations INTEGER NOT NULL,
    input_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    analysis_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    lifecycle_status VARCHAR(32) NOT NULL DEFAULT 'COMPLETE_IMMUTABLE',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_interseries_generations_n_series CHECK (n_series >= 2),
    CONSTRAINT ck_interseries_generations_n_edges CHECK (n_edges >= 1),
    CONSTRAINT ck_interseries_generations_graph_dof CHECK (graph_dof >= 0)
);

CREATE INDEX IF NOT EXISTS ix_interseries_generations_field_filter
    ON interseries_calibration_generations(field_id, filter_name);
CREATE INDEX IF NOT EXISTS ix_interseries_generations_catalog
    ON interseries_calibration_generations(catalog_name);
CREATE INDEX IF NOT EXISTS ix_interseries_generations_reliability
    ON interseries_calibration_generations(reliability_status);
CREATE INDEX IF NOT EXISTS ix_interseries_generations_created
    ON interseries_calibration_generations(created_at);

CREATE TABLE IF NOT EXISTS interseries_calibration_series (
    interseries_series_id BIGSERIAL PRIMARY KEY,
    interseries_generation_id BIGINT NOT NULL
        REFERENCES interseries_calibration_generations(interseries_generation_id) ON DELETE CASCADE,
    series_id BIGINT NOT NULL REFERENCES field_series(series_id) ON DELETE CASCADE,
    science_calibration_version_id BIGINT NOT NULL
        REFERENCES calibration_versions(calibration_version_id) ON DELETE CASCADE,
    offset_mag DOUBLE PRECISION NOT NULL,
    offset_err_mag DOUBLE PRECISION,
    is_reference_series BOOLEAN NOT NULL DEFAULT false,
    n_incident_edges INTEGER NOT NULL DEFAULT 0,
    n_frames INTEGER NOT NULL DEFAULT 0,
    n_science_points INTEGER NOT NULL DEFAULT 0,
    n_objects INTEGER NOT NULL DEFAULT 0,
    n_state_objects INTEGER NOT NULL DEFAULT 0,
    n_ensemble_references INTEGER NOT NULL DEFAULT 0,
    n_reference_eligible INTEGER NOT NULL DEFAULT 0,
    median_within_series_robust_sigma_mag DOUBLE PRECISION,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_interseries_calibration_series_generation_series
        UNIQUE (interseries_generation_id, series_id),
    CONSTRAINT uq_interseries_calibration_series_generation_science
        UNIQUE (interseries_generation_id, science_calibration_version_id)
);

CREATE INDEX IF NOT EXISTS ix_interseries_calibration_series_generation
    ON interseries_calibration_series(interseries_generation_id);
CREATE INDEX IF NOT EXISTS ix_interseries_calibration_series_series
    ON interseries_calibration_series(series_id);
CREATE INDEX IF NOT EXISTS ix_interseries_calibration_series_science
    ON interseries_calibration_series(science_calibration_version_id);

CREATE TABLE IF NOT EXISTS interseries_calibration_edges (
    interseries_edge_id BIGSERIAL PRIMARY KEY,
    interseries_generation_id BIGINT NOT NULL
        REFERENCES interseries_calibration_generations(interseries_generation_id) ON DELETE CASCADE,
    left_series_id BIGINT NOT NULL REFERENCES field_series(series_id) ON DELETE CASCADE,
    right_series_id BIGINT NOT NULL REFERENCES field_series(series_id) ON DELETE CASCADE,
    measured_delta_mag DOUBLE PRECISION,
    measured_err_mag DOUBLE PRECISION,
    scatter_mag DOUBLE PRECISION,
    n_initial_calibrators INTEGER NOT NULL DEFAULT 0,
    n_used_calibrators INTEGER NOT NULL DEFAULT 0,
    n_rejected_calibrators INTEGER NOT NULL DEFAULT 0,
    clip_iterations INTEGER NOT NULL DEFAULT 0,
    pair_status VARCHAR(48) NOT NULL,
    predicted_delta_mag DOUBLE PRECISION,
    closure_residual_mag DOUBLE PRECISION,
    standardized_residual DOUBLE PRECISION,
    common_reference_object_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    used_object_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    rejected_object_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_interseries_calibration_edges_series_order CHECK (left_series_id < right_series_id),
    CONSTRAINT uq_interseries_calibration_edges_generation_pair
        UNIQUE (interseries_generation_id, left_series_id, right_series_id)
);

CREATE INDEX IF NOT EXISTS ix_interseries_calibration_edges_generation
    ON interseries_calibration_edges(interseries_generation_id);
CREATE INDEX IF NOT EXISTS ix_interseries_calibration_edges_left
    ON interseries_calibration_edges(left_series_id);
CREATE INDEX IF NOT EXISTS ix_interseries_calibration_edges_right
    ON interseries_calibration_edges(right_series_id);
CREATE INDEX IF NOT EXISTS ix_interseries_calibration_edges_status
    ON interseries_calibration_edges(pair_status);
