-- Grandgousier MVP5.20: event-detection links for transient/event registry.
-- Run after scripts/migrate_mvp5_16_event_registry.sql and MVP5 source detection migrations.

CREATE TABLE IF NOT EXISTS event_detection_links (
    event_detection_link_id BIGSERIAL PRIMARY KEY,
    event_id BIGINT NOT NULL REFERENCES event_registry(event_id) ON DELETE CASCADE,
    source_detection_id BIGINT NOT NULL REFERENCES source_detections(source_detection_id) ON DELETE CASCADE,
    frame_id BIGINT NOT NULL REFERENCES frames(frame_id) ON DELETE CASCADE,
    object_id BIGINT REFERENCES objects(object_id) ON DELETE SET NULL,
    instrument_unit_id BIGINT REFERENCES instrument_units(instrument_unit_id) ON DELETE SET NULL,

    time_relation VARCHAR(32),
    frame_time_jd_approx DOUBLE PRECISION,
    separation_arcsec DOUBLE PRECISION,

    detection_snr DOUBLE PRECISION,
    detection_flux DOUBLE PRECISION,
    peak_adu DOUBLE PRECISION,

    catalog_matched BOOLEAN NOT NULL DEFAULT false,
    nearest_catalog_name VARCHAR(64),
    nearest_catalog_source_id VARCHAR(128),
    nearest_catalog_sep_arcsec DOUBLE PRECISION,

    confidence DOUBLE PRECISION,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_event_detection_links_event_detection UNIQUE (event_id, source_detection_id)
);

CREATE INDEX IF NOT EXISTS ix_event_detection_links_event ON event_detection_links(event_id);
CREATE INDEX IF NOT EXISTS ix_event_detection_links_detection ON event_detection_links(source_detection_id);
CREATE INDEX IF NOT EXISTS ix_event_detection_links_frame ON event_detection_links(frame_id);
CREATE INDEX IF NOT EXISTS ix_event_detection_links_object ON event_detection_links(object_id);
CREATE INDEX IF NOT EXISTS ix_event_detection_links_instrument_unit ON event_detection_links(instrument_unit_id);
CREATE INDEX IF NOT EXISTS ix_event_detection_links_time_relation ON event_detection_links(time_relation);
CREATE INDEX IF NOT EXISTS ix_event_detection_links_catalog_matched ON event_detection_links(catalog_matched);
CREATE INDEX IF NOT EXISTS ix_event_detection_links_snr ON event_detection_links(detection_snr);
CREATE INDEX IF NOT EXISTS ix_event_detection_links_sep ON event_detection_links(separation_arcsec);
