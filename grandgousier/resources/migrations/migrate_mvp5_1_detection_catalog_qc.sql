-- MVP5.1: detection-catalog crossmatch and frame-level QC tables.

CREATE TABLE IF NOT EXISTS detection_catalog_matches (
    detection_catalog_match_id BIGSERIAL PRIMARY KEY,
    frame_id BIGINT NOT NULL REFERENCES frames(frame_id) ON DELETE CASCADE,
    source_detection_id BIGINT NOT NULL REFERENCES source_detections(source_detection_id) ON DELETE CASCADE,
    catalog_source_id BIGINT NOT NULL REFERENCES catalog_sources(catalog_source_id) ON DELETE CASCADE,
    catalog_name VARCHAR(64) NOT NULL,
    algorithm VARCHAR(64) NOT NULL DEFAULT 'nearest_sky_v1',
    match_status VARCHAR(32) NOT NULL DEFAULT 'MATCHED',
    sep_arcsec DOUBLE PRECISION,
    dx_pix DOUBLE PRECISION,
    dy_pix DOUBLE PRECISION,
    detection_mag_inst DOUBLE PRECISION,
    catalog_mag DOUBLE PRECISION,
    zero_point DOUBLE PRECISION,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
    CONSTRAINT uq_detection_catalog_matches_detection_source_algorithm
        UNIQUE (source_detection_id, catalog_source_id, algorithm)
);

CREATE INDEX IF NOT EXISTS ix_detection_catalog_matches_frame
    ON detection_catalog_matches(frame_id);
CREATE INDEX IF NOT EXISTS ix_detection_catalog_matches_catalog
    ON detection_catalog_matches(catalog_name);
CREATE INDEX IF NOT EXISTS ix_detection_catalog_matches_sep
    ON detection_catalog_matches(sep_arcsec);
CREATE INDEX IF NOT EXISTS ix_detection_catalog_matches_zp
    ON detection_catalog_matches(zero_point);

CREATE TABLE IF NOT EXISTS frame_qc (
    frame_qc_id BIGSERIAL PRIMARY KEY,
    frame_id BIGINT NOT NULL REFERENCES frames(frame_id) ON DELETE CASCADE,
    catalog_name VARCHAR(64) NOT NULL,
    algorithm VARCHAR(64) NOT NULL DEFAULT 'detection_catalog_qc_v1',
    n_detections INTEGER NOT NULL DEFAULT 0,
    n_catalog_sources INTEGER NOT NULL DEFAULT 0,
    n_matched INTEGER NOT NULL DEFAULT 0,
    zp_n INTEGER NOT NULL DEFAULT 0,
    astrom_rms_arcsec DOUBLE PRECISION,
    dx_median_pix DOUBLE PRECISION,
    dy_median_pix DOUBLE PRECISION,
    sep_median_arcsec DOUBLE PRECISION,
    sep_p95_arcsec DOUBLE PRECISION,
    zp_median DOUBLE PRECISION,
    zp_mad DOUBLE PRECISION,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
    CONSTRAINT uq_frame_qc_frame_catalog_algorithm
        UNIQUE (frame_id, catalog_name, algorithm)
);

CREATE INDEX IF NOT EXISTS ix_frame_qc_frame
    ON frame_qc(frame_id);
CREATE INDEX IF NOT EXISTS ix_frame_qc_catalog
    ON frame_qc(catalog_name);
CREATE INDEX IF NOT EXISTS ix_frame_qc_astrom
    ON frame_qc(astrom_rms_arcsec);
CREATE INDEX IF NOT EXISTS ix_frame_qc_zp
    ON frame_qc(zp_median);
