-- MVP5.25: persistent pipeline logging.
--
-- Add file-log path columns to pipeline_runs and a small structured log-events
-- table for run/step starts, completions, warnings, and errors. Full logs stay
-- on disk in logs/pipeline_runs/<pipeline_run_id>/.

ALTER TABLE pipeline_runs
    ADD COLUMN IF NOT EXISTS log_dir TEXT,
    ADD COLUMN IF NOT EXISTS log_file TEXT,
    ADD COLUMN IF NOT EXISTS jsonl_log_file TEXT,
    ADD COLUMN IF NOT EXISTS error_log_file TEXT,
    ADD COLUMN IF NOT EXISTS command_json_file TEXT,
    ADD COLUMN IF NOT EXISTS environment_json_file TEXT;

CREATE TABLE IF NOT EXISTS pipeline_log_events (
    pipeline_log_event_id BIGSERIAL PRIMARY KEY,
    pipeline_run_id BIGINT NOT NULL REFERENCES pipeline_runs(pipeline_run_id) ON DELETE CASCADE,
    pipeline_run_step_id BIGINT NULL REFERENCES pipeline_run_steps(pipeline_run_step_id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    level TEXT NOT NULL,
    event_type TEXT NOT NULL,
    message TEXT,
    context_json JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS ix_pipeline_log_events_run
    ON pipeline_log_events(pipeline_run_id, pipeline_log_event_id);

CREATE INDEX IF NOT EXISTS ix_pipeline_log_events_step
    ON pipeline_log_events(pipeline_run_step_id);

CREATE INDEX IF NOT EXISTS ix_pipeline_log_events_level
    ON pipeline_log_events(level);

CREATE INDEX IF NOT EXISTS ix_pipeline_log_events_event_type
    ON pipeline_log_events(event_type);
