-- Optional non-destructive MVP1.1 schema migration.
-- For development it is simpler to run:
--   python -m grandgousier.cli reset-db --yes
-- This script is only for keeping existing indexed rows.

CREATE TABLE IF NOT EXISTS instrument_units (
    instrument_unit_id bigserial PRIMARY KEY,
    unit_uid varchar(96) NOT NULL UNIQUE,
    instrument varchar(64) NOT NULL,
    site varchar(64),
    tube varchar(64),
    camera varchar(64),
    filter_name varchar(32),
    observing_mode varchar(64),
    profile_name varchar(64),
    profile_version varchar(32),
    pixel_scale_arcsec double precision,
    saturation_level_adu double precision,
    nominal_gain_e_per_adu double precision,
    nominal_read_noise_e double precision,
    config_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    active boolean NOT NULL DEFAULT true,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    CONSTRAINT uq_instrument_units_identity UNIQUE (
        instrument, site, tube, camera, filter_name, observing_mode
    )
);

ALTER TABLE frames ADD COLUMN IF NOT EXISTS instrument_unit_id bigint;
CREATE INDEX IF NOT EXISTS ix_frames_instrument_unit_id ON frames (instrument_unit_id);
CREATE INDEX IF NOT EXISTS ix_instrument_units_identity
    ON instrument_units (instrument, site, tube, camera, filter_name);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'frames_instrument_unit_id_fkey'
    ) THEN
        ALTER TABLE frames
            ADD CONSTRAINT frames_instrument_unit_id_fkey
            FOREIGN KEY (instrument_unit_id)
            REFERENCES instrument_units(instrument_unit_id)
            ON DELETE SET NULL;
    END IF;
END $$;
