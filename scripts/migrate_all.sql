\set ON_ERROR_STOP on

-- Grandgousier MVP5 cumulative migrations.
-- Run from the repository root with:
--   psql -U grandgousier -d grandgousier -h localhost -f scripts/migrate_all.sql
-- psql \ir paths below are relative to this file.

\ir migrate_mvp1_1_instrument_units.sql
\ir migrate_mvp3_catalogs.sql
\ir migrate_mvp5_detection_catalog.sql
\ir migrate_mvp5_1_detection_catalog_qc.sql
\ir migrate_mvp5_6_object_qc.sql
\ir migrate_mvp5_16_event_registry.sql
\ir migrate_mvp5_20_event_detection_links.sql
\ir migrate_mvp5_21_pipeline_runs.sql
\ir migrate_mvp5_24_detection_idempotency.sql
\ir migrate_mvp5_25_pipeline_logging.sql
\ir migrate_mvp5_26_fields_series.sql
\ir migrate_mvp5_28_reference_photometry.sql
\ir migrate_mvp5_30_5_calibration_provenance.sql
\ir migrate_mvp5_31_0_lightcurve_product.sql
