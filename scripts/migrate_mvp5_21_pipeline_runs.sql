CREATE TABLE IF NOT EXISTS pipeline_runs (
    pipeline_run_id BIGSERIAL PRIMARY KEY,
    run_uid VARCHAR(160) NOT NULL UNIQUE,
    instrument_unit_id BIGINT REFERENCES instrument_units(instrument_unit_id) ON DELETE SET NULL,
    catalog_name TEXT,
    calibration_prefix TEXT,
    recipe_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(32) NOT NULL DEFAULT 'RUNNING',
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    summary_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_pipeline_runs_instrument_unit_id
    ON pipeline_runs(instrument_unit_id);
CREATE INDEX IF NOT EXISTS ix_pipeline_runs_catalog_name
    ON pipeline_runs(catalog_name);
CREATE INDEX IF NOT EXISTS ix_pipeline_runs_status
    ON pipeline_runs(status);

CREATE TABLE IF NOT EXISTS pipeline_run_steps (
    pipeline_run_step_id BIGSERIAL PRIMARY KEY,
    pipeline_run_id BIGINT NOT NULL REFERENCES pipeline_runs(pipeline_run_id) ON DELETE CASCADE,
    order_index INTEGER NOT NULL DEFAULT 0,
    step_name VARCHAR(128) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'RUNNING',
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    result_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (pipeline_run_id, step_name)
);

CREATE INDEX IF NOT EXISTS ix_pipeline_run_steps_pipeline_run_id
    ON pipeline_run_steps(pipeline_run_id);
CREATE INDEX IF NOT EXISTS ix_pipeline_run_steps_status
    ON pipeline_run_steps(status);
