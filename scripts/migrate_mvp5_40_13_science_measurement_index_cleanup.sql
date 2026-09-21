-- MVP5.40.13: prune write-amplifying science_measurements indexes.
--
-- science_measurements is an immutable, calibration-scoped science product.
-- Normal readers use either:
--   (calibration_version_id, object_id[, frame_id])
-- or
--   (calibration_version_id, frame_id[, object_id]).
--
-- The UNIQUE constraint uq_science_measurements_version_object_frame already
-- owns a btree on (calibration_version_id, object_id, frame_id), so the old
-- ix_science_measurements_calibration index was an exact duplicate.
--
-- Stand-alone object_id, frame_id and base_measurement_id indexes primarily
-- helped rare parent-row DELETE/SET NULL operations, while every science
-- materialization paid their maintenance cost for ~50k inserted rows.  The
-- archive is append/immutable in normal operation, so prefer the ingest/read
-- workload and retain one complementary frame-oriented index.

DROP INDEX IF EXISTS ix_science_measurements_calibration;
DROP INDEX IF EXISTS ix_science_measurements_object;
DROP INDEX IF EXISTS ix_science_measurements_frame;
DROP INDEX IF EXISTS ix_science_measurements_base;

CREATE INDEX IF NOT EXISTS ix_science_measurements_calibration_frame
    ON science_measurements (calibration_version_id, frame_id, object_id);
