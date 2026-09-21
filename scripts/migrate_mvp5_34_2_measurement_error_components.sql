BEGIN;

ALTER TABLE measurements
    ADD COLUMN IF NOT EXISTS mag_err_random double precision,
    ADD COLUMN IF NOT EXISTS mag_err_systematic double precision;

-- Existing mag_err values are random per-epoch uncertainties. Preserve that
-- interpretation explicitly without changing current downstream behaviour.
UPDATE measurements
SET mag_err_random = mag_err
WHERE mag_err_random IS NULL
  AND mag_err IS NOT NULL;

COMMENT ON COLUMN measurements.mag_err IS
    'Backward-compatible random per-epoch magnitude uncertainty.';
COMMENT ON COLUMN measurements.mag_err_random IS
    'Random per-epoch magnitude uncertainty used for time-series statistics.';
COMMENT ON COLUMN measurements.mag_err_systematic IS
    'Object-level absolute calibration uncertainty correlated across epochs.';

COMMIT;
