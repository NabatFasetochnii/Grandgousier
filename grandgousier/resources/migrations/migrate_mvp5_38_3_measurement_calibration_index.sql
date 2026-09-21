BEGIN;

-- Most calibration-analysis queries start from one calibration version and then
-- group/order by object and frame. The legacy unique index starts with object_id
-- and cannot efficiently serve this access path.
CREATE INDEX IF NOT EXISTS ix_measurements_calibration_object_frame
    ON measurements (calibration_version_id, object_id, frame_id);

COMMIT;

-- Refresh planner statistics after creating the new access path.
ANALYZE measurements;
