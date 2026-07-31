-- MVP5: source detections table for image-driven detection branch.

CREATE TABLE IF NOT EXISTS source_detections (
    source_detection_id BIGSERIAL PRIMARY KEY,
    frame_id BIGINT NOT NULL REFERENCES frames(frame_id) ON DELETE CASCADE,

    x DOUBLE PRECISION NOT NULL,
    y DOUBLE PRECISION NOT NULL,
    ra_deg DOUBLE PRECISION,
    dec_deg DOUBLE PRECISION,

    peak_adu DOUBLE PRECISION,
    flux DOUBLE PRECISION,
    flux_err DOUBLE PRECISION,
    local_background DOUBLE PRECISION,
    snr DOUBLE PRECISION,
    npix INTEGER,

    detection_status VARCHAR(32) NOT NULL DEFAULT 'DETECTED',
    algorithm VARCHAR(64) NOT NULL DEFAULT 'numpy_localmax_v1',
    parameters JSONB NOT NULL DEFAULT '{}'::jsonb,
    flags JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_source_detections_frame ON source_detections(frame_id);
CREATE INDEX IF NOT EXISTS ix_source_detections_status ON source_detections(detection_status);
CREATE INDEX IF NOT EXISTS ix_source_detections_sky ON source_detections(ra_deg, dec_deg);
CREATE INDEX IF NOT EXISTS ix_source_detections_snr ON source_detections(snr);
