-- MVP3 catalog tables for Grandgousier.
-- Use this when you do not want to reset the development database.

CREATE TABLE IF NOT EXISTS catalog_sources (
    catalog_source_id BIGSERIAL PRIMARY KEY,
    catalog_name VARCHAR(64) NOT NULL,
    source_id VARCHAR(128) NOT NULL,
    source_uid VARCHAR(192) NOT NULL UNIQUE,
    ra_deg DOUBLE PRECISION NOT NULL,
    dec_deg DOUBLE PRECISION NOT NULL,
    epoch DOUBLE PRECISION,
    pmra_masyr DOUBLE PRECISION,
    pmdec_masyr DOUBLE PRECISION,
    ref_mags JSONB NOT NULL DEFAULT '{}'::jsonb,
    colors JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_flags JSONB NOT NULL DEFAULT '{}'::jsonb,
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
    CONSTRAINT uq_catalog_sources_catalog_source UNIQUE (catalog_name, source_id)
);

CREATE INDEX IF NOT EXISTS ix_catalog_sources_catalog ON catalog_sources (catalog_name);
CREATE INDEX IF NOT EXISTS ix_catalog_sources_sky ON catalog_sources (ra_deg, dec_deg);

CREATE TABLE IF NOT EXISTS frame_catalog_links (
    frame_catalog_link_id BIGSERIAL PRIMARY KEY,
    frame_id BIGINT NOT NULL REFERENCES frames(frame_id) ON DELETE CASCADE,
    catalog_source_id BIGINT NOT NULL REFERENCES catalog_sources(catalog_source_id) ON DELETE CASCADE,
    x DOUBLE PRECISION,
    y DOUBLE PRECISION,
    in_footprint BOOLEAN NOT NULL DEFAULT false,
    margin_pix DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    match_status VARCHAR(32) NOT NULL DEFAULT 'WCS_PROJECTED',
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
    CONSTRAINT uq_frame_catalog_links_frame_source UNIQUE (frame_id, catalog_source_id)
);

CREATE INDEX IF NOT EXISTS ix_frame_catalog_links_frame ON frame_catalog_links (frame_id);
CREATE INDEX IF NOT EXISTS ix_frame_catalog_links_source ON frame_catalog_links (catalog_source_id);
CREATE INDEX IF NOT EXISTS ix_frame_catalog_links_status ON frame_catalog_links (match_status);
