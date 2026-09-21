-- MVP5.39.1: managed schema migration ledger.
--
-- Existing pre-ledger databases should normally use:
--   python -m grandgousier.cli migrate-db --adopt-legacy
-- The command validates the legacy schema before recording historical entries.

CREATE TABLE IF NOT EXISTS schema_migrations (
    migration_id VARCHAR(128) PRIMARY KEY,
    filename TEXT NOT NULL UNIQUE,
    checksum_sha256 VARCHAR(64) NOT NULL,
    state VARCHAR(24) NOT NULL DEFAULT 'APPLIED',
    application_version VARCHAR(32),
    execution_ms DOUBLE PRECISION,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
