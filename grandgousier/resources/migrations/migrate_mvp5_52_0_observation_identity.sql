-- MVP5.52.0: path-independent physical observation identity foundation.
--
-- This migration is intentionally non-destructive:
--   * existing frames.frame_id / frames.frame_uid values are unchanged;
--   * frames.observation_id is nullable;
--   * no frame rows are merged, deleted or rewritten;
--   * population/backfill is performed explicitly by the CLI after a read-only plan.

CREATE TABLE IF NOT EXISTS observations (
    observation_id BIGSERIAL PRIMARY KEY,
    observation_uid VARCHAR(96) NOT NULL,
    instrument VARCHAR(64) NOT NULL,
    identity_scheme VARCHAR(64) NOT NULL,
    native_identity TEXT NOT NULL,
    camera_identity VARCHAR(96),
    observed_at TIMESTAMPTZ,
    identity_components JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_observations_observation_uid UNIQUE (observation_uid),
    CONSTRAINT uq_observations_scheme_native_identity UNIQUE (identity_scheme, native_identity)
);

CREATE INDEX IF NOT EXISTS ix_observations_instrument_observed_at
    ON observations (instrument, observed_at);

ALTER TABLE frames
    ADD COLUMN IF NOT EXISTS observation_id BIGINT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_frames_observation_id'
          AND conrelid = 'frames'::regclass
    ) THEN
        ALTER TABLE frames
            ADD CONSTRAINT fk_frames_observation_id
            FOREIGN KEY (observation_id)
            REFERENCES observations(observation_id)
            ON DELETE SET NULL;
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS ix_frames_observation_id
    ON frames (observation_id);
