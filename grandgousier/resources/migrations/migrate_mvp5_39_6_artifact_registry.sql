-- MVP5.39.6: immutable artifact registry and mutable publication/head aliases.
-- Additive migration; existing export/log files are not backfilled automatically.

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id BIGSERIAL PRIMARY KEY,
    artifact_uid VARCHAR(64) NOT NULL UNIQUE,
    pipeline_run_id BIGINT REFERENCES pipeline_runs(pipeline_run_id) ON DELETE SET NULL,
    pipeline_run_step_id BIGINT REFERENCES pipeline_run_steps(pipeline_run_step_id) ON DELETE SET NULL,
    calibration_version_id BIGINT REFERENCES calibration_versions(calibration_version_id) ON DELETE SET NULL,
    series_id BIGINT REFERENCES field_series(series_id) ON DELETE SET NULL,
    artifact_type VARCHAR(64) NOT NULL,
    artifact_role VARCHAR(48) NOT NULL DEFAULT 'DIAGNOSTIC',
    artifact_format VARCHAR(24),
    schema_version VARCHAR(32),
    storage_kind VARCHAR(24) NOT NULL DEFAULT 'FILE',
    uri TEXT NOT NULL,
    publication_uri TEXT,
    relative_path TEXT,
    size_bytes BIGINT,
    sha256 VARCHAR(64),
    lifecycle_status VARCHAR(32) NOT NULL DEFAULT 'ACTIVE_IMMUTABLE',
    is_immutable BOOLEAN NOT NULL DEFAULT TRUE,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_artifacts_run_uri UNIQUE (pipeline_run_id, uri)
);

CREATE INDEX IF NOT EXISTS ix_artifacts_run ON artifacts (pipeline_run_id, artifact_id);
CREATE INDEX IF NOT EXISTS ix_artifacts_step ON artifacts (pipeline_run_step_id);
CREATE INDEX IF NOT EXISTS ix_artifacts_calibration ON artifacts (calibration_version_id);
CREATE INDEX IF NOT EXISTS ix_artifacts_series ON artifacts (series_id);
CREATE INDEX IF NOT EXISTS ix_artifacts_type_role ON artifacts (artifact_type, artifact_role);
CREATE INDEX IF NOT EXISTS ix_artifacts_sha256 ON artifacts (sha256);
CREATE INDEX IF NOT EXISTS ix_artifacts_publication_uri ON artifacts (publication_uri);

CREATE TABLE IF NOT EXISTS artifact_publications (
    artifact_publication_id BIGSERIAL PRIMARY KEY,
    publication_uri TEXT NOT NULL UNIQUE,
    artifact_id BIGINT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
    updated_by_pipeline_run_id BIGINT REFERENCES pipeline_runs(pipeline_run_id) ON DELETE SET NULL,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_artifact_publications_artifact
    ON artifact_publications (artifact_id);
CREATE INDEX IF NOT EXISTS ix_artifact_publications_pipeline_run
    ON artifact_publications (updated_by_pipeline_run_id);
