-- MVP5.39.7: database-native batch orchestration state.
-- CSV files remain compatibility exports; PostgreSQL becomes authoritative.

CREATE TABLE IF NOT EXISTS batch_runs (
    batch_run_id BIGSERIAL PRIMARY KEY,
    batch_uid VARCHAR(64) NOT NULL UNIQUE,
    batch_name VARCHAR(128) NOT NULL,
    root_path TEXT NOT NULL,
    script_name VARCHAR(128),
    script_version VARCHAR(32),
    plan_revision INTEGER NOT NULL DEFAULT 1,
    inventory_sha256 VARCHAR(64),
    queue_sha256 VARCHAR(64),
    config_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(32) NOT NULL DEFAULT 'PLANNED',
    planned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    last_activity_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_batch_runs_name ON batch_runs(batch_name);
CREATE INDEX IF NOT EXISTS ix_batch_runs_status ON batch_runs(status);
CREATE INDEX IF NOT EXISTS ix_batch_runs_root ON batch_runs(root_path);

CREATE TABLE IF NOT EXISTS batch_inventory_items (
    batch_inventory_item_id BIGSERIAL PRIMARY KEY,
    batch_run_id BIGINT NOT NULL REFERENCES batch_runs(batch_run_id) ON DELETE CASCADE,
    plan_revision INTEGER NOT NULL,
    job_id VARCHAR(160) NOT NULL,
    object_name TEXT,
    observation_name TEXT,
    directory_name TEXT,
    path TEXT NOT NULL,
    relative_path TEXT,
    fits_count INTEGER NOT NULL DEFAULT 0,
    classification VARCHAR(48),
    exclude_reason TEXT,
    selected BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_batch_inventory_revision_job UNIQUE(batch_run_id, plan_revision, job_id)
);

CREATE INDEX IF NOT EXISTS ix_batch_inventory_run_revision
    ON batch_inventory_items(batch_run_id, plan_revision);
CREATE INDEX IF NOT EXISTS ix_batch_inventory_selected
    ON batch_inventory_items(batch_run_id, plan_revision, selected);

CREATE TABLE IF NOT EXISTS batch_jobs (
    batch_job_id BIGSERIAL PRIMARY KEY,
    batch_run_id BIGINT NOT NULL REFERENCES batch_runs(batch_run_id) ON DELETE CASCADE,
    job_id VARCHAR(160) NOT NULL,
    object_name TEXT,
    observation_name TEXT,
    path TEXT NOT NULL,
    relative_path TEXT,
    fits_count INTEGER NOT NULL DEFAULT 0,
    queue_index INTEGER NOT NULL DEFAULT 0,
    first_seen_revision INTEGER NOT NULL DEFAULT 1,
    last_seen_revision INTEGER NOT NULL DEFAULT 1,
    active_in_plan BOOLEAN NOT NULL DEFAULT TRUE,
    current_attempt_no INTEGER NOT NULL DEFAULT 0,
    current_status VARCHAR(48) NOT NULL DEFAULT 'PENDING',
    current_health VARCHAR(48) NOT NULL DEFAULT 'PENDING',
    current_science_status VARCHAR(64),
    latest_pipeline_run_id BIGINT REFERENCES pipeline_runs(pipeline_run_id) ON DELETE SET NULL,
    exit_code INTEGER,
    catalog_name TEXT,
    catalog_sources BIGINT NOT NULL DEFAULT 0,
    pipeline_count INTEGER NOT NULL DEFAULT 0,
    total_points BIGINT NOT NULL DEFAULT 0,
    accepted_points BIGINT NOT NULL DEFAULT 0,
    elapsed_sec DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    manifest_path TEXT,
    console_log_path TEXT,
    message TEXT,
    last_started_at TIMESTAMPTZ,
    last_finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_batch_jobs_run_job UNIQUE(batch_run_id, job_id)
);

CREATE INDEX IF NOT EXISTS ix_batch_jobs_run_queue
    ON batch_jobs(batch_run_id, active_in_plan, queue_index);
CREATE INDEX IF NOT EXISTS ix_batch_jobs_status
    ON batch_jobs(batch_run_id, current_status);
CREATE INDEX IF NOT EXISTS ix_batch_jobs_object
    ON batch_jobs(batch_run_id, object_name);
CREATE INDEX IF NOT EXISTS ix_batch_jobs_pipeline_run
    ON batch_jobs(latest_pipeline_run_id);

CREATE TABLE IF NOT EXISTS batch_job_attempts (
    batch_job_attempt_id BIGSERIAL PRIMARY KEY,
    batch_job_id BIGINT NOT NULL REFERENCES batch_jobs(batch_job_id) ON DELETE CASCADE,
    attempt_no INTEGER NOT NULL,
    attempt_kind VARCHAR(32) NOT NULL DEFAULT 'EXECUTION',
    pipeline_run_id BIGINT REFERENCES pipeline_runs(pipeline_run_id) ON DELETE SET NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    exit_code INTEGER,
    status VARCHAR(48) NOT NULL DEFAULT 'RUNNING',
    health VARCHAR(48) NOT NULL DEFAULT 'RUNNING',
    science_status VARCHAR(64),
    catalog_name TEXT,
    catalog_sources BIGINT NOT NULL DEFAULT 0,
    pipeline_count INTEGER NOT NULL DEFAULT 0,
    total_points BIGINT NOT NULL DEFAULT 0,
    accepted_points BIGINT NOT NULL DEFAULT 0,
    elapsed_sec DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    manifest_path TEXT,
    console_log_path TEXT,
    message TEXT,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_batch_job_attempts_job_attempt UNIQUE(batch_job_id, attempt_no)
);

CREATE INDEX IF NOT EXISTS ix_batch_job_attempts_job
    ON batch_job_attempts(batch_job_id, attempt_no);
CREATE INDEX IF NOT EXISTS ix_batch_job_attempts_status
    ON batch_job_attempts(status);
CREATE INDEX IF NOT EXISTS ix_batch_job_attempts_pipeline_run
    ON batch_job_attempts(pipeline_run_id);

-- Batch-owned artifacts extend the B2 registry. These links make controller
-- manifests/logs traceable even when a process-archive invocation spans more
-- than one pipeline run.
ALTER TABLE artifacts
    ADD COLUMN IF NOT EXISTS batch_run_id BIGINT,
    ADD COLUMN IF NOT EXISTS batch_job_attempt_id BIGINT;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_artifacts_batch_run') THEN
        ALTER TABLE artifacts
            ADD CONSTRAINT fk_artifacts_batch_run
            FOREIGN KEY (batch_run_id) REFERENCES batch_runs(batch_run_id) ON DELETE SET NULL;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_artifacts_batch_job_attempt') THEN
        ALTER TABLE artifacts
            ADD CONSTRAINT fk_artifacts_batch_job_attempt
            FOREIGN KEY (batch_job_attempt_id) REFERENCES batch_job_attempts(batch_job_attempt_id) ON DELETE SET NULL;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_artifacts_batch_attempt_uri') THEN
        ALTER TABLE artifacts
            ADD CONSTRAINT uq_artifacts_batch_attempt_uri UNIQUE (batch_job_attempt_id, uri);
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS ix_artifacts_batch_run
    ON artifacts(batch_run_id, artifact_id);
CREATE INDEX IF NOT EXISTS ix_artifacts_batch_attempt
    ON artifacts(batch_job_attempt_id);
