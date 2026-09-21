BEGIN;

-- MVP5.30.5: explicit derived-calibration provenance.
-- Older Grandgousier versions stored source calibration provenance only in
-- calibration_versions.parameters.  New code uses this FK column and still keeps
-- the JSON keys for compatibility with exported diagnostics and older patches.

ALTER TABLE calibration_versions
    ADD COLUMN IF NOT EXISTS source_calibration_version_id BIGINT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'calibration_versions_source_calibration_version_id_fkey'
    ) THEN
        ALTER TABLE calibration_versions
            ADD CONSTRAINT calibration_versions_source_calibration_version_id_fkey
            FOREIGN KEY (source_calibration_version_id)
            REFERENCES calibration_versions(calibration_version_id)
            ON DELETE SET NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_calibration_versions_source
    ON calibration_versions(source_calibration_version_id);

-- Backfill the FK from legacy JSON provenance when possible.
UPDATE calibration_versions AS target
SET source_calibration_version_id = source.calibration_version_id
FROM calibration_versions AS source
WHERE target.source_calibration_version_id IS NULL
  AND target.parameters ? 'source_calibration_name'
  AND target.parameters->>'source_calibration_name' = source.name
  AND target.calibration_version_id <> source.calibration_version_id;

-- Backfill JSON source_calibration_version_id from the new FK for consistency.
UPDATE calibration_versions AS target
SET parameters = jsonb_set(
    COALESCE(target.parameters, '{}'::jsonb),
    '{source_calibration_version_id}',
    to_jsonb(target.source_calibration_version_id),
    true
)
WHERE target.source_calibration_version_id IS NOT NULL
  AND NOT (COALESCE(target.parameters, '{}'::jsonb) ? 'source_calibration_version_id');

COMMIT;
