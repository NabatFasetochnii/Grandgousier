BEGIN;

CREATE TABLE IF NOT EXISTS reference_photometry_catalogs (
    refcat_id BIGSERIAL PRIMARY KEY,
    catalog_name VARCHAR(64) NOT NULL UNIQUE,
    catalog_version VARCHAR(64),
    photometric_system VARCHAR(64),
    source_priority INTEGER NOT NULL DEFAULT 100,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_reference_photometry_catalogs_priority
    ON reference_photometry_catalogs (source_priority, catalog_name);

CREATE TABLE IF NOT EXISTS reference_photometry (
    refphot_id BIGSERIAL PRIMARY KEY,
    refcat_id BIGINT REFERENCES reference_photometry_catalogs(refcat_id) ON DELETE SET NULL,
    catalog_name VARCHAR(64) NOT NULL,
    catalog_source_id VARCHAR(128) NOT NULL,
    ref_uid VARCHAR(192) NOT NULL UNIQUE,

    gaia_catalog_name VARCHAR(64),
    gaia_source_id VARCHAR(128),
    gaia_catalog_source_id BIGINT REFERENCES catalog_sources(catalog_source_id) ON DELETE SET NULL,
    distance_to_gaia_arcsec DOUBLE PRECISION,

    ra_deg DOUBLE PRECISION NOT NULL,
    dec_deg DOUBLE PRECISION NOT NULL,
    epoch DOUBLE PRECISION,

    mag_g DOUBLE PRECISION,
    mag_r DOUBLE PRECISION,
    mag_i DOUBLE PRECISION,
    mag_z DOUBLE PRECISION,
    mag_y DOUBLE PRECISION,
    mag_b DOUBLE PRECISION,
    mag_v DOUBLE PRECISION,
    mag_j DOUBLE PRECISION,
    mag_h DOUBLE PRECISION,
    mag_k DOUBLE PRECISION,
    mag_bp DOUBLE PRECISION,
    mag_rp DOUBLE PRECISION,
    mag_g_gaia DOUBLE PRECISION,

    mag_errors JSONB NOT NULL DEFAULT '{}'::jsonb,
    colors JSONB NOT NULL DEFAULT '{}'::jsonb,
    quality_flags JSONB NOT NULL DEFAULT '{}'::jsonb,
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_reference_photometry_catalog_source UNIQUE (catalog_name, catalog_source_id)
);

CREATE INDEX IF NOT EXISTS ix_reference_photometry_catalog
    ON reference_photometry (catalog_name);
CREATE INDEX IF NOT EXISTS ix_reference_photometry_sky
    ON reference_photometry (ra_deg, dec_deg);
CREATE INDEX IF NOT EXISTS ix_reference_photometry_gaia
    ON reference_photometry (gaia_catalog_name, gaia_source_id);
CREATE INDEX IF NOT EXISTS ix_reference_photometry_gaia_source
    ON reference_photometry (gaia_catalog_source_id);

COMMIT;
