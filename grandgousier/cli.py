from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from pathlib import Path

from grandgousier.db.init_db import init_db, reset_db
from grandgousier.db.session import make_session_factory
from grandgousier.instruments.base import InstrumentProfile
from grandgousier.io.archive_indexer import index_archive
from grandgousier.astrometry.wcs_check import WcsStatus, check_frames_wcs
from grandgousier.logging_config import configure_logging
from grandgousier.settings import get_settings

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="grandgousier",
        description="Grandgousier archive photometry engine",
    )
    parser.add_argument("--database-url", default=None, help="SQLAlchemy PostgreSQL URL")
    parser.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING, ERROR")

    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init-db", help="Create PostgreSQL tables")
    p_init.set_defaults(func=cmd_init_db)

    p_reset = sub.add_parser("reset-db", help="Drop and recreate all Grandgousier tables")
    p_reset.add_argument("--yes", action="store_true", help="Confirm destructive reset")
    p_reset.set_defaults(func=cmd_reset_db)

    p_index = sub.add_parser("index-archive", help="Index FITS archive into frames table")
    p_index.add_argument("--root", required=True, help="Root directory with raw FITS files")
    p_index.add_argument("--instrument", required=True, help="Instrument YAML profile")
    p_index.add_argument("--pattern", default="*.fits", help="Glob pattern, default: *.fits")
    p_index.add_argument("--recursive", action="store_true", help="Search recursively")
    p_index.add_argument("--dry-run", action="store_true", help="Do not write to DB")
    p_index.add_argument("--limit", type=int, default=None, help="Limit number of discovered files")
    p_index.set_defaults(func=cmd_index_archive)


    p_wcs = sub.add_parser("check-wcs", help="Validate header WCS for indexed FITS frames")
    p_wcs.add_argument("--frame-id", type=int, action="append", help="Specific frame_id; may be repeated")
    p_wcs.add_argument("--instrument", default=None, help="Filter by instrument name, e.g. MASTER or RoboPhot")
    p_wcs.add_argument("--instrument-unit-id", type=int, default=None, help="Filter by instrument_units.instrument_unit_id")
    p_wcs.add_argument(
        "--status",
        default=WcsStatus.NOT_CHECKED.value,
        help="Only check frames with this current wcs_status; use --all-statuses to ignore",
    )
    p_wcs.add_argument("--all-statuses", action="store_true", help="Ignore current wcs_status and re-check all selected frames")
    p_wcs.add_argument("--limit", type=int, default=None, help="Maximum number of frames to check")
    p_wcs.add_argument("--dry-run", action="store_true", help="Check WCS without writing results to DB")
    p_wcs.add_argument(
        "--keep-indexed-center",
        action="store_true",
        help="Do not replace ra_center_deg/dec_center_deg with WCS image-center coordinates",
    )
    p_wcs.add_argument("--record-events", action="store_true", help="Write processing_events for non-OK WCS results")
    p_wcs.set_defaults(func=cmd_check_wcs)

    p_show = sub.add_parser("show-frames", help="Print indexed frames without opening psql")
    p_show.add_argument("--limit", type=int, default=20, help="Maximum number of rows to print")
    p_show.add_argument("--offset", type=int, default=0, help="Number of rows to skip")
    p_show.add_argument("--instrument", default=None, help="Filter by instrument name")
    p_show.add_argument("--instrument-unit-id", type=int, default=None, help="Filter by instrument unit id")
    p_show.add_argument("--filter", dest="filter_name", default=None, help="Filter by filter_name")
    p_show.add_argument("--object", dest="object_name", default=None, help="Substring filter for object_name, case-insensitive")
    p_show.add_argument("--wcs-status", default=None, help="Filter by wcs_status")
    p_show.add_argument("--processing-status", default=None, help="Filter by processing_status value, e.g. HEADER_READ")
    p_show.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_show.set_defaults(func=cmd_show_frames)

    p_frame_summary = sub.add_parser("frame-summary", help="Print frame counts by instrument unit and statuses")
    p_frame_summary.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_frame_summary.set_defaults(func=cmd_frame_summary)


    p_build_series = sub.add_parser("build-field-series", help="Group indexed frames into persistent fields and observing series")
    p_build_series.add_argument("--instrument-unit-id", type=int, default=None, help="Restrict frames to one instrument unit")
    p_build_series.add_argument("--max-field-separation-arcmin", type=float, default=30.0, help="Maximum center separation for same field")
    p_build_series.add_argument("--max-series-gap-minutes", type=float, default=60.0, help="Start new series after this time gap")
    p_build_series.add_argument("--min-frames-per-series", type=int, default=1, help="Drop groups smaller than this")
    p_build_series.add_argument("--include-assigned", dest="only_unassigned", action="store_false", help="Reassign frames already present in field_series_frames")
    p_build_series.set_defaults(only_unassigned=True)
    p_build_series.add_argument("--include-non-science", dest="only_science", action="store_false", help="Include non-science frames")
    p_build_series.set_defaults(only_science=True)
    p_build_series.add_argument("--dry-run", action="store_true", help="Show proposed fields/series without writing")
    p_build_series.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_build_series.set_defaults(func=cmd_build_field_series)

    p_show_fields = sub.add_parser("show-fields", help="Show persistent sky fields")
    p_show_fields.add_argument("--limit", type=int, default=50, help="Maximum rows")
    p_show_fields.add_argument("--status", default=None, help="Filter by field status")
    p_show_fields.add_argument("--name", default=None, help="Substring filter for field_name")
    p_show_fields.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_show_fields.set_defaults(func=cmd_show_fields)

    p_show_series = sub.add_parser("show-field-series", help="Show observing series for fields")
    p_show_series.add_argument("--field-id", type=int, default=None, help="Filter by field_id")
    p_show_series.add_argument("--series-id", type=int, default=None, help="Show one series")
    p_show_series.add_argument("--instrument-unit-id", type=int, default=None, help="Filter by instrument unit")
    p_show_series.add_argument("--status", default=None, help="Filter by series status")
    p_show_series.add_argument("--limit", type=int, default=100, help="Maximum rows")
    p_show_series.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_show_series.set_defaults(func=cmd_show_field_series)

    p_show_series_frames = sub.add_parser("show-series-frames", help="Show frames assigned to one observing series")
    p_show_series_frames.add_argument("--series-id", type=int, required=True, help="field_series.series_id")
    p_show_series_frames.add_argument("--limit", type=int, default=200, help="Maximum rows")
    p_show_series_frames.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_show_series_frames.set_defaults(func=cmd_show_series_frames)

    p_import_catalog = sub.add_parser("import-catalog", help="Import a local external catalog into catalog_sources")
    p_import_catalog.add_argument("--file", required=True, help="Catalog file: CSV/TSV/TXT/FITS/ECSV supported")
    p_import_catalog.add_argument("--catalog-name", required=True, help="Catalog namespace, e.g. gaia_test or panstarrs")
    p_import_catalog.add_argument("--ra-col", default="ra", help="RA column in degrees, default: ra")
    p_import_catalog.add_argument("--dec-col", default="dec", help="Dec column in degrees, default: dec")
    p_import_catalog.add_argument("--source-id-col", default=None, help="Stable source-id column; if omitted, generated from coordinates")
    p_import_catalog.add_argument("--pmra-col", default=None, help="Proper motion RA column, mas/yr")
    p_import_catalog.add_argument("--pmdec-col", default=None, help="Proper motion Dec column, mas/yr")
    p_import_catalog.add_argument("--epoch-col", default=None, help="Epoch column, e.g. 2016.0")
    p_import_catalog.add_argument(
        "--mag-col",
        action="append",
        default=[],
        help="Reference magnitude mapping name=column; may be repeated, e.g. --mag-col G=phot_g_mean_mag",
    )
    p_import_catalog.add_argument(
        "--color-col",
        action="append",
        default=[],
        help="Color mapping name=column; may be repeated, e.g. --color-col BP_RP=bp_rp",
    )
    p_import_catalog.add_argument("--limit", type=int, default=None, help="Limit imported rows for testing")
    p_import_catalog.add_argument("--dry-run", action="store_true", help="Read and validate without writing to DB")
    p_import_catalog.set_defaults(func=cmd_import_catalog)

    p_catalog_summary = sub.add_parser("catalog-summary", help="Print imported catalog counts")
    p_catalog_summary.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_catalog_summary.set_defaults(func=cmd_catalog_summary)

    p_show_catalog = sub.add_parser("show-catalog-sources", help="Print imported catalog sources")
    p_show_catalog.add_argument("--catalog-name", default=None, help="Filter by catalog_name")
    p_show_catalog.add_argument("--limit", type=int, default=20, help="Maximum rows")
    p_show_catalog.add_argument("--offset", type=int, default=0, help="Rows to skip")
    p_show_catalog.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_show_catalog.set_defaults(func=cmd_show_catalog_sources)

    p_import_refphot = sub.add_parser("import-reference-photometry", help="Import external reference photometry into a separate photometric layer")
    p_import_refphot.add_argument("--file", required=True, help="Reference photometry file: CSV/TSV/TXT/FITS/ECSV supported")
    p_import_refphot.add_argument("--catalog-name", required=True, help="Reference catalog namespace, e.g. panstarrs_dr2")
    p_import_refphot.add_argument("--catalog-version", default=None, help="Optional reference catalog version label")
    p_import_refphot.add_argument("--photometric-system", default=None, help="Photometric system label, e.g. ps1_ab, sdss_ab, apass_vega")
    p_import_refphot.add_argument("--source-priority", type=int, default=100, help="Lower value means preferred reference source")
    p_import_refphot.add_argument("--ra-col", default="ra", help="RA column in degrees")
    p_import_refphot.add_argument("--dec-col", default="dec", help="Dec column in degrees")
    p_import_refphot.add_argument("--source-id-col", default=None, help="Stable source-id column; if omitted, generated from coordinates")
    p_import_refphot.add_argument("--epoch-col", default=None, help="Optional epoch column")
    p_import_refphot.add_argument("--mag-col", action="append", default=[], help="Magnitude mapping band=column; e.g. --mag-col i=iMeanPSFMag")
    p_import_refphot.add_argument("--mag-err-col", action="append", default=[], help="Magnitude error mapping name=column; may be repeated")
    p_import_refphot.add_argument("--color-col", action="append", default=[], help="Color mapping name=column; may be repeated")
    p_import_refphot.add_argument("--quality-col", action="append", default=[], help="Quality mapping name=column; stored in quality_flags")
    p_import_refphot.add_argument("--match-to-catalog", default=None, help="Identity catalog to crossmatch against, e.g. gaia_dr3_master")
    p_import_refphot.add_argument("--max-match-arcsec", type=float, default=1.0, help="Maximum reference-to-identity match radius")
    p_import_refphot.add_argument("--limit", type=int, default=None, help="Limit imported rows for testing")
    p_import_refphot.add_argument("--dry-run", action="store_true", help="Read, validate and match without writing")
    p_import_refphot.set_defaults(func=cmd_import_reference_photometry)

    p_create_refphot_csv = sub.add_parser(
        "create-reference-photometry-csv",
        help="Fetch external reference photometry for a field/series/cone and write a local CSV",
    )
    p_create_refphot_csv.add_argument("--provider", default="panstarrs_dr2", help="Reference provider; currently panstarrs_dr2")
    p_create_refphot_csv.add_argument("--field-id", type=int, default=None, help="Use field center and radius")
    p_create_refphot_csv.add_argument("--series-id", type=int, default=None, help="Use series center and parent field radius")
    p_create_refphot_csv.add_argument("--ra", type=float, default=None, help="Manual RA center in degrees")
    p_create_refphot_csv.add_argument("--dec", type=float, default=None, help="Manual Dec center in degrees")
    p_create_refphot_csv.add_argument("--radius-arcmin", type=float, default=None, help="Manual or override cone radius in arcmin")
    p_create_refphot_csv.add_argument("--out", required=True, help="Output CSV path")
    p_create_refphot_csv.add_argument("--overwrite", action="store_true", help="Replace existing output CSV")
    p_create_refphot_csv.add_argument("--require-band", action="append", default=[], help="Keep only rows with this band; may be repeated or comma-separated, e.g. --require-band i --require-band r")
    p_create_refphot_csv.add_argument("--min-mag", action="append", default=[], help="Minimum magnitude by band, band=value; e.g. --min-mag i=10")
    p_create_refphot_csv.add_argument("--max-mag", action="append", default=[], help="Maximum magnitude by band, band=value; e.g. --max-mag i=19")
    p_create_refphot_csv.add_argument("--max-mag-err", action="append", default=[], help="Maximum magnitude error by band, band=value; e.g. --max-mag-err i=0.1")
    p_create_refphot_csv.add_argument("--min-detections", type=int, default=None, help="Keep only reference sources with at least this many detections")
    p_create_refphot_csv.add_argument("--no-remote-filters", action="store_true", help="Do not push supported filters into the remote provider query; apply them only after download")
    p_create_refphot_csv.add_argument("--limit", type=int, default=None, help="Limit written rows after filtering for testing")
    p_create_refphot_csv.add_argument("--print-import-command", action="store_true", help="Print import-reference-photometry command for the created CSV")
    p_create_refphot_csv.set_defaults(func=cmd_create_reference_photometry_csv)

    p_refphot_catalogs = sub.add_parser("show-reference-photometry-catalogs", help="Show imported reference photometry catalogs")
    p_refphot_catalogs.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_refphot_catalogs.set_defaults(func=cmd_show_reference_photometry_catalogs)

    p_show_refphot = sub.add_parser("show-object-reference-photometry", help="Show all reference photometry matched to one identity source/object")
    p_show_refphot.add_argument("--object-id", type=int, default=None, help="Grandgousier object_id")
    p_show_refphot.add_argument("--identity-catalog-name", default=None, help="Identity catalog, e.g. gaia_dr3_master")
    p_show_refphot.add_argument("--source-id", default=None, help="Identity source_id")
    p_show_refphot.add_argument("--limit", type=int, default=50, help="Maximum rows")
    p_show_refphot.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_show_refphot.set_defaults(func=cmd_show_object_reference_photometry)

    p_diag_refphot = sub.add_parser("diagnose-reference-photometry", help="Summarize reference photometry coverage for a field")
    p_diag_refphot.add_argument("--field-id", type=int, default=None, help="Field id; if omitted, uses all field-linked sources")
    p_diag_refphot.add_argument("--identity-catalog-name", default="gaia_dr3_master", help="Identity catalog used by field photometry")
    p_diag_refphot.add_argument("--reference-catalog-name", default=None, help="Restrict to one reference photometry catalog")
    p_diag_refphot.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_diag_refphot.set_defaults(func=cmd_diagnose_reference_photometry)

    p_color_cal = sub.add_parser(
        "compute-color-calibration",
        help="Diagnose color-aware photometric calibration against reference photometry",
    )
    p_color_cal.add_argument("--series-id", type=int, default=None, help="Restrict to one field_series.series_id")
    p_color_cal.add_argument("--calibration-name", required=True, help="Measurement CalibrationVersion to diagnose")
    p_color_cal.add_argument("--identity-catalog-name", default="gaia_dr3_master", help="Identity catalog used in measurement flags")
    p_color_cal.add_argument("--reference-catalog-name", required=True, help="Reference photometry catalog, e.g. panstarrs_dr2")
    p_color_cal.add_argument("--reference-band", required=True, help="Reference magnitude band, e.g. i")
    p_color_cal.add_argument("--color-index", required=True, help="Color index, e.g. r-i or bp-rp")
    p_color_cal.add_argument("--out-dir", default=None, help="Directory for color_calibration_summary.json and CSV diagnostics")
    p_color_cal.add_argument("--min-measurements-per-object", type=int, default=5, help="Minimum measurements per object")
    p_color_cal.add_argument("--min-stars-per-frame", type=int, default=20, help="Minimum reference stars per frame for frame ZP")
    p_color_cal.add_argument("--max-measurement-mag-err", type=float, default=None, help="Reject measurements with mag_err above this value")
    p_color_cal.add_argument("--max-reference-mag-err", type=float, default=None, help="Reject reference stars with reference-band mag error above this value")
    p_color_cal.add_argument("--min-reference-mag", default=None, help="Reject reference stars brighter than this reference-band magnitude; accepts value or band=value")
    p_color_cal.add_argument("--max-reference-mag", default=None, help="Reject reference stars fainter than this reference-band magnitude; accepts value or band=value")
    p_color_cal.add_argument("--min-color", default=None, help="Reject stars below this color index; accepts value or color=value, e.g. r-i=-0.2")
    p_color_cal.add_argument("--max-color", default=None, help="Reject stars above this color index; accepts value or color=value, e.g. r-i=1.5")
    p_color_cal.add_argument("--include-flagged-calibrators", action="store_true", help="Do not reject stars with bad object_qc flags")
    p_color_cal.add_argument("--exclude-object-id", type=int, action="append", default=[], help="Explicit object_id to exclude from calibrators; may be repeated")
    p_color_cal.add_argument("--exclude-object-ids", default="", help="Comma-separated object_id list to exclude from calibrators")
    p_color_cal.add_argument("--clip-sigma", type=float, default=4.0, help="Robust clipping threshold for frame/color model")
    p_color_cal.add_argument("--n-iterations", type=int, default=5, help="Maximum frame/color model iterations")
    p_color_cal.add_argument("--include-saturated", action="store_true", help="Do not reject saturated_or_near_saturated measurement flags")
    p_color_cal.add_argument("--limit", type=int, default=None, help="Limit loaded measurements for debugging")
    p_color_cal.add_argument("--format", choices=("table", "json"), default="table", help="Output format")
    p_color_cal.set_defaults(func=cmd_compute_color_calibration)

    p_apply_color = sub.add_parser(
        "apply-color-calibration",
        help="Create a new reference-band color-calibrated CalibrationVersion",
    )
    p_apply_color.add_argument("--series-id", type=int, default=None, help="Restrict to one field_series.series_id")
    p_apply_color.add_argument("--source-calibration-name", required=True, help="Source measurement CalibrationVersion")
    p_apply_color.add_argument("--target-calibration-name", required=True, help="New/updated target CalibrationVersion name")
    p_apply_color.add_argument("--identity-catalog-name", default="gaia_dr3_master", help="Identity catalog used in measurement flags")
    p_apply_color.add_argument("--reference-catalog-name", required=True, help="Reference photometry catalog, e.g. panstarrs_dr2")
    p_apply_color.add_argument("--reference-band", required=True, help="Reference magnitude band, e.g. i")
    p_apply_color.add_argument("--color-index", required=True, help="Color index, e.g. r-i or bp-rp")
    p_apply_color.add_argument("--out-dir", default=None, help="Directory for diagnostic summary/CSV used by the fit")
    p_apply_color.add_argument("--min-measurements-per-object", type=int, default=5, help="Minimum measurements per calibrator object")
    p_apply_color.add_argument("--min-stars-per-frame", type=int, default=20, help="Minimum calibrator stars per frame for frame ZP")
    p_apply_color.add_argument("--max-measurement-mag-err", type=float, default=None, help="Reject calibrator measurements with mag_err above this value")
    p_apply_color.add_argument("--max-reference-mag-err", type=float, default=None, help="Reject reference stars with reference-band mag error above this value")
    p_apply_color.add_argument("--min-reference-mag", default=None, help="Reject calibrators brighter than this reference-band magnitude; accepts value or band=value")
    p_apply_color.add_argument("--max-reference-mag", default=None, help="Reject calibrators fainter than this reference-band magnitude; accepts value or band=value")
    p_apply_color.add_argument("--min-color", default=None, help="Reject calibrators below this color index; accepts value or color=value")
    p_apply_color.add_argument("--max-color", default=None, help="Reject calibrators above this color index; accepts value or color=value")
    p_apply_color.add_argument("--include-flagged-calibrators", action="store_true", help="Do not reject calibrators with bad object_qc flags")
    p_apply_color.add_argument("--exclude-object-id", type=int, action="append", default=[], help="Explicit object_id to exclude from calibrators; may be repeated")
    p_apply_color.add_argument("--exclude-object-ids", default="", help="Comma-separated object_id list to exclude from calibrators")
    p_apply_color.add_argument("--clip-sigma", type=float, default=4.0, help="Robust clipping threshold for frame/color model")
    p_apply_color.add_argument("--n-iterations", type=int, default=5, help="Maximum frame/color model iterations")
    p_apply_color.add_argument("--include-saturated", action="store_true", help="Do not reject saturated_or_near_saturated measurement flags")
    p_apply_color.add_argument("--limit", type=int, default=None, help="Limit loaded measurements for debugging")
    p_apply_color.add_argument("--dry-run", action="store_true", help="Fit model and count rows without writing target calibration")
    p_apply_color.add_argument("--format", choices=("table", "json"), default="table", help="Output format")
    p_apply_color.set_defaults(func=cmd_apply_color_calibration)

    p_match_catalog = sub.add_parser("match-catalog-to-frames", help="Project imported catalog sources into WCS frames")
    p_match_catalog.add_argument("--catalog-name", required=True, help="catalog_sources.catalog_name to project")
    p_match_catalog.add_argument("--frame-id", type=int, action="append", help="Specific frame_id; may be repeated")
    p_match_catalog.add_argument("--instrument", default=None, help="Filter frames by instrument")
    p_match_catalog.add_argument("--instrument-unit-id", type=int, default=None, help="Filter frames by instrument unit")
    p_match_catalog.add_argument("--all-wcs-statuses", action="store_true", help="Do not require wcs_status=HEADER_WCS_OK")
    p_match_catalog.add_argument("--margin-pix", type=float, default=0.0, help="Accepted footprint margin in pixels")
    p_match_catalog.add_argument("--limit-frames", type=int, default=None, help="Limit selected frames")
    p_match_catalog.add_argument("--limit-sources", type=int, default=None, help="Limit selected catalog sources")
    p_match_catalog.add_argument("--dry-run", action="store_true", help="Project sources without writing frame_catalog_links")
    p_match_catalog.set_defaults(func=cmd_match_catalog_to_frames)

    p_phot = sub.add_parser("run-forced-photometry", help="Run MVP circular-aperture forced photometry")
    p_phot.add_argument("--catalog-name", default=None, help="Only use frame_catalog_links from this catalog")
    p_phot.add_argument("--frame-id", type=int, action="append", help="Specific frame_id; may be repeated")
    p_phot.add_argument("--instrument", default=None, help="Filter frames by instrument")
    p_phot.add_argument("--instrument-unit-id", type=int, default=None, help="Filter frames by instrument unit")
    p_phot.add_argument("--all-wcs-statuses", action="store_true", help="Do not require wcs_status=HEADER_WCS_OK")
    p_phot.add_argument("--aperture-radius-pix", type=float, default=3.0, help="Circular aperture radius in pixels")
    p_phot.add_argument("--annulus-inner-pix", type=float, default=8.0, help="Background annulus inner radius in pixels")
    p_phot.add_argument("--annulus-outer-pix", type=float, default=12.0, help="Background annulus outer radius in pixels")
    p_phot.add_argument("--zero-point", type=float, default=0.0, help="Instrumental magnitude additive zero point")
    p_phot.add_argument("--saturation-level-adu", type=float, default=None, help="Override saturation threshold")
    p_phot.add_argument("--calibration-name", default=None, help="CalibrationVersion name; default generated from aperture settings")
    p_phot.add_argument("--limit-frames", type=int, default=None, help="Limit selected frames")
    p_phot.add_argument("--limit-links", type=int, default=None, help="Limit catalog links per frame")
    p_phot.add_argument("--update-processing-status", action="store_true", help="Set frames.processing_status=PHOTOMETRY_DONE")
    p_phot.add_argument("--bulk-insert-batch-size", type=int, default=1000, help="Rows per batched measurement INSERT/UPSERT")
    p_phot.add_argument("--dry-run", action="store_true", help="Measure without writing measurements")
    p_phot.set_defaults(func=cmd_run_forced_photometry)

    p_meas_summary = sub.add_parser("measurement-summary", help="Print measurement counts")
    p_meas_summary.add_argument("--calibration-name", default=None, help="Filter by CalibrationVersion name")
    p_meas_summary.add_argument("--instrument-unit-id", type=int, default=None, help="Filter by instrument unit")
    p_meas_summary.add_argument("--limit", type=int, default=None, help="Maximum rows")
    p_meas_summary.add_argument("--offset", type=int, default=0, help="Rows to skip")
    p_meas_summary.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_meas_summary.set_defaults(func=cmd_measurement_summary)

    p_show_meas = sub.add_parser("show-measurements", help="Print measurements")
    p_show_meas.add_argument("--limit", type=int, default=20, help="Maximum rows")
    p_show_meas.add_argument("--offset", type=int, default=0, help="Rows to skip")
    p_show_meas.add_argument("--catalog-name", default=None, help="Filter by catalog source namespace in flags")
    p_show_meas.add_argument("--calibration-name", default=None, help="Filter by CalibrationVersion name")
    p_show_meas.add_argument("--instrument", default=None, help="Filter by frame instrument")
    p_show_meas.add_argument("--instrument-unit-id", type=int, default=None, help="Filter by instrument unit")
    p_show_meas.add_argument("--object", dest="object_name", default=None, help="Substring filter for frame object_name")
    p_show_meas.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_show_meas.set_defaults(func=cmd_show_measurements)

    p_cal = sub.add_parser("calibrate-measurements", help="Apply preliminary per-frame zero points from frame_qc to measurements")
    p_cal.add_argument("--source-calibration-name", default="raw_aperture_r3_ann8_12_v1", help="Existing raw Measurement calibration name")
    p_cal.add_argument("--target-calibration-name", default="preliminary_gaia_g_zp_v1", help="New CalibrationVersion name to write")
    p_cal.add_argument("--catalog-name", required=True, help="frame_qc catalog_name used for zero points")
    p_cal.add_argument("--source-catalog-name", default=None, help="Only calibrate measurements from this source catalog; default = --catalog-name")
    p_cal.add_argument("--instrument-unit-id", type=int, default=None, help="Filter by instrument unit")
    p_cal.add_argument("--frame-id", type=int, action="append", help="Specific frame_id; may be repeated")
    p_cal.add_argument("--qc-algorithm", default="detection_catalog_qc_v1", help="frame_qc.algorithm to use")
    p_cal.add_argument("--min-zp-n", type=int, default=10, help="Minimum number of matched stars used for frame zero point")
    p_cal.add_argument("--max-zp-mad", type=float, default=None, help="Reject frames with zp_mad above this value")
    p_cal.add_argument("--min-n-matched", type=int, default=None, help="Reject frames with n_matched below this value")
    p_cal.add_argument("--bulk-insert-batch-size", type=int, default=1000, help="Rows per batched measurement INSERT/UPSERT")
    p_cal.add_argument("--dry-run", action="store_true", help="Compute without writing calibrated measurements")
    p_cal.set_defaults(func=cmd_calibrate_measurements)

    p_ap_zp = sub.add_parser("aperture-zp-calibrate", help="Calibrate forced-aperture measurements using aperture-derived per-frame zero points")
    p_ap_zp.add_argument("--source-calibration-name", required=True, help="Source CalibrationVersion name, usually raw_aperture_*")
    p_ap_zp.add_argument("--target-calibration-name", required=True, help="Target CalibrationVersion name")
    p_ap_zp.add_argument("--catalog-name", required=True, help="Catalog namespace used in measurement flags")
    p_ap_zp.add_argument("--instrument-unit-id", type=int, default=None, help="Filter by instrument unit")
    p_ap_zp.add_argument("--mag-key", default="G", help="Reference magnitude key in objects.ref_mags")
    p_ap_zp.add_argument("--min-ref-stars-per-frame", type=int, default=20, help="Minimum reference stars after clipping per frame")
    p_ap_zp.add_argument("--max-mag-err", type=float, default=0.08, help="Maximum source measurement mag_err to use for ZP; use negative to disable")
    p_ap_zp.add_argument("--max-zp-mad", type=float, default=None, help="Reject frame ZP if MAD exceeds this")
    p_ap_zp.add_argument("--clip-sigma", type=float, default=3.0, help="Sigma clipping threshold for frame ZP")
    p_ap_zp.add_argument("--n-iterations", type=int, default=3, help="Number of sigma clipping iterations")
    p_ap_zp.add_argument("--include-saturated", action="store_true", help="Allow saturated_or_near_saturated measurements in ZP reference set")
    p_ap_zp.add_argument("--bulk-insert-batch-size", type=int, default=1000, help="Rows per batched measurement INSERT/UPSERT")
    p_ap_zp.add_argument("--dry-run", action="store_true", help="Compute but do not write calibrated measurements")
    p_ap_zp.set_defaults(func=cmd_aperture_zp_calibrate)

    p_ens = sub.add_parser("ensemble-calibrate", help="Apply robust ensemble differential calibration to measurements")
    p_ens.add_argument("--source-calibration-name", default="preliminary_gaia_g_zp_v1", help="Existing Measurement calibration name")
    p_ens.add_argument("--target-calibration-name", default="ensemble_gaia_g_v1", help="New CalibrationVersion name to write")
    p_ens.add_argument("--catalog-name", default=None, help="Only calibrate measurements from this catalog namespace")
    p_ens.add_argument("--instrument-unit-id", type=int, default=None, help="Filter by instrument unit")
    p_ens.add_argument("--frame-id", type=int, action="append", help="Specific frame_id; may be repeated")
    p_ens.add_argument("--min-measurements", type=int, default=5, help="Minimum measurements for object statistics")
    p_ens.add_argument("--min-ref-measurements", type=int, default=5, help="Minimum measurements for reference-star candidates")
    p_ens.add_argument("--min-ref-stars-per-frame", type=int, default=20, help="Minimum reference stars needed to compute a frame offset")
    p_ens.add_argument("--max-ref-rms", type=float, default=0.08, help="Maximum raw/reference RMS for reference candidates; use negative to disable")
    p_ens.add_argument("--max-ref-mad", type=float, default=0.05, help="Maximum raw/reference MAD for reference candidates; use negative to disable")
    p_ens.add_argument("--min-mag", type=float, default=None, help="Minimum median magnitude for reference candidates")
    p_ens.add_argument("--max-mag", type=float, default=None, help="Maximum median magnitude for reference candidates")
    p_ens.add_argument("--n-iterations", type=int, default=2, help="Number of ensemble offset iterations")
    p_ens.add_argument("--use-mag-inst", action="store_true", help="Prefer mag_inst instead of mag_cal as input")
    p_ens.add_argument("--bulk-insert-batch-size", type=int, default=1000, help="Rows per batched measurement INSERT/UPSERT")
    p_ens.add_argument("--dry-run", action="store_true", help="Compute without writing calibrated measurements")
    p_ens.set_defaults(func=cmd_ensemble_calibrate)

    p_ens_summary = sub.add_parser("ensemble-frame-summary", help="Print per-frame ensemble offsets from an ensemble calibration")
    p_ens_summary.add_argument("--calibration-name", default="ensemble_gaia_g_v1", help="Ensemble CalibrationVersion name")
    p_ens_summary.add_argument("--catalog-name", default=None, help="Filter by catalog namespace in measurement flags")
    p_ens_summary.add_argument("--instrument-unit-id", type=int, default=None, help="Filter by instrument unit")
    p_ens_summary.add_argument("--limit", type=int, default=50, help="Maximum rows")
    p_ens_summary.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_ens_summary.set_defaults(func=cmd_ensemble_frame_summary)

    p_obj_summary = sub.add_parser("object-summary", help="Print per-object light-curve summary statistics")
    p_obj_summary.add_argument("--calibration-name", default=None, help="Filter by CalibrationVersion name")
    p_obj_summary.add_argument("--catalog-name", default=None, help="Filter by catalog source namespace in measurement flags")
    p_obj_summary.add_argument("--source-id", default=None, help="Filter by external source_id in measurement flags")
    p_obj_summary.add_argument("--instrument-unit-id", type=int, default=None, help="Filter by instrument unit")
    p_obj_summary.add_argument("--limit", type=int, default=50, help="Maximum rows")
    p_obj_summary.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_obj_summary.set_defaults(func=cmd_object_summary)

    p_rms = sub.add_parser("rms-mag-summary", help="Print RMS--magnitude quality-control summary")
    p_rms.add_argument("--calibration-name", required=True, help="CalibrationVersion name")
    p_rms.add_argument("--catalog-name", default=None, help="Filter by catalog source namespace")
    p_rms.add_argument("--instrument-unit-id", type=int, default=None, help="Filter by instrument unit")
    p_rms.add_argument("--min-measurements", type=int, default=5, help="Minimum measurements per object")
    p_rms.add_argument("--bin-width", type=float, default=0.5, help="Magnitude bin width")
    p_rms.add_argument("--min-objects-per-bin", type=int, default=3, help="Minimum objects per magnitude bin")
    p_rms.add_argument("--mag-key", default="G", help="Reference magnitude key from objects.ref_mags")
    p_rms.add_argument("--color-key", default="BP_RP", help="Color key from objects.colors")
    p_rms.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_rms.set_defaults(func=cmd_rms_mag_summary)

    p_export_rms = sub.add_parser("export-rms-mag-plot", help="Export an RMS--magnitude diagnostic plot")
    p_export_rms.add_argument("--out", required=True, help="Output plot path, e.g. exports/rms_mag.png")
    p_export_rms.add_argument("--calibration-name", required=True, help="CalibrationVersion name")
    p_export_rms.add_argument("--catalog-name", default=None, help="Filter by catalog source namespace")
    p_export_rms.add_argument("--instrument-unit-id", type=int, default=None, help="Filter by instrument unit")
    p_export_rms.add_argument("--min-measurements", type=int, default=5, help="Minimum measurements per object")
    p_export_rms.add_argument("--bin-width", type=float, default=0.5, help="Magnitude bin width for binned median")
    p_export_rms.add_argument("--min-objects-per-bin", type=int, default=3, help="Minimum objects per magnitude bin")
    p_export_rms.add_argument("--y", choices=("mag_rms", "mag_mad"), default="mag_rms", help="Scatter metric to plot")
    p_export_rms.add_argument("--ylim-max", type=float, default=None, help="Optional upper y-axis limit")
    p_export_rms.add_argument("--title", default=None, help="Plot title")
    p_export_rms.add_argument("--dpi", type=int, default=150, help="Output DPI")
    p_export_rms.add_argument("--no-binned", action="store_true", help="Do not draw binned median curve")
    p_export_rms.add_argument("--exclude-flagged", action="store_true", help="Exclude objects with any object_qc flag")
    p_export_rms.add_argument("--exclude-crowded", action="store_true", help="Exclude CROWDED / BLENDED_NEIGHBOR objects from object_qc")
    p_export_rms.add_argument("--exclude-edge", action="store_true", help="Exclude EDGE objects from object_qc")
    p_export_rms.add_argument("--exclude-saturated", action="store_true", help="Exclude SATURATED_LIKE objects from object_qc")
    p_export_rms.add_argument("--min-nearest-neighbor-arcsec", type=float, default=None, help="Require object_qc nearest-neighbor separation")
    p_export_rms.set_defaults(func=cmd_export_rms_mag_plot)

    p_candidates = sub.add_parser("variable-candidate-summary", help="Rank objects by RMS excess above local RMS--magnitude baseline")
    p_candidates.add_argument("--calibration-name", required=True, help="CalibrationVersion name")
    p_candidates.add_argument("--catalog-name", default=None, help="Filter by catalog source namespace")
    p_candidates.add_argument("--instrument-unit-id", type=int, default=None, help="Filter by instrument unit")
    p_candidates.add_argument("--min-measurements", type=int, default=5, help="Minimum measurements per object")
    p_candidates.add_argument("--bin-width", type=float, default=0.5, help="Magnitude bin width")
    p_candidates.add_argument("--min-objects-per-bin", type=int, default=3, help="Minimum objects per magnitude bin")
    p_candidates.add_argument("--min-rms", type=float, default=None, help="Optional absolute RMS threshold")
    p_candidates.add_argument("--min-excess", type=float, default=2.0, help="Minimum RMS / local median RMS")
    p_candidates.add_argument("--exclude-flagged", action="store_true", help="Exclude objects with any object_qc flag")
    p_candidates.add_argument("--exclude-crowded", action="store_true", help="Exclude CROWDED / BLENDED_NEIGHBOR objects")
    p_candidates.add_argument("--exclude-edge", action="store_true", help="Exclude EDGE objects")
    p_candidates.add_argument("--exclude-saturated", action="store_true", help="Exclude SATURATED_LIKE objects")
    p_candidates.add_argument("--min-nearest-neighbor-arcsec", type=float, default=None, help="Require object_qc nearest-neighbor separation")
    p_candidates.add_argument("--max-mag-median", type=float, default=None, help="Reject objects fainter than this median magnitude")
    p_candidates.add_argument("--require-detection-support", action="store_true", help="Reject objects without adequate source-detection support from object_qc")
    p_candidates.add_argument("--max-ref-mag-delta", type=float, default=None, help="Reject objects with |mag_median - ref_mag| above this value")
    p_candidates.add_argument("--exclude-catalog-outliers", action="store_true", help="Reject LARGE_CATALOG_MAG_RESIDUAL objects from object_qc")
    p_candidates.add_argument("--exclude-aperture-unstable", action="store_true", help="Reject APERTURE_UNSTABLE / APERTURE_MEDIAN_SHIFT objects from object_qc")
    p_candidates.add_argument("--limit", type=int, default=50, help="Maximum rows")
    p_candidates.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_candidates.set_defaults(func=cmd_variable_candidate_summary)

    p_triage = sub.add_parser("candidate-triage-summary", help="Show variable candidates with explicit rejection reasons")
    p_triage.add_argument("--calibration-name", required=True, help="CalibrationVersion name")
    p_triage.add_argument("--catalog-name", default=None, help="Filter by catalog source namespace")
    p_triage.add_argument("--instrument-unit-id", type=int, default=None, help="Filter by instrument unit")
    p_triage.add_argument("--min-measurements", type=int, default=5, help="Minimum measurements per object")
    p_triage.add_argument("--bin-width", type=float, default=0.5, help="Magnitude bin width")
    p_triage.add_argument("--min-objects-per-bin", type=int, default=3, help="Minimum objects per magnitude bin")
    p_triage.add_argument("--min-rms", type=float, default=None, help="Optional absolute RMS threshold")
    p_triage.add_argument("--min-excess", type=float, default=2.0, help="Minimum RMS / local median RMS")
    p_triage.add_argument("--disable-intrinsic-variability-candidates", dest="enable_intrinsic_variability_candidates", action="store_false", help="Disable the intrinsic variability candidate channel based on scatter relative to formal errors")
    p_triage.set_defaults(enable_intrinsic_variability_candidates=True)
    p_triage.add_argument("--min-intrinsic-rms", type=float, default=0.03, help="Minimum RMS for intrinsic-variability candidates; use negative to disable")
    p_triage.add_argument("--min-rms-over-mag-err", type=float, default=4.0, help="Minimum mag_rms/effective_mag_err for intrinsic-variability candidates; use negative to disable")
    p_triage.add_argument("--min-mad-over-mag-err", type=float, default=2.5, help="Minimum mag_mad/effective_mag_err for intrinsic-variability candidates; use negative to disable")
    p_triage.add_argument("--intrinsic-mag-err-floor", type=float, default=0.01, help="Systematic floor for intrinsic-variability candidate significance; use negative to disable")
    p_triage.add_argument("--min-robust-peak-to-peak", type=float, default=0.08, help="Minimum P90-P10 robust amplitude for intrinsic-variability candidates; use negative to disable")
    p_triage.add_argument("--disable-low-amplitude-intrinsic-candidates", dest="enable_low_amplitude_intrinsic_candidates", action="store_false", help="Disable low-amplitude intrinsic-variability candidate channel")
    p_triage.set_defaults(enable_low_amplitude_intrinsic_candidates=True)
    p_triage.add_argument("--low-amplitude-min-measurements", type=int, default=30, help="Minimum measurements for low-amplitude intrinsic candidates")
    p_triage.add_argument("--low-amplitude-min-intrinsic-rms", type=float, default=0.03, help="Minimum RMS for low-amplitude intrinsic candidates; use negative to disable")
    p_triage.add_argument("--low-amplitude-min-rms-over-mag-err", type=float, default=2.3, help="Minimum mag_rms/effective_mag_err for low-amplitude intrinsic candidates; use negative to disable")
    p_triage.add_argument("--low-amplitude-min-mad-over-mag-err", type=float, default=2.0, help="Minimum mag_mad/effective_mag_err for low-amplitude intrinsic candidates; use negative to disable")
    p_triage.add_argument("--low-amplitude-min-robust-peak-to-peak", type=float, default=0.08, help="Minimum P90-P10 amplitude for low-amplitude intrinsic candidates; use negative to disable")
    p_triage.add_argument("--exclude-flagged", action="store_true", help="Reject objects with any object_qc flag")
    p_triage.add_argument("--exclude-crowded", action="store_true", default=True, help="Reject CROWDED / BLENDED_NEIGHBOR objects")
    p_triage.add_argument("--include-crowded", dest="exclude_crowded", action="store_false", help="Do not reject crowded/blended objects")
    p_triage.add_argument("--exclude-edge", action="store_true", default=True, help="Reject EDGE objects")
    p_triage.add_argument("--include-edge", dest="exclude_edge", action="store_false", help="Do not reject EDGE objects")
    p_triage.add_argument("--exclude-saturated", action="store_true", default=True, help="Reject SATURATED_LIKE objects")
    p_triage.add_argument("--include-saturated", dest="exclude_saturated", action="store_false", help="Do not reject saturated-like objects")
    p_triage.add_argument("--exclude-catalog-outliers", action="store_true", default=True, help="Reject LARGE_CATALOG_MAG_RESIDUAL objects")
    p_triage.add_argument("--include-catalog-outliers", dest="exclude_catalog_outliers", action="store_false", help="Do not reject catalog-magnitude outliers")
    p_triage.add_argument("--reject-aperture-unstable", action="store_true", default=True, help="Reject APERTURE_UNSTABLE / APERTURE_MEDIAN_SHIFT objects")
    p_triage.add_argument("--include-aperture-unstable", dest="reject_aperture_unstable", action="store_false", help="Do not reject aperture-unstable objects")
    p_triage.add_argument("--require-detection-support", action="store_true", default=True, help="Reject weak/no detection-support objects")
    p_triage.add_argument("--allow-weak-detection-support", dest="require_detection_support", action="store_false", help="Do not reject weak/no detection-support objects")
    p_triage.add_argument("--min-nearest-neighbor-arcsec", type=float, default=None, help="Require minimum nearest-neighbor separation")
    p_triage.add_argument("--max-mag-median", type=float, default=None, help="Reject objects fainter than this median magnitude")
    p_triage.add_argument("--max-ref-mag-delta", type=float, default=None, help="Reject objects with |mag_median - ref_mag| above this value")
    p_triage.add_argument("--qc-algorithm", default="object_qc_v1", help="object_qc.algorithm")
    p_triage.add_argument("--limit", type=int, default=50, help="Maximum displayed rows")
    p_triage.add_argument("--out", default=None, help="Optional CSV path for full triage table before display limit")
    p_triage.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_triage.set_defaults(func=cmd_candidate_triage_summary)

    p_blend_diag = sub.add_parser("blend-diagnostic-summary", help="Show dynamic-separation and blend diagnostics from object_qc")
    p_blend_diag.add_argument("--calibration-name", required=True, help="CalibrationVersion name")
    p_blend_diag.add_argument("--catalog-name", default=None, help="Filter by catalog source namespace")
    p_blend_diag.add_argument("--instrument-unit-id", type=int, default=None, help="Filter by instrument unit")
    p_blend_diag.add_argument("--qc-algorithm", default="object_qc_v1", help="object_qc.algorithm")
    p_blend_diag.add_argument("--only-flagged", action="store_true", help="Only show objects with at least one object_qc flag")
    p_blend_diag.add_argument("--only-possible-blends", action="store_true", help="Only show dynamic/aperture blend-risk objects")
    p_blend_diag.add_argument("--max-separation-index", type=float, default=None, help="Only show objects with separation_index <= this value")
    p_blend_diag.add_argument("--min-mag-rms", type=float, default=None, help="Only show objects with mag_rms >= this value")
    p_blend_diag.add_argument("--sort", choices=("risk", "separation_index", "mag_rms", "source_id"), default="risk", help="Sort mode")
    p_blend_diag.add_argument("--limit", type=int, default=50, help="Maximum displayed rows")
    p_blend_diag.add_argument("--out", default=None, help="Optional CSV output path for full filtered table")
    p_blend_diag.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_blend_diag.set_defaults(func=cmd_blend_diagnostic_summary)

    p_field_report = sub.add_parser("field-qc-report", help="Build one compact field/series QC report")
    p_field_report.add_argument("--calibration-name", required=True, help="CalibrationVersion name")
    p_field_report.add_argument("--catalog-name", default=None, help="Filter by catalog source namespace")
    p_field_report.add_argument("--instrument-unit-id", type=int, default=None, help="Filter by instrument unit")
    p_field_report.add_argument("--series-id", type=int, default=None, help="Restrict report to one field_series.series_id")
    p_field_report.add_argument("--field-series-name", default=None, help="Restrict report to one field_series identifier: series_id, series_uid, field_name, pipeline/calibration prefix, or derived calibration prefix")
    p_field_report.add_argument("--frame-qc-algorithm", default="detection_catalog_qc_v1", help="frame_qc.algorithm")
    p_field_report.add_argument("--object-qc-algorithm", default="object_qc_v1", help="object_qc.algorithm")
    p_field_report.add_argument("--min-measurements", type=int, default=10, help="Minimum measurements per object for scatter/triage")
    p_field_report.add_argument("--bin-width", type=float, default=0.5, help="Magnitude bin width for RMS baseline")
    p_field_report.add_argument("--min-objects-per-bin", type=int, default=3, help="Minimum objects per RMS bin")
    p_field_report.add_argument("--min-rms", type=float, default=None, help="Optional absolute RMS threshold for raw candidates")
    p_field_report.add_argument("--min-excess", type=float, default=2.0, help="Minimum RMS / local median RMS")
    p_field_report.add_argument("--disable-intrinsic-variability-candidates", dest="enable_intrinsic_variability_candidates", action="store_false", help="Disable intrinsic-variability candidate channel")
    p_field_report.set_defaults(enable_intrinsic_variability_candidates=True)
    p_field_report.add_argument("--min-intrinsic-rms", type=float, default=0.03, help="Minimum RMS for intrinsic-variability candidates; use negative to disable")
    p_field_report.add_argument("--min-rms-over-mag-err", type=float, default=4.0, help="Minimum mag_rms/effective_mag_err for intrinsic-variability candidates; use negative to disable")
    p_field_report.add_argument("--min-mad-over-mag-err", type=float, default=2.5, help="Minimum mag_mad/effective_mag_err for intrinsic-variability candidates; use negative to disable")
    p_field_report.add_argument("--intrinsic-mag-err-floor", type=float, default=0.01, help="Systematic floor for intrinsic-variability candidate significance; use negative to disable")
    p_field_report.add_argument("--min-robust-peak-to-peak", type=float, default=0.08, help="Minimum P90-P10 robust amplitude for intrinsic-variability candidates; use negative to disable")
    p_field_report.add_argument("--disable-low-amplitude-intrinsic-candidates", dest="enable_low_amplitude_intrinsic_candidates", action="store_false", help="Disable low-amplitude intrinsic-variability candidate channel")
    p_field_report.set_defaults(enable_low_amplitude_intrinsic_candidates=True)
    p_field_report.add_argument("--low-amplitude-min-measurements", type=int, default=30, help="Minimum measurements for low-amplitude intrinsic candidates")
    p_field_report.add_argument("--low-amplitude-min-intrinsic-rms", type=float, default=0.03, help="Minimum RMS for low-amplitude intrinsic candidates; use negative to disable")
    p_field_report.add_argument("--low-amplitude-min-rms-over-mag-err", type=float, default=2.3, help="Minimum mag_rms/effective_mag_err for low-amplitude intrinsic candidates; use negative to disable")
    p_field_report.add_argument("--low-amplitude-min-mad-over-mag-err", type=float, default=2.0, help="Minimum mag_mad/effective_mag_err for low-amplitude intrinsic candidates; use negative to disable")
    p_field_report.add_argument("--low-amplitude-min-robust-peak-to-peak", type=float, default=0.08, help="Minimum P90-P10 amplitude for low-amplitude intrinsic candidates; use negative to disable")
    p_field_report.add_argument("--exclude-flagged", action="store_true", help="Reject objects with any object_qc flag in triage")
    p_field_report.add_argument("--exclude-crowded", action="store_true", default=True, help="Reject CROWDED / BLENDED_NEIGHBOR objects in triage")
    p_field_report.add_argument("--include-crowded", dest="exclude_crowded", action="store_false", help="Do not reject crowded/blended objects")
    p_field_report.add_argument("--exclude-edge", action="store_true", default=True, help="Reject EDGE objects in triage")
    p_field_report.add_argument("--include-edge", dest="exclude_edge", action="store_false", help="Do not reject EDGE objects")
    p_field_report.add_argument("--exclude-saturated", action="store_true", default=True, help="Reject SATURATED_LIKE objects in triage")
    p_field_report.add_argument("--include-saturated", dest="exclude_saturated", action="store_false", help="Do not reject saturated-like objects")
    p_field_report.add_argument("--exclude-catalog-outliers", action="store_true", default=True, help="Reject LARGE_CATALOG_MAG_RESIDUAL objects in triage")
    p_field_report.add_argument("--include-catalog-outliers", dest="exclude_catalog_outliers", action="store_false", help="Do not reject catalog-magnitude outliers")
    p_field_report.add_argument("--reject-aperture-unstable", action="store_true", default=True, help="Reject APERTURE_UNSTABLE / APERTURE_MEDIAN_SHIFT objects")
    p_field_report.add_argument("--include-aperture-unstable", dest="reject_aperture_unstable", action="store_false", help="Do not reject aperture-unstable objects")
    p_field_report.add_argument("--require-detection-support", action="store_true", default=True, help="Reject weak/no detection-support objects")
    p_field_report.add_argument("--allow-weak-detection-support", dest="require_detection_support", action="store_false", help="Do not reject weak/no detection-support objects")
    p_field_report.add_argument("--max-mag-median", type=float, default=22.5, help="Reject objects fainter than this median magnitude")
    p_field_report.add_argument("--max-ref-mag-delta", type=float, default=2.0, help="Reject objects with |mag_median - ref_mag| above this value")
    p_field_report.add_argument("--top-limit", type=int, default=20, help="Number of top candidates/blend rows stored in JSON report")
    p_field_report.add_argument("--out", default=None, help="Optional JSON report path")
    p_field_report.add_argument("--tables-prefix", default=None, help="Optional prefix for RMS/candidate/blend CSV tables")
    p_field_report.add_argument("--format", choices=("table", "json"), default="table", help="Console output format")
    p_field_report.set_defaults(func=cmd_field_qc_report)

    p_import_events = sub.add_parser("import-events", help="Import generic event registry CSV")
    p_import_events.add_argument("--file", required=True, help="CSV file with event rows")
    p_import_events.add_argument("--default-event-type", default=None, help="Default event_type when missing in CSV")
    p_import_events.add_argument("--default-source-catalog", default=None, help="Default source_catalog when missing in CSV")
    p_import_events.add_argument("--dry-run", action="store_true", help="Read/validate but do not write")
    p_import_events.set_defaults(func=cmd_import_events)

    p_show_events = sub.add_parser("show-events", help="Show registered events")
    p_show_events.add_argument("--event-type", default=None, help="Filter by event_type")
    p_show_events.add_argument("--source-catalog", default=None, help="Filter by source_catalog")
    p_show_events.add_argument("--status", default=None, help="Filter by status")
    p_show_events.add_argument("--target", default=None, help="Substring filter for target_name")
    p_show_events.add_argument("--limit", type=int, default=50, help="Maximum rows")
    p_show_events.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_show_events.set_defaults(func=cmd_show_events)

    p_match_events = sub.add_parser("match-events-to-objects", help="Link registered events to objects by ID or coordinates")
    p_match_events.add_argument("--event-type", default=None, help="Filter events by event_type")
    p_match_events.add_argument("--event-uid", default=None, help="Single event_uid")
    p_match_events.add_argument("--source-catalog", default=None, help="Filter events by source_catalog")
    p_match_events.add_argument("--external-catalog", default=None, help="Override external catalog used for ID matching")
    p_match_events.add_argument("--catalog-name", dest="external_id_column_preference", default=None, help="Catalog namespace for matching measurement flags, e.g. gaia_dr3_master")
    p_match_events.add_argument("--coordinate-radius-arcsec", type=float, default=5.0, help="Coordinate match radius")
    p_match_events.add_argument("--dry-run", action="store_true", help="Compute matches but do not write links")
    p_match_events.set_defaults(func=cmd_match_events_to_objects)

    p_event_links = sub.add_parser("show-event-object-links", help="Show event-object links")
    p_event_links.add_argument("--event-type", default=None, help="Filter by event_type")
    p_event_links.add_argument("--event-uid", default=None, help="Filter by event_uid")
    p_event_links.add_argument("--limit", type=int, default=50, help="Maximum rows")
    p_event_links.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_event_links.set_defaults(func=cmd_show_event_object_links)

    p_event_cov = sub.add_parser("event-coverage-summary", help="Summarize light-curve coverage of registered events")
    p_event_cov.add_argument("--calibration-name", required=True, help="CalibrationVersion name")
    p_event_cov.add_argument("--event-type", default=None, help="Filter by event_type")
    p_event_cov.add_argument("--event-uid", default=None, help="Single event_uid")
    p_event_cov.add_argument("--catalog-name", default=None, help="Filter measurement flags by catalog namespace")
    p_event_cov.add_argument("--instrument-unit-id", type=int, default=None, help="Filter by instrument unit")
    p_event_cov.add_argument("--window-padding-factor", type=float, default=1.0, help="Fallback pre/post baseline window width in units of event duration")
    p_event_cov.add_argument("--pre-margin-hours", type=float, default=None, help="Explicit pre-event baseline margin in hours; overrides --window-padding-factor for the pre side")
    p_event_cov.add_argument("--post-margin-hours", type=float, default=None, help="Explicit post-event baseline margin in hours; overrides --window-padding-factor for the post side")
    p_event_cov.add_argument("--event-window-source", choices=("auto", "explicit", "t0_duration"), default="auto", help="Use explicit t_start/t_end, t0/duration, or auto-prefer explicit")
    p_event_cov.add_argument("--min-depth-total-snr", type=float, default=3.0, help="Minimum estimated total SNR for GOOD_COVERAGE_GOOD_PRECISION")
    p_event_cov.add_argument("--min-event-points", type=int, default=1, help="Minimum points inside the event window")
    p_event_cov.add_argument("--min-pre-baseline-points", type=int, default=1, help="Minimum pre-event baseline points")
    p_event_cov.add_argument("--min-post-baseline-points", type=int, default=1, help="Minimum post-event baseline points")
    p_event_cov.add_argument("--bad-object-qc-flags", default=None, help="Comma-separated QC flags that make good coverage unusable; default uses built-in conservative list")
    p_event_cov.add_argument("--ignore-object-qc", action="store_true", help="Do not downgrade event observability because of object_qc flags")
    p_event_cov.add_argument("--write-frame-coverage", action="store_true", help="Write per-frame event_frame_coverage rows")
    p_event_cov.add_argument("--limit", type=int, default=None, help="Maximum events")
    p_event_cov.add_argument("--out", default=None, help="Optional CSV output path")
    p_event_cov.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_event_cov.set_defaults(func=cmd_event_coverage_summary)

    p_show_event_cov = sub.add_parser("show-event-coverage", help="Show stored per-frame event coverage rows")
    p_show_event_cov.add_argument("--event-type", default=None, help="Filter by event_type")
    p_show_event_cov.add_argument("--event-uid", default=None, help="Filter by event_uid")
    p_show_event_cov.add_argument("--calibration-name", default=None, help="Filter by CalibrationVersion name")
    p_show_event_cov.add_argument("--instrument-unit-id", type=int, default=None, help="Filter by instrument unit")
    p_show_event_cov.add_argument("--object-id", type=int, default=None, help="Filter by internal object_id")
    p_show_event_cov.add_argument("--time-relation", default=None, help="Filter by time_relation: pre_baseline, event, post_baseline, outside")
    p_show_event_cov.add_argument("--limit", type=int, default=200, help="Maximum stored frame-coverage rows")
    p_show_event_cov.add_argument("--out", default=None, help="Optional CSV output path")
    p_show_event_cov.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_show_event_cov.set_defaults(func=cmd_show_event_coverage)

    p_match_event_det = sub.add_parser("match-events-to-detections", help="Link registered events to source detections near event/object coordinates")
    p_match_event_det.add_argument("--event-type", default=None, help="Filter by event_type")
    p_match_event_det.add_argument("--event-uid", default=None, help="Single event_uid")
    p_match_event_det.add_argument("--instrument-unit-id", type=int, default=None, help="Filter detections by instrument unit")
    p_match_event_det.add_argument("--radius-arcsec", type=float, default=3.0, help="Sky matching radius around event/object position")
    p_match_event_det.add_argument("--min-snr", type=float, default=None, help="Minimum source_detection SNR")
    p_match_event_det.add_argument("--event-window-source", choices=("auto", "explicit", "t0_duration"), default="auto", help="Event time window source")
    p_match_event_det.add_argument("--pre-margin-hours", type=float, default=None, help="Pre-event baseline margin in hours")
    p_match_event_det.add_argument("--post-margin-hours", type=float, default=None, help="Post-event baseline margin in hours")
    p_match_event_det.add_argument("--window-padding-factor", type=float, default=1.0, help="Fallback baseline padding in event durations")
    p_match_event_det.add_argument("--include-outside-window", action="store_true", help="Also keep detections outside pre/event/post windows; required for events without a time window")
    p_match_event_det.add_argument("--limit-events", type=int, default=None, help="Maximum events to process")
    p_match_event_det.add_argument("--dry-run", action="store_true", help="Do not write event_detection_links")
    p_match_event_det.set_defaults(func=cmd_match_events_to_detections)

    p_show_event_det = sub.add_parser("show-event-detection-links", help="Show stored event-detection links")
    p_show_event_det.add_argument("--event-type", default=None, help="Filter by event_type")
    p_show_event_det.add_argument("--event-uid", default=None, help="Filter by event_uid")
    p_show_event_det.add_argument("--instrument-unit-id", type=int, default=None, help="Filter by instrument unit")
    p_show_event_det.add_argument("--time-relation", default=None, help="Filter by time_relation: pre_baseline, event, post_baseline, outside, no_event_window")
    p_show_event_det.add_argument("--catalog-matched", choices=("yes", "no"), default=None, help="Keep only catalog-matched or unmatched detections")
    p_show_event_det.add_argument("--min-snr", type=float, default=None, help="Minimum detection SNR")
    p_show_event_det.add_argument("--limit", type=int, default=200, help="Maximum rows")
    p_show_event_det.add_argument("--out", default=None, help="Optional CSV output path")
    p_show_event_det.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_show_event_det.set_defaults(func=cmd_show_event_detection_links)

    p_field_pipeline = sub.add_parser("run-field-pipeline", help="Run the main field-processing pipeline and record step provenance")
    p_field_pipeline.add_argument("--catalog-name", required=True, help="Catalog namespace already imported/matched for the field")
    p_field_pipeline.add_argument("--instrument-unit-id", type=int, default=None, help="Filter frames/measurements by instrument unit")
    p_field_pipeline.add_argument("--series-id", type=int, default=None, help="Run only frames assigned to this field_series.series_id")
    p_field_pipeline.add_argument("--calibration-prefix", required=True, help="Prefix for generated calibration versions and report files")
    p_field_pipeline.add_argument("--run-uid", default=None, help="Optional stable run identifier; autogenerated if omitted")
    p_field_pipeline.add_argument("--out-dir", default=None, help="Optional directory for field QC JSON/CSV outputs")
    p_field_pipeline.add_argument("--margin-pix", type=float, default=2.0, help="Catalog-to-frame footprint margin")
    p_field_pipeline.add_argument("--aperture-radius-pix", type=float, default=3.0, help="Forced-photometry aperture radius")
    p_field_pipeline.add_argument("--annulus-inner-pix", type=float, default=8.0, help="Background annulus inner radius")
    p_field_pipeline.add_argument("--annulus-outer-pix", type=float, default=12.0, help="Background annulus outer radius")
    p_field_pipeline.add_argument("--raw-calibration-name", default=None, help="Override generated raw aperture calibration name")
    p_field_pipeline.add_argument("--aperture-zp-calibration-name", default=None, help="Override generated aperture-ZP calibration name")
    p_field_pipeline.add_argument("--ensemble-calibration-name", default=None, help="Override generated ensemble calibration name")
    p_field_pipeline.add_argument("--detection-threshold-sigma", type=float, default=5.0, help="Source detection threshold")
    p_field_pipeline.add_argument("--detection-max-sources-per-frame", type=int, default=2000, help="Maximum detections per frame")
    p_field_pipeline.add_argument("--detection-min-separation-pix", type=float, default=5.0, help="Minimum source-detection separation")
    p_field_pipeline.add_argument("--detection-match-radius-arcsec", type=float, default=2.0, help="Detection-to-catalog matching radius")
    p_field_pipeline.add_argument("--bulk-insert-batch-size", type=int, default=1000, help="Rows per batched DB INSERT/UPSERT; capped automatically by PostgreSQL bind limit")
    p_field_pipeline.add_argument("--mag-key", default="G", help="Reference magnitude key in objects.ref_mags")
    p_field_pipeline.add_argument("--aperture-zp-min-ref-stars-per-frame", type=int, default=30, help="Aperture-ZP minimum reference stars per frame")
    p_field_pipeline.add_argument("--aperture-zp-max-mag-err", type=float, default=0.08, help="Aperture-ZP max mag_err; use negative to disable")
    p_field_pipeline.add_argument("--aperture-zp-clip-sigma", type=float, default=3.0, help="Aperture-ZP sigma clipping threshold")
    p_field_pipeline.add_argument("--aperture-zp-n-iterations", type=int, default=3, help="Aperture-ZP clipping iterations")
    p_field_pipeline.add_argument("--ensemble-min-measurements", type=int, default=10, help="Ensemble object-stat minimum measurements")
    p_field_pipeline.add_argument("--ensemble-min-ref-measurements", type=int, default=10, help="Ensemble reference candidate minimum measurements")
    p_field_pipeline.add_argument("--ensemble-min-ref-stars-per-frame", type=int, default=30, help="Minimum ensemble references per frame")
    p_field_pipeline.add_argument("--ensemble-max-ref-rms", type=float, default=0.08, help="Maximum RMS for ensemble reference candidates; use negative to disable")
    p_field_pipeline.add_argument("--ensemble-max-ref-mad", type=float, default=0.05, help="Maximum MAD for ensemble reference candidates; use negative to disable")
    p_field_pipeline.add_argument("--ensemble-n-iterations", type=int, default=3, help="Ensemble iterations")
    p_field_pipeline.add_argument("--object-qc-min-measurements", type=int, default=10, help="Object-QC minimum measurements")
    p_field_pipeline.add_argument("--crowded-arcsec", type=float, default=10.0, help="Object-QC crowded threshold")
    p_field_pipeline.add_argument("--blended-arcsec", type=float, default=5.0, help="Object-QC blend threshold")
    p_field_pipeline.add_argument("--edge-distance-pix", type=float, default=10.0, help="Object-QC edge threshold")
    p_field_pipeline.add_argument("--high-rms", type=float, default=0.10, help="Object-QC HIGH_RMS threshold")
    p_field_pipeline.add_argument("--high-mag-err", type=float, default=0.08, help="Object-QC FAINT_NOISY threshold")
    p_field_pipeline.add_argument("--min-detection-fraction", type=float, default=0.5, help="Object-QC weak detection-support threshold")
    p_field_pipeline.add_argument("--max-ref-mag-delta", type=float, default=2.0, help="Object-QC catalog residual threshold; use negative to disable")
    p_field_pipeline.add_argument("--disable-dynamic-separation", dest="dynamic_separation", action="store_false", help="Disable dynamic separation QC")
    p_field_pipeline.set_defaults(dynamic_separation=True)
    p_field_pipeline.add_argument("--dynamic-aperture-factor", type=float, default=1.0, help="Dynamic blend threshold aperture multiplier")
    p_field_pipeline.add_argument("--dynamic-crowding-factor", type=float, default=1.5, help="Dynamic crowded threshold multiplier")
    p_field_pipeline.add_argument("--fwhm-header-unit", choices=("arcsec", "pix", "auto"), default="arcsec", help="FWHM header unit for dynamic QC")
    p_field_pipeline.add_argument("--field-report-min-excess", type=float, default=2.0, help="Field report candidate RMS excess threshold")
    p_field_pipeline.add_argument("--disable-intrinsic-variability-candidates", dest="enable_intrinsic_variability_candidates", action="store_false", help="Disable field-report intrinsic variability candidate channel")
    p_field_pipeline.set_defaults(enable_intrinsic_variability_candidates=True)
    p_field_pipeline.add_argument("--field-report-min-intrinsic-rms", type=float, default=0.03, help="Field report intrinsic candidate minimum RMS; use negative to disable")
    p_field_pipeline.add_argument("--field-report-min-rms-over-mag-err", type=float, default=4.0, help="Field report intrinsic candidate mag_rms/effective_mag_err threshold; use negative to disable")
    p_field_pipeline.add_argument("--field-report-min-mad-over-mag-err", type=float, default=2.5, help="Field report intrinsic candidate mag_mad/effective_mag_err threshold; use negative to disable")
    p_field_pipeline.add_argument("--field-report-intrinsic-mag-err-floor", type=float, default=0.01, help="Field report intrinsic candidate error floor; use negative to disable")
    p_field_pipeline.add_argument("--field-report-min-robust-peak-to-peak", type=float, default=0.08, help="Field report intrinsic candidate P90-P10 amplitude threshold; use negative to disable")
    p_field_pipeline.add_argument("--disable-low-amplitude-intrinsic-candidates", dest="enable_low_amplitude_intrinsic_candidates", action="store_false", help="Disable field-report low-amplitude intrinsic candidate channel")
    p_field_pipeline.set_defaults(enable_low_amplitude_intrinsic_candidates=True)
    p_field_pipeline.add_argument("--field-report-low-amplitude-min-measurements", type=int, default=30, help="Field report low-amplitude intrinsic minimum measurements")
    p_field_pipeline.add_argument("--field-report-low-amplitude-min-intrinsic-rms", type=float, default=0.03, help="Field report low-amplitude intrinsic minimum RMS; use negative to disable")
    p_field_pipeline.add_argument("--field-report-low-amplitude-min-rms-over-mag-err", type=float, default=2.3, help="Field report low-amplitude intrinsic mag_rms/effective_mag_err threshold; use negative to disable")
    p_field_pipeline.add_argument("--field-report-low-amplitude-min-mad-over-mag-err", type=float, default=2.0, help="Field report low-amplitude intrinsic mag_mad/effective_mag_err threshold; use negative to disable")
    p_field_pipeline.add_argument("--field-report-low-amplitude-min-robust-peak-to-peak", type=float, default=0.08, help="Field report low-amplitude intrinsic P90-P10 threshold; use negative to disable")
    p_field_pipeline.add_argument("--field-report-max-mag-median", type=float, default=22.5, help="Field report faint cutoff; use negative to disable")
    p_field_pipeline.add_argument("--field-report-top-limit", type=int, default=20, help="Number of top candidates/blends in JSON report")
    p_field_pipeline.add_argument("--aperture-stability-radii", default="2,3,4,5", help="Candidate aperture-stability radii in pixels")
    p_field_pipeline.add_argument("--aperture-stability-max-candidates", type=int, default=None, help="Limit raw candidates checked by aperture-stability QC")
    p_field_pipeline.add_argument("--aperture-stability-min-corr", type=float, default=0.7, help="APERTURE_UNSTABLE threshold: minimum correlation with reference aperture")
    p_field_pipeline.add_argument("--aperture-stability-max-rms-delta", type=float, default=0.03, help="APERTURE_UNSTABLE threshold: maximum RMS delta vs reference aperture")
    p_field_pipeline.add_argument("--aperture-stability-max-median-shift", type=float, default=0.2, help="APERTURE_MEDIAN_SHIFT threshold")
    p_field_pipeline.add_argument("--aperture-stability-sweep-prefix", default=None, help="Override per-radius sweep calibration prefix")
    p_field_pipeline.add_argument("--skip-aperture-stability-qc", action="store_true", help="Skip batch aperture-stability QC for raw candidates")
    p_field_pipeline.add_argument("--skip-wcs", action="store_true", help="Skip WCS checking")
    p_field_pipeline.add_argument("--skip-catalog-frame-match", action="store_true", help="Skip catalog-to-frame projection")
    p_field_pipeline.add_argument("--skip-detection", action="store_true", help="Skip source detection")
    p_field_pipeline.add_argument("--skip-detection-catalog-match", action="store_true", help="Skip detection-to-catalog matching/frame QC")
    p_field_pipeline.add_argument("--skip-forced-photometry", action="store_true", help="Skip forced aperture photometry")
    p_field_pipeline.add_argument("--skip-aperture-zp", action="store_true", help="Skip aperture-ZP calibration")
    p_field_pipeline.add_argument("--skip-ensemble", action="store_true", help="Skip ensemble calibration")
    p_field_pipeline.add_argument("--skip-object-qc", action="store_true", help="Skip object QC")
    p_field_pipeline.add_argument("--skip-field-report", action="store_true", help="Skip field QC report")
    p_field_pipeline.add_argument("--no-candidate-light-curves", dest="export_candidate_light_curves", action="store_false", help="Do not export per-candidate accepted light curves")
    p_field_pipeline.set_defaults(export_candidate_light_curves=True)
    p_field_pipeline.add_argument("--no-candidate-plots", dest="export_candidate_plots", action="store_false", help="Do not render PNG quick-look plots for exported candidates")
    p_field_pipeline.set_defaults(export_candidate_plots=True)
    p_field_pipeline.add_argument("--candidate-light-curve-limit", type=int, default=None, help="Limit accepted candidates exported as light curves")
    p_field_pipeline.add_argument("--exclude-candidates-from-ensemble", action="store_true", help="Run candidate-excluded ensemble passes after initial field candidate triage")
    p_field_pipeline.add_argument("--candidate-ensemble-max-iterations", type=int, default=2, help="Maximum total ensemble iterations for fixed candidate-excluded mode; 2 means one candidate-excluded pass")
    p_field_pipeline.add_argument("--candidate-ensemble-until-converged", action="store_true", help="Iterate candidate-excluded ensemble calibration until no new accepted candidates appear, up to --candidate-ensemble-convergence-max-iterations")
    p_field_pipeline.add_argument("--candidate-ensemble-convergence-max-iterations", type=int, default=10, help="Safety cap for --candidate-ensemble-until-converged; counts the initial ensemble as iteration 1")
    p_field_pipeline.add_argument(
        "--replace-detections",
        dest="replace_detections",
        action="store_true",
        default=True,
        help="Delete existing detections for selected frames before detection (default for idempotent runs)",
    )
    p_field_pipeline.add_argument(
        "--keep-existing-detections",
        dest="replace_detections",
        action="store_false",
        help="Keep existing source_detections and append new detection rows",
    )
    p_field_pipeline.add_argument(
        "--replace-detection-matches",
        dest="replace_detection_matches",
        action="store_true",
        default=True,
        help="Delete existing detection-catalog matches before matching (default for idempotent runs)",
    )
    p_field_pipeline.add_argument(
        "--keep-existing-detection-matches",
        dest="replace_detection_matches",
        action="store_false",
        help="Keep existing detection_catalog_matches and rely only on upserts",
    )
    p_field_pipeline.add_argument("--dry-run", action="store_true", help="Run stages in dry-run mode; pipeline metadata is still recorded")
    p_field_pipeline.add_argument("--no-record", dest="record_run", action="store_false", help="Do not write pipeline_runs/pipeline_run_steps metadata")
    p_field_pipeline.set_defaults(record_run=True)
    p_field_pipeline.add_argument("--log-dir", default="logs", help="Root directory for per-run logs; default: logs")
    p_field_pipeline.add_argument("--file-log-level", default=None, help="File log level: DEBUG, INFO, WARNING, ERROR; defaults to global --log-level or INFO")
    p_field_pipeline.add_argument("--no-file-log", action="store_true", help="Do not write run.log/run.jsonl/errors.log files")
    p_field_pipeline.add_argument("--continue-on-error", action="store_true", help="Continue later stages after a failed step")
    p_field_pipeline.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_field_pipeline.set_defaults(func=cmd_run_field_pipeline)

    p_idem = sub.add_parser("pipeline-idempotency-report", help="Audit duplicate DB identities after repeated pipeline runs")
    p_idem.add_argument("--calibration-name", default=None, help="CalibrationVersion name for measurements/object_qc scope")
    p_idem.add_argument("--catalog-name", default=None, help="Catalog namespace for object_qc/frame_qc/detection matches")
    p_idem.add_argument("--instrument-unit-id", type=int, default=None, help="Restrict frame-backed checks to one instrument unit")
    p_idem.add_argument("--series-id", type=int, default=None, help="Restrict checks to one field_series.series_id")
    p_idem.add_argument("--field-series-name", default=None, help="Restrict checks to one field_series identifier/pipeline/calibration prefix")
    p_idem.add_argument("--frame-id", type=int, action="append", default=[], help="Explicit frame_id restriction; may be repeated")
    p_idem.add_argument("--object-qc-algorithm", default="object_qc_v1", help="object_qc.algorithm")
    p_idem.add_argument("--detection-algorithm", default="numpy_localmax_v1", help="source_detections.algorithm")
    p_idem.add_argument("--detection-match-algorithm", default="nearest_sky_v1", help="detection_catalog_matches.algorithm")
    p_idem.add_argument("--frame-qc-algorithm", default="detection_catalog_qc_v1", help="frame_qc.algorithm")
    p_idem.add_argument("--example-limit", type=int, default=10, help="Maximum duplicate examples per check in JSON output")
    p_idem.add_argument("--out", default=None, help="Optional JSON report path")
    p_idem.add_argument("--format", choices=("table", "json"), default="table", help="Console output format")
    p_idem.set_defaults(func=cmd_pipeline_idempotency_report)

    p_archive_pipeline = sub.add_parser("run-archive-pipeline", help="Run field pipeline for a batch of field_series rows")
    p_archive_pipeline.add_argument("--catalog-name", required=True, help="Catalog namespace already imported/matched")
    p_archive_pipeline.add_argument("--status", default="READY", help="Series status to process, default: READY")
    p_archive_pipeline.add_argument("--field-id", type=int, default=None, help="Restrict to one field_id")
    p_archive_pipeline.add_argument("--instrument-unit-id", type=int, default=None, help="Restrict to one instrument unit")
    p_archive_pipeline.add_argument("--limit", type=int, default=None, help="Maximum number of series to process")
    p_archive_pipeline.add_argument("--calibration-prefix-template", default="{field_name}_s{series_id}", help="Template fields: field_name, field_id, series_id, filter_name, instrument_unit_id")
    p_archive_pipeline.add_argument("--out-root", default="exports", help="Root directory for per-series export directories")
    p_archive_pipeline.add_argument("--log-dir", default="logs", help="Root directory for per-run logs")
    p_archive_pipeline.add_argument("--file-log-level", default=None, help="File log level: DEBUG, INFO, WARNING, ERROR")
    p_archive_pipeline.add_argument("--aperture-radius-pix", type=float, default=3.0, help="Forced-photometry aperture radius")
    p_archive_pipeline.add_argument("--annulus-inner-pix", type=float, default=8.0, help="Background annulus inner radius")
    p_archive_pipeline.add_argument("--annulus-outer-pix", type=float, default=12.0, help="Background annulus outer radius")
    p_archive_pipeline.add_argument("--dry-run", action="store_true", help="Run pipeline stages in dry-run mode")
    p_archive_pipeline.add_argument("--no-file-log", action="store_true", help="Do not write run.log/run.jsonl/errors.log files")
    p_archive_pipeline.add_argument("--stop-on-error", action="store_true", help="Stop batch after first failed series")
    p_archive_pipeline.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_archive_pipeline.set_defaults(func=cmd_run_archive_pipeline)

    p_process_path = sub.add_parser(
        "process-archive-path",
        help="One-command path processing: auto-detect instrument, index FITS, build series, run pipeline",
    )
    p_process_path.add_argument("--path", required=True, help="Directory with FITS files")
    p_process_path.add_argument("--catalog-name", default=None, help="Catalog namespace already imported; omitted means auto_catalog_* with Gaia DR3 fetch")
    p_process_path.add_argument("--execute", action="store_true", help="Write to DB and run pipelines; omitted means plan-only")
    p_process_path.add_argument("--plan-only", action="store_true", help="Alias for the default non-writing planning mode")
    p_process_path.add_argument("--instrument-profile", default="auto", choices=("auto", "MASTER", "RoboPhot"), help="Profile detection mode")
    p_process_path.add_argument("--master-profile", default=None, help="Override MASTER YAML profile path")
    p_process_path.add_argument("--robophot-profile", default=None, help="Override RoboPhot YAML profile path")
    p_process_path.add_argument("--extensions", default=".fits,.fit,.fts", help="Comma-separated FITS extensions")
    p_process_path.add_argument("--no-recursive", dest="recursive", action="store_false", help="Do not search subdirectories")
    p_process_path.set_defaults(recursive=True)
    p_process_path.add_argument("--limit", type=int, default=None, help="Limit discovered FITS files")
    p_process_path.add_argument("--min-frames-per-series", type=int, default=10, help="Minimum frames per generated field_series")
    p_process_path.add_argument("--max-field-separation-arcmin", type=float, default=30.0, help="Maximum center separation for same field")
    p_process_path.add_argument("--max-series-gap-minutes", type=float, default=60.0, help="Start new series after this time gap")
    p_process_path.add_argument("--include-assigned", action="store_true", help="Allow already assigned frames when building series")
    p_process_path.add_argument("--include-non-science", action="store_true", help="Include calibration/focus/test frames in series grouping")
    p_process_path.add_argument("--calibration-prefix-template", default=None, help="Template for generated pipeline prefixes")
    p_process_path.add_argument("--out-root", default="exports", help="Root directory for per-series outputs")
    p_process_path.add_argument("--log-dir", default="logs", help="Root directory for pipeline logs")
    p_process_path.add_argument("--file-log-level", default="INFO", help="File log level")
    p_process_path.add_argument("--no-file-log", action="store_true", help="Do not write run.log/run.jsonl/errors.log")
    p_process_path.add_argument("--aperture-radius-pix", type=float, default=3.0, help="Forced-photometry aperture radius")
    p_process_path.add_argument("--annulus-inner-pix", type=float, default=8.0, help="Background annulus inner radius")
    p_process_path.add_argument("--annulus-outer-pix", type=float, default=12.0, help="Background annulus outer radius")
    p_process_path.add_argument("--bulk-insert-batch-size", type=int, default=1000, help="Rows per batched DB INSERT/UPSERT inside the field pipeline")
    p_process_path.add_argument("--working-catalog-mag-limit", type=float, default=19.0, help="Gaia/reference magnitude cutoff for the forced-photometry working set; use negative to disable")
    p_process_path.add_argument("--max-working-sources-per-frame", type=int, default=2000, help="Maximum brightest catalog sources linked/measured per frame; use 0 to disable")
    p_process_path.add_argument("--catalog-edge-margin-pix", type=float, default=20.0, help="Reject working catalog sources closer than this to frame edges")
    p_process_path.add_argument("--enable-detection-branch", action="store_true", help="Run source detection and detection-catalog matching; disabled by default for large forced-photometry runs")
    p_process_path.add_argument("--require-detection-support", dest="require_detection_support", action="store_true", default=None, help="Reject weak/no detection-support objects in science light curves and candidate triage")
    p_process_path.add_argument("--no-require-detection-support", dest="require_detection_support", action="store_false", help="Treat weak/no detection support as diagnostic only, not as a science-light-curve rejection flag")
    p_process_path.add_argument("--target-source-id", default=None, help="Explicit external source_id/Gaia DR3 source_id for the field target")
    p_process_path.add_argument("--target-object-id", type=int, default=None, help="Explicit internal objects.object_id for the field target")
    p_process_path.add_argument("--target-max-sep-arcsec", type=float, default=120.0, help="Maximum allowed auto-resolved target distance from the mean frame centre")
    p_process_path.add_argument("--no-target-light-curve", dest="export_target_light_curve", action="store_false", help="Do not export a separate target light curve")
    p_process_path.set_defaults(export_target_light_curve=True)
    p_process_path.add_argument("--no-candidate-light-curves", dest="export_candidate_light_curves", action="store_false", help="Do not export per-candidate accepted light curves")
    p_process_path.set_defaults(export_candidate_light_curves=True)
    p_process_path.add_argument("--no-candidate-plots", dest="export_candidate_plots", action="store_false", help="Do not render PNG quick-look plots for exported candidates")
    p_process_path.set_defaults(export_candidate_plots=True)
    p_process_path.add_argument("--candidate-light-curve-limit", type=int, default=None, help="Limit accepted candidates exported as light curves")
    p_process_path.add_argument("--exclude-candidates-from-ensemble", action="store_true", help="Run candidate-excluded ensemble passes after initial field candidate triage")
    p_process_path.add_argument("--candidate-ensemble-max-iterations", type=int, default=2, help="Maximum total ensemble iterations for fixed candidate-excluded mode; 2 means one candidate-excluded pass")
    p_process_path.add_argument("--candidate-ensemble-until-converged", action="store_true", help="Iterate candidate-excluded ensemble calibration until no new accepted candidates appear, up to --candidate-ensemble-convergence-max-iterations")
    p_process_path.add_argument("--candidate-ensemble-convergence-max-iterations", type=int, default=10, help="Safety cap for --candidate-ensemble-until-converged; counts the initial ensemble as iteration 1")
    p_process_path.add_argument("--enable-aperture-stability-qc", action="store_true", help="Run expensive aperture-radius stability sweep; disabled by default in process-archive-path")
    p_process_path.add_argument("--exclude-bad-pre-qc-frames", action="store_true", help="Exclude frames marked BAD by pre-photometry QC from downstream photometry/calibration")
    p_process_path.add_argument("--force-recompute-photometry", action="store_true", help="Recompute raw forced photometry even if the target calibration already has measurements")
    p_process_path.add_argument("--disable-intrinsic-variability-candidates", dest="enable_intrinsic_variability_candidates", action="store_false", help="Disable intrinsic-variability candidate channel in field QC")
    p_process_path.set_defaults(enable_intrinsic_variability_candidates=True)
    p_process_path.add_argument("--field-report-min-intrinsic-rms", type=float, default=0.03, help="Intrinsic candidate minimum RMS in field QC; use negative to disable")
    p_process_path.add_argument("--field-report-min-rms-over-mag-err", type=float, default=4.0, help="Intrinsic candidate mag_rms/effective_mag_err threshold; use negative to disable")
    p_process_path.add_argument("--field-report-min-mad-over-mag-err", type=float, default=2.5, help="Intrinsic candidate mag_mad/effective_mag_err threshold; use negative to disable")
    p_process_path.add_argument("--field-report-intrinsic-mag-err-floor", type=float, default=0.01, help="Intrinsic candidate error floor in field QC; use negative to disable")
    p_process_path.add_argument("--field-report-min-robust-peak-to-peak", type=float, default=0.08, help="Intrinsic candidate P90-P10 amplitude threshold in field QC; use negative to disable")
    p_process_path.add_argument("--disable-low-amplitude-intrinsic-candidates", dest="enable_low_amplitude_intrinsic_candidates", action="store_false", help="Disable field-report low-amplitude intrinsic candidate channel")
    p_process_path.set_defaults(enable_low_amplitude_intrinsic_candidates=True)
    p_process_path.add_argument("--field-report-low-amplitude-min-measurements", type=int, default=30, help="Low-amplitude intrinsic minimum measurements")
    p_process_path.add_argument("--field-report-low-amplitude-min-intrinsic-rms", type=float, default=0.03, help="Low-amplitude intrinsic minimum RMS; use negative to disable")
    p_process_path.add_argument("--field-report-low-amplitude-min-rms-over-mag-err", type=float, default=2.3, help="Low-amplitude intrinsic mag_rms/effective_mag_err threshold; use negative to disable")
    p_process_path.add_argument("--field-report-low-amplitude-min-mad-over-mag-err", type=float, default=2.0, help="Low-amplitude intrinsic mag_mad/effective_mag_err threshold; use negative to disable")
    p_process_path.add_argument("--field-report-low-amplitude-min-robust-peak-to-peak", type=float, default=0.08, help="Low-amplitude intrinsic P90-P10 threshold; use negative to disable")
    p_process_path.add_argument("--no-light-curve-summary", dest="make_light_curve_summary", action="store_false", help="Skip per-series science light-curve summary JSON")
    p_process_path.set_defaults(make_light_curve_summary=True)
    p_process_path.add_argument("--no-auto-catalog-query", dest="auto_catalog_query", action="store_false", help="When --catalog-name is omitted, generate an auto catalog name but do not query Gaia DR3")
    p_process_path.set_defaults(auto_catalog_query=True)
    p_process_path.add_argument("--auto-catalog-margin-arcmin", type=float, default=2.0, help="Gaia cone margin for auto catalog generation")
    p_process_path.add_argument("--auto-catalog-mag-limit", type=float, default=None, help="Optional Gaia G magnitude limit for auto catalog generation")
    p_process_path.add_argument("--auto-catalog-max-rows-per-frame", type=int, default=50000, help="Maximum Gaia rows in the auto catalog query")
    p_process_path.add_argument("--auto-catalog-query-mode", choices=("frame_union", "per_frame"), default="frame_union", help="Gaia query strategy for auto catalogs; frame_union queries one cone for the whole input field")
    p_process_path.add_argument("--auto-catalog-max-radius-deg", type=float, default=1.0, help="Abort auto Gaia query if the computed cone radius exceeds this value")
    p_process_path.add_argument("--auto-catalog-timeout-sec", type=float, default=120.0, help="Best-effort astroquery timeout for auto Gaia query")
    p_process_path.add_argument("--allow-empty-catalog", action="store_true", help="Debug only: continue pipeline even if auto catalog query fails or inserts zero sources")
    p_process_path.add_argument("--manifest-out", default=None, help="Optional JSON manifest path")
    p_process_path.add_argument("--format", choices=("table", "json"), default="table", help="Console output format")
    p_process_path.set_defaults(func=cmd_process_archive_path)

    p_show_pipeline_runs = sub.add_parser("show-pipeline-runs", help="Show recorded field pipeline runs")
    p_show_pipeline_runs.add_argument("--limit", type=int, default=20, help="Maximum rows")
    p_show_pipeline_runs.add_argument("--status", default=None, help="Filter by run status")
    p_show_pipeline_runs.add_argument("--catalog-name", default=None, help="Filter by catalog name")
    p_show_pipeline_runs.add_argument("--instrument-unit-id", type=int, default=None, help="Filter by instrument unit")
    p_show_pipeline_runs.add_argument("--field-id", type=int, default=None, help="Filter by field_id")
    p_show_pipeline_runs.add_argument("--series-id", type=int, default=None, help="Filter by series_id")
    p_show_pipeline_runs.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_show_pipeline_runs.set_defaults(func=cmd_show_pipeline_runs)

    p_show_pipeline_steps = sub.add_parser("show-pipeline-steps", help="Show recorded steps for a pipeline run")
    p_show_pipeline_steps.add_argument("--pipeline-run-id", type=int, default=None, help="pipeline_runs.pipeline_run_id")
    p_show_pipeline_steps.add_argument("--run-uid", default=None, help="pipeline_runs.run_uid")
    p_show_pipeline_steps.add_argument("--limit", type=int, default=50, help="Maximum rows")
    p_show_pipeline_steps.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_show_pipeline_steps.set_defaults(func=cmd_show_pipeline_steps)

    p_show_pipeline_logs = sub.add_parser("show-pipeline-log-events", help="Show structured WARNING/ERROR/step pipeline log events")
    p_show_pipeline_logs.add_argument("--pipeline-run-id", type=int, default=None, help="pipeline_runs.pipeline_run_id")
    p_show_pipeline_logs.add_argument("--run-uid", default=None, help="pipeline_runs.run_uid")
    p_show_pipeline_logs.add_argument("--level", default=None, help="Filter by level, e.g. WARNING or ERROR")
    p_show_pipeline_logs.add_argument("--event-type", default=None, help="Filter by event_type, e.g. step_failed")
    p_show_pipeline_logs.add_argument("--limit", type=int, default=100, help="Maximum rows")
    p_show_pipeline_logs.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_show_pipeline_logs.set_defaults(func=cmd_show_pipeline_log_events)

    p_ap_sweep = sub.add_parser("aperture-radius-sweep", help="Run/compare one object across several aperture radii")
    p_ap_sweep.add_argument("--catalog-name", required=True, help="Catalog namespace in measurement flags")
    p_ap_sweep.add_argument("--source-id", default=None, help="External catalog source_id")
    p_ap_sweep.add_argument("--object-id", type=int, default=None, help="Internal object_id")
    p_ap_sweep.add_argument("--instrument-unit-id", type=int, default=None, help="Filter by instrument unit")
    p_ap_sweep.add_argument("--radii", required=True, help="Comma-separated radii in pixels, e.g. 2,3,4,5")
    p_ap_sweep.add_argument("--calibration-prefix", default=None, help="Optional prefix for per-radius calibration versions")
    p_ap_sweep.add_argument("--annulus-inner-pix", type=float, default=8.0, help="Annulus inner radius")
    p_ap_sweep.add_argument("--annulus-outer-pix", type=float, default=12.0, help="Annulus outer radius")
    p_ap_sweep.add_argument("--mag-key", default="G", help="Reference magnitude key")
    p_ap_sweep.add_argument("--min-ref-stars-per-frame", type=int, default=30, help="Aperture-ZP minimum reference stars")
    p_ap_sweep.add_argument("--max-mag-err", type=float, default=0.08, help="Aperture-ZP maximum mag_err; use negative to disable")
    p_ap_sweep.add_argument("--max-zp-mad", type=float, default=None, help="Reject frame ZP if MAD exceeds this")
    p_ap_sweep.add_argument("--clip-sigma", type=float, default=3.0, help="Aperture-ZP sigma clipping threshold")
    p_ap_sweep.add_argument("--n-iterations", type=int, default=3, help="Number of clipping/ensemble iterations")
    p_ap_sweep.add_argument("--min-ref-measurements", type=int, default=10, help="Ensemble minimum reference measurements")
    p_ap_sweep.add_argument("--ensemble-min-ref-stars-per-frame", type=int, default=30, help="Ensemble minimum reference stars per frame")
    p_ap_sweep.add_argument("--max-ref-rms", type=float, default=0.08, help="Ensemble max reference RMS")
    p_ap_sweep.add_argument("--max-ref-mad", type=float, default=0.05, help="Ensemble max reference MAD")
    p_ap_sweep.add_argument("--skip-photometry", action="store_true", help="Only use existing raw aperture measurements")
    p_ap_sweep.add_argument("--skip-calibration", action="store_true", help="Only summarize existing ensemble calibrations")
    p_ap_sweep.add_argument("--dry-run", action="store_true", help="Compute pipeline stages without writing")
    p_ap_sweep.add_argument("--out", default=None, help="Optional CSV output path")
    p_ap_sweep.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_ap_sweep.set_defaults(func=cmd_aperture_radius_sweep)

    p_ap_stab = sub.add_parser("compute-aperture-stability-qc", help="Append aperture-radius stability flags to object_qc")
    p_ap_stab.add_argument("--calibration-name", required=True, help="Base CalibrationVersion name used in object_qc")
    p_ap_stab.add_argument("--catalog-name", default=None, help="Catalog namespace in measurement flags")
    p_ap_stab.add_argument("--source-id", default=None, help="External catalog source_id")
    p_ap_stab.add_argument("--object-id", type=int, default=None, help="Internal object_id")
    p_ap_stab.add_argument("--instrument-unit-id", type=int, default=None, help="Filter by instrument unit")
    p_ap_stab.add_argument("--radii", required=True, help="Comma-separated radii used in aperture-radius-sweep")
    p_ap_stab.add_argument("--sweep-calibration-prefix", default=None, help="Optional prefix used by aperture-radius-sweep per-radius calibration versions")
    p_ap_stab.add_argument("--annulus-inner-pix", type=float, default=8.0, help="Annulus inner radius used for sweep calibration names")
    p_ap_stab.add_argument("--annulus-outer-pix", type=float, default=12.0, help="Annulus outer radius used for sweep calibration names")
    p_ap_stab.add_argument("--mag-key", default="G", help="Reference magnitude key used for sweep calibration names")
    p_ap_stab.add_argument("--min-stable-corr", type=float, default=0.7, help="Flag APERTURE_UNSTABLE if min correlation is below this")
    p_ap_stab.add_argument("--max-stable-rms-delta", type=float, default=0.03, help="Flag APERTURE_UNSTABLE if max RMS delta exceeds this")
    p_ap_stab.add_argument("--max-median-shift", type=float, default=0.2, help="Flag APERTURE_MEDIAN_SHIFT if max median shift exceeds this")
    p_ap_stab.add_argument("--possible-blend-neighbor-arcsec", type=float, default=None, help="Flag POSSIBLE_BLEND for unstable objects with neighbor closer than this")
    p_ap_stab.add_argument("--algorithm", default="object_qc_v1", help="object_qc.algorithm to update")
    p_ap_stab.add_argument("--dry-run", action="store_true", help="Compute but do not update object_qc")
    p_ap_stab.set_defaults(func=cmd_compute_aperture_stability_qc)

    p_transfer_ap_stab = sub.add_parser(
        "transfer-aperture-stability-qc",
        help="Copy aperture-stability diagnostics from a source calibration to a derived calibration",
    )
    p_transfer_ap_stab.add_argument("--target-calibration-name", required=True, help="Target/derived CalibrationVersion name")
    p_transfer_ap_stab.add_argument("--source-calibration-name", default=None, help="Source CalibrationVersion name; defaults to target provenance")
    p_transfer_ap_stab.add_argument("--catalog-name", default=None, help="Catalog namespace in object_qc")
    p_transfer_ap_stab.add_argument("--instrument-unit-id", type=int, default=None, help="Filter by instrument unit")
    p_transfer_ap_stab.add_argument("--algorithm", default="object_qc_v1", help="object_qc.algorithm to update")
    p_transfer_ap_stab.add_argument("--create-missing", action="store_true", help="Create minimal target object_qc rows when they are absent")
    p_transfer_ap_stab.add_argument("--dry-run", action="store_true", help="Count rows without writing")
    p_transfer_ap_stab.add_argument("--format", choices=("table", "json"), default="table", help="Output format")
    p_transfer_ap_stab.set_defaults(func=cmd_transfer_aperture_stability_qc)

    p_candidate_ap_stab = sub.add_parser("compute-candidate-aperture-stability-qc", help="Run aperture-stability QC for all raw high-RMS candidates")
    p_candidate_ap_stab.add_argument("--calibration-name", required=True, help="Base CalibrationVersion name used in object_qc")
    p_candidate_ap_stab.add_argument("--catalog-name", default=None, help="Catalog namespace in measurement flags")
    p_candidate_ap_stab.add_argument("--instrument-unit-id", type=int, default=None, help="Filter by instrument unit")
    p_candidate_ap_stab.add_argument("--radii", default="2,3,4,5", help="Comma-separated radii in pixels")
    p_candidate_ap_stab.add_argument("--sweep-calibration-prefix", default=None, help="Optional prefix for per-radius sweep calibration versions")
    p_candidate_ap_stab.add_argument("--annulus-inner-pix", type=float, default=8.0, help="Annulus inner radius")
    p_candidate_ap_stab.add_argument("--annulus-outer-pix", type=float, default=12.0, help="Annulus outer radius")
    p_candidate_ap_stab.add_argument("--mag-key", default="G", help="Reference magnitude key")
    p_candidate_ap_stab.add_argument("--min-measurements", type=int, default=10, help="Minimum points per object in candidate search")
    p_candidate_ap_stab.add_argument("--min-excess", type=float, default=2.0, help="Raw candidate RMS excess threshold")
    p_candidate_ap_stab.add_argument("--max-mag-median", type=float, default=22.5, help="Candidate faint cutoff; use negative to disable")
    p_candidate_ap_stab.add_argument("--max-ref-mag-delta", type=float, default=2.0, help="Candidate catalog residual threshold; use negative to disable")
    p_candidate_ap_stab.add_argument("--max-candidates", type=int, default=None, help="Maximum raw candidates to check")
    p_candidate_ap_stab.add_argument("--min-stable-corr", type=float, default=0.7, help="APERTURE_UNSTABLE min correlation threshold")
    p_candidate_ap_stab.add_argument("--max-stable-rms-delta", type=float, default=0.03, help="APERTURE_UNSTABLE max RMS-delta threshold")
    p_candidate_ap_stab.add_argument("--max-median-shift", type=float, default=0.2, help="APERTURE_MEDIAN_SHIFT threshold")
    p_candidate_ap_stab.add_argument("--possible-blend-neighbor-arcsec", type=float, default=None, help="Flag POSSIBLE_BLEND for unstable objects with close neighbors")
    p_candidate_ap_stab.add_argument("--min-ref-stars-per-frame", type=int, default=30, help="Aperture-ZP minimum reference stars")
    p_candidate_ap_stab.add_argument("--max-mag-err", type=float, default=0.08, help="Aperture-ZP maximum mag_err; use negative to disable")
    p_candidate_ap_stab.add_argument("--clip-sigma", type=float, default=3.0, help="Aperture-ZP clipping sigma")
    p_candidate_ap_stab.add_argument("--n-iterations", type=int, default=3, help="Calibration iteration count")
    p_candidate_ap_stab.add_argument("--min-ref-measurements", type=int, default=10, help="Ensemble minimum reference measurements")
    p_candidate_ap_stab.add_argument("--ensemble-min-ref-stars-per-frame", type=int, default=30, help="Ensemble minimum reference stars per frame")
    p_candidate_ap_stab.add_argument("--max-ref-rms", type=float, default=0.08, help="Ensemble max reference RMS; use negative to disable")
    p_candidate_ap_stab.add_argument("--max-ref-mad", type=float, default=0.05, help="Ensemble max reference MAD; use negative to disable")
    p_candidate_ap_stab.add_argument("--skip-sweep-preparation", action="store_true", help="Use existing per-radius calibration versions")
    p_candidate_ap_stab.add_argument("--stop-on-error", dest="continue_on_error", action="store_false", help="Stop at first failed candidate")
    p_candidate_ap_stab.set_defaults(continue_on_error=True)
    p_candidate_ap_stab.add_argument("--dry-run", action="store_true", help="Compute without updating object_qc")
    p_candidate_ap_stab.add_argument("--out", default=None, help="Optional CSV output path")
    p_candidate_ap_stab.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_candidate_ap_stab.set_defaults(func=cmd_compute_candidate_aperture_stability_qc)

    p_object_qc = sub.add_parser("compute-object-qc", help="Compute object-level QC flags: crowding, edge, saturation-like, high RMS")
    p_object_qc.add_argument("--calibration-name", required=True, help="CalibrationVersion name")
    p_object_qc.add_argument("--catalog-name", default=None, help="Filter by catalog namespace")
    p_object_qc.add_argument("--instrument-unit-id", type=int, default=None, help="Filter by instrument unit")
    p_object_qc.add_argument("--min-measurements", type=int, default=5, help="Minimum measurements per object")
    p_object_qc.add_argument("--crowded-arcsec", type=float, default=5.0, help="Nearest neighbor threshold for CROWDED")
    p_object_qc.add_argument("--blended-arcsec", type=float, default=3.0, help="Nearest neighbor threshold for BLENDED_NEIGHBOR")
    p_object_qc.add_argument("--edge-distance-pix", type=float, default=10.0, help="Minimum acceptable distance from frame edge")
    p_object_qc.add_argument("--high-rms", type=float, default=0.10, help="RMS threshold for HIGH_RMS flag")
    p_object_qc.add_argument("--high-mag-err", type=float, default=0.08, help="Median mag_err threshold for FAINT_NOISY flag")
    p_object_qc.add_argument("--min-detection-fraction", type=float, default=0.5, help="Minimum detection-match fraction before WEAK_DETECTION_SUPPORT")
    p_object_qc.add_argument("--max-ref-mag-delta", type=float, default=None, help="Flag LARGE_CATALOG_MAG_RESIDUAL when |mag_median - ref_mag| exceeds this")
    p_object_qc.add_argument("--proper-motion-sep-arcsec", type=float, default=1.0, help="Flag POSSIBLE_PROPER_MOTION_MISMATCH when large mag residual and match sep exceeds this")
    p_object_qc.add_argument("--dynamic-separation", action="store_true", help="Compute seeing/aperture-based dynamic separation thresholds")
    p_object_qc.add_argument("--dynamic-seeing-factor", type=float, default=1.5, help="Multiplier for median frame FWHM in dynamic blend threshold")
    p_object_qc.add_argument("--dynamic-aperture-factor", type=float, default=1.0, help="Multiplier for aperture radius in dynamic blend threshold")
    p_object_qc.add_argument("--dynamic-crowding-factor", type=float, default=1.5, help="Multiplier from dynamic blend threshold to dynamic crowding threshold")
    p_object_qc.add_argument("--min-dynamic-blend-arcsec", type=float, default=None, help="Optional minimum dynamic blend threshold")
    p_object_qc.add_argument("--fwhm-header-unit", choices=("arcsec", "pix", "auto"), default="arcsec", help="Interpret frame FWHM/seeing header values as arcsec, pixels, or auto")
    p_object_qc.add_argument("--algorithm", default="object_qc_v1", help="object_qc.algorithm")
    p_object_qc.add_argument("--dry-run", action="store_true", help="Compute but do not write object_qc rows")
    p_object_qc.set_defaults(func=cmd_compute_object_qc)

    p_compare = sub.add_parser("compare-calibrations", help="Compare per-object scatter between two calibration versions")
    p_compare.add_argument("--left", required=True, help="Left/source CalibrationVersion name")
    p_compare.add_argument("--right", required=True, help="Right/target CalibrationVersion name")
    p_compare.add_argument("--catalog-name", default=None, help="Filter by catalog source namespace")
    p_compare.add_argument("--instrument-unit-id", type=int, default=None, help="Filter by instrument unit")
    p_compare.add_argument("--min-measurements", type=int, default=5, help="Minimum measurements per object")
    p_compare.add_argument("--limit", type=int, default=50, help="Maximum object rows")
    p_compare.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_compare.set_defaults(func=cmd_compare_calibrations)

    p_show_lc = sub.add_parser("show-lightcurve", help="Print a light curve from measurements")
    p_show_lc.add_argument("--calibration-name", default=None, help="Filter by CalibrationVersion name")
    p_show_lc.add_argument("--source-id", default=None, help="External source_id from measurement flags")
    p_show_lc.add_argument("--object-id", type=int, default=None, help="Internal object_id")
    p_show_lc.add_argument("--catalog-name", default=None, help="Filter by catalog source namespace in measurement flags")
    p_show_lc.add_argument("--instrument-unit-id", type=int, default=None, help="Filter by instrument unit")
    p_show_lc.add_argument("--limit", type=int, default=50, help="Maximum rows")
    p_show_lc.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_show_lc.set_defaults(func=cmd_show_lightcurve)

    p_export_lc = sub.add_parser("export-lightcurve", help="Export a light curve to CSV")
    p_export_lc.add_argument("--out", required=True, help="Output CSV path")
    p_export_lc.add_argument("--calibration-name", default=None, help="Filter by CalibrationVersion name")
    p_export_lc.add_argument("--source-id", default=None, help="External source_id from measurement flags")
    p_export_lc.add_argument("--object-id", type=int, default=None, help="Internal object_id")
    p_export_lc.add_argument("--catalog-name", default=None, help="Filter by catalog source namespace in measurement flags")
    p_export_lc.add_argument("--instrument-unit-id", type=int, default=None, help="Filter by instrument unit")
    p_export_lc.add_argument("--limit", type=int, default=None, help="Maximum rows")
    p_export_lc.set_defaults(func=cmd_export_lightcurve)

    p_list_science_lc = sub.add_parser("list-light-curve-objects", help="List objects with science light-curve coverage")
    _add_science_lc_scope_arguments(p_list_science_lc)
    p_list_science_lc.add_argument("--min-points", type=int, default=1, help="Minimum points per object")
    p_list_science_lc.add_argument("--exclude-rejected", action="store_true", help="Count only accepted science points")
    p_list_science_lc.add_argument("--limit", type=int, default=50, help="Maximum objects to show")
    p_list_science_lc.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_list_science_lc.set_defaults(func=cmd_list_light_curve_objects)

    p_science_lc_summary = sub.add_parser("light-curve-summary", help="Summarize science light-curve product coverage and QC")
    _add_science_lc_scope_arguments(p_science_lc_summary)
    p_science_lc_summary.add_argument("--out", default=None, help="Optional JSON report path")
    p_science_lc_summary.add_argument("--format", choices=("table", "json"), default="table", help="Console output format")
    p_science_lc_summary.set_defaults(func=cmd_light_curve_summary)

    p_export_science_lc = sub.add_parser("export-light-curve", help="Export science light-curve product CSV")
    _add_science_lc_scope_arguments(p_export_science_lc)
    p_export_science_lc.add_argument("--out", required=True, help="Output science light-curve CSV path")
    p_export_science_lc.add_argument("--source-id", default=None, help="External source_id from measurement flags")
    p_export_science_lc.add_argument("--object-id", type=int, default=None, help="Internal object_id")
    p_export_science_lc.add_argument("--exclude-rejected", action="store_true", help="Write only accepted science points")
    p_export_science_lc.add_argument("--limit", type=int, default=None, help="Maximum rows")
    p_export_science_lc.add_argument("--format", choices=("table", "json"), default="table", help="Console output format")
    p_export_science_lc.set_defaults(func=cmd_export_science_light_curve)

    p_plot_lc = sub.add_parser("export-lightcurve-plot", help="Export a light-curve diagnostic plot")
    p_plot_lc.add_argument("--out", required=True, help="Output plot path, e.g. exports/lc.png")
    p_plot_lc.add_argument("--calibration-name", default=None, help="Filter by CalibrationVersion name")
    p_plot_lc.add_argument("--source-id", default=None, help="External source_id from measurement flags")
    p_plot_lc.add_argument("--object-id", type=int, default=None, help="Internal object_id")
    p_plot_lc.add_argument("--catalog-name", default=None, help="Filter by catalog source namespace in measurement flags")
    p_plot_lc.add_argument("--instrument-unit-id", type=int, default=None, help="Filter by instrument unit")
    p_plot_lc.add_argument("--limit", type=int, default=None, help="Maximum rows")
    p_plot_lc.add_argument(
        "--x",
        choices=(
            "mjd", "mjd_zero", "mjd_frac",
            "bjd_tdb", "bjd_tdb_zero", "bjd_tdb_frac",
            "bmjd_tdb", "bmjd_tdb_zero", "bmjd_tdb_frac",
            "frame_id", "measurement_id",
        ),
        default="mjd_zero",
        help="X axis, default: mjd_zero",
    )
    p_plot_lc.add_argument(
        "--y",
        choices=("mag_cal", "mag_inst", "flux", "local_zp_correction"),
        default="mag_cal",
        help="Y axis, default: mag_cal",
    )
    p_plot_lc.add_argument("--title", default=None, help="Plot title; default generated from catalog/source id")
    p_plot_lc.add_argument("--show-stats", action="store_true", help="Annotate plot with N/mean/median/RMS/MAD")
    p_plot_lc.add_argument("--dpi", type=int, default=150, help="Output DPI for raster formats")
    p_plot_lc.add_argument("--no-errors", action="store_true", help="Do not draw error bars")
    p_plot_lc.add_argument("--no-invert-y", action="store_true", help="Do not invert magnitude axis")
    p_plot_lc.add_argument("--observatory", default="KAO", help="Known observatory for BJD_TDB conversion; default: KAO")
    p_plot_lc.add_argument("--observatory-lat-deg", type=float, default=None, help="Custom observatory latitude for BJD_TDB")
    p_plot_lc.add_argument("--observatory-lon-deg", type=float, default=None, help="Custom observatory longitude east-positive for BJD_TDB")
    p_plot_lc.add_argument("--observatory-height-m", type=float, default=None, help="Custom observatory height for BJD_TDB")
    p_plot_lc.set_defaults(func=cmd_export_lightcurve_plot)



    p_find_obj_frames = sub.add_parser("find-object-frames", help="List frames/measurements for one catalog object")
    p_find_obj_frames.add_argument("--calibration-name", required=True, help="CalibrationVersion name")
    p_find_obj_frames.add_argument("--catalog-name", required=True, help="Catalog namespace in measurement flags")
    p_find_obj_frames.add_argument("--source-id", default=None, help="External catalog source_id")
    p_find_obj_frames.add_argument("--object-id", type=int, default=None, help="Internal object_id")
    p_find_obj_frames.add_argument("--instrument-unit-id", type=int, default=None, help="Filter by instrument unit")
    p_find_obj_frames.add_argument("--sort", choices=("time", "brightest", "faintest", "frame_id"), default="time", help="Sort output rows")
    p_find_obj_frames.add_argument("--limit", type=int, default=None, help="Maximum rows")
    p_find_obj_frames.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_find_obj_frames.set_defaults(func=cmd_find_object_frames)

    p_cutout = sub.add_parser("export-object-cutout", help="Export FITS image cutout around one forced-photometry object")
    p_cutout.add_argument("--calibration-name", required=True, help="CalibrationVersion name")
    p_cutout.add_argument("--catalog-name", required=True, help="Catalog namespace in measurement flags")
    p_cutout.add_argument("--source-id", default=None, help="External catalog source_id")
    p_cutout.add_argument("--object-id", type=int, default=None, help="Internal object_id")
    p_cutout.add_argument("--frame-id", type=int, required=True, help="Frame ID to cut out")
    p_cutout.add_argument("--size-pix", type=int, default=80, help="Cutout size in pixels")
    p_cutout.add_argument("--out", required=True, help="Output PNG path")
    p_cutout.add_argument("--no-detections", action="store_true", help="Do not overlay source detections")
    p_cutout.add_argument("--no-aperture", action="store_true", help="Do not draw aperture circle")
    p_cutout.add_argument("--vmin-percentile", type=float, default=1.0, help="Image display lower percentile")
    p_cutout.add_argument("--vmax-percentile", type=float, default=99.5, help="Image display upper percentile")
    p_cutout.add_argument("--cmap", default="gray", help="Matplotlib colormap")
    p_cutout.add_argument("--dpi", type=int, default=140, help="Output image DPI")
    p_cutout.set_defaults(func=cmd_export_object_cutout)



    p_detect = sub.add_parser("detect-sources", help="Detect image sources in selected frames using a simple NumPy local-maxima detector")
    p_detect.add_argument("--frame-id", type=int, action="append", help="Specific frame_id; may be repeated")
    p_detect.add_argument("--instrument", default=None, help="Filter frames by instrument")
    p_detect.add_argument("--instrument-unit-id", type=int, default=None, help="Filter frames by instrument unit")
    p_detect.add_argument("--all-wcs-statuses", action="store_true", help="Do not require wcs_status=HEADER_WCS_OK")
    p_detect.add_argument("--threshold-sigma", type=float, default=5.0, help="Detection threshold over robust background sigma")
    p_detect.add_argument("--max-sources-per-frame", type=int, default=2000, help="Maximum detections per frame")
    p_detect.add_argument("--min-separation-pix", type=float, default=5.0, help="Non-max suppression radius")
    p_detect.add_argument("--aperture-radius-pix", type=float, default=3.0, help="Aperture radius used for rough detection flux")
    p_detect.add_argument("--annulus-inner-pix", type=float, default=8.0, help="Background annulus inner radius")
    p_detect.add_argument("--annulus-outer-pix", type=float, default=12.0, help="Background annulus outer radius")
    p_detect.add_argument("--centroid-box-radius-pix", type=int, default=3, help="Half-size of centroid COM box")
    p_detect.add_argument("--replace-existing", action="store_true", help="Delete previous detections for selected frames/algorithm before inserting")
    p_detect.add_argument("--limit-frames", type=int, default=None, help="Limit selected frames")
    p_detect.add_argument("--dry-run", action="store_true", help="Detect without writing source_detections")
    p_detect.set_defaults(func=cmd_detect_sources)

    p_det_summary = sub.add_parser("source-detection-summary", help="Print source detection counts by instrument unit and status")
    p_det_summary.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_det_summary.set_defaults(func=cmd_source_detection_summary)

    p_show_det = sub.add_parser("show-source-detections", help="Print stored source detections")
    p_show_det.add_argument("--frame-id", type=int, default=None, help="Filter by frame_id")
    p_show_det.add_argument("--instrument", default=None, help="Filter by instrument")
    p_show_det.add_argument("--instrument-unit-id", type=int, default=None, help="Filter by instrument unit")
    p_show_det.add_argument("--limit", type=int, default=20, help="Maximum rows")
    p_show_det.add_argument("--offset", type=int, default=0, help="Rows to skip")
    p_show_det.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_show_det.set_defaults(func=cmd_show_source_detections)

    p_gaia = sub.add_parser("query-gaia-for-frames", help="Query Gaia DR3 for selected frame footprints and import into catalog_sources")
    p_gaia.add_argument("--catalog-name", default="gaia_dr3", help="catalog_sources.catalog_name to write")
    p_gaia.add_argument("--frame-id", type=int, action="append", help="Specific frame_id; may be repeated")
    p_gaia.add_argument("--instrument", default=None, help="Filter frames by instrument")
    p_gaia.add_argument("--instrument-unit-id", type=int, default=None, help="Filter frames by instrument unit")
    p_gaia.add_argument("--all-wcs-statuses", action="store_true", help="Do not require wcs_status=HEADER_WCS_OK")
    p_gaia.add_argument("--margin-arcmin", type=float, default=1.0, help="Extra cone radius margin around frame footprint")
    p_gaia.add_argument("--mag-limit", type=float, default=None, help="Optional Gaia G magnitude upper limit")
    p_gaia.add_argument("--max-rows-per-frame", type=int, default=50000, help="Gaia TOP N per query")
    p_gaia.add_argument("--query-mode", choices=("per_frame", "frame_union"), default="per_frame", help="Gaia query strategy")
    p_gaia.add_argument("--max-radius-deg", type=float, default=None, help="Skip a Gaia cone if its radius exceeds this value")
    p_gaia.add_argument("--gaia-timeout-sec", type=float, default=120.0, help="Best-effort astroquery timeout")
    p_gaia.add_argument("--match", action="store_true", help="After import, project catalog into selected frames")
    p_gaia.add_argument("--match-margin-pix", type=float, default=2.0, help="Frame projection margin if --match")
    p_gaia.add_argument("--limit-frames", type=int, default=None, help="Limit selected frames")
    p_gaia.add_argument("--dry-run", action="store_true", help="Query but do not write to DB")
    p_gaia.set_defaults(func=cmd_query_gaia_for_frames)

    p_match_det = sub.add_parser(
        "match-detections-to-catalog",
        help="Cross-match source_detections with projected catalog_sources and write frame QC",
    )
    p_match_det.add_argument("--catalog-name", required=True, help="catalog_sources.catalog_name to match against")
    p_match_det.add_argument("--frame-id", type=int, action="append", help="Specific frame_id; may be repeated")
    p_match_det.add_argument("--instrument", default=None, help="Filter frames by instrument")
    p_match_det.add_argument("--instrument-unit-id", type=int, default=None, help="Filter frames by instrument unit")
    p_match_det.add_argument("--all-wcs-statuses", action="store_true", help="Do not require wcs_status=HEADER_WCS_OK")
    p_match_det.add_argument("--detection-algorithm", default="numpy_localmax_v1", help="source_detections.algorithm to use")
    p_match_det.add_argument("--radius-arcsec", type=float, default=2.0, help="Maximum sky separation for match")
    p_match_det.add_argument("--mag-key", default="G", help="Reference magnitude key in catalog_sources.ref_mags")
    p_match_det.add_argument("--min-snr", type=float, default=None, help="Optional minimum detection S/N")
    p_match_det.add_argument("--limit-frames", type=int, default=None, help="Limit selected frames")
    p_match_det.add_argument("--replace-existing", action="store_true", help="Delete existing matches for selected frames/catalog/algorithm before writing")
    p_match_det.add_argument("--dry-run", action="store_true", help="Match and compute QC without writing DB rows")
    p_match_det.set_defaults(func=cmd_match_detections_to_catalog)

    p_cleanup_det_matches = sub.add_parser(
        "cleanup-duplicate-detection-matches",
        help="Remove stale duplicate detection-catalog matches at frame/source/catalog grain",
    )
    p_cleanup_det_matches.add_argument("--catalog-name", default=None, help="Restrict cleanup to a catalog namespace")
    p_cleanup_det_matches.add_argument("--instrument-unit-id", type=int, default=None, help="Restrict cleanup by instrument unit")
    p_cleanup_det_matches.add_argument("--algorithm", default="nearest_sky_v1", help="Detection-catalog match algorithm")
    p_cleanup_det_matches.add_argument("--keep-orphans", action="store_true", help="Do not delete orphan match rows")
    p_cleanup_det_matches.add_argument("--dry-run", action="store_true", help="Count rows that would be deleted without writing")
    p_cleanup_det_matches.set_defaults(func=cmd_cleanup_duplicate_detection_matches)

    p_match_summary = sub.add_parser("source-catalog-match-summary", help="Print detection-catalog match counts and residual statistics")
    p_match_summary.add_argument("--catalog-name", default=None, help="Filter by catalog_name")
    p_match_summary.add_argument("--instrument-unit-id", type=int, default=None, help="Filter by instrument unit")
    p_match_summary.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_match_summary.set_defaults(func=cmd_source_catalog_match_summary)

    p_show_matches = sub.add_parser("show-source-catalog-matches", help="Print stored detection-catalog matches")
    p_show_matches.add_argument("--catalog-name", default=None, help="Filter by catalog_name")
    p_show_matches.add_argument("--frame-id", type=int, default=None, help="Filter by frame_id")
    p_show_matches.add_argument("--instrument", default=None, help="Filter by instrument")
    p_show_matches.add_argument("--instrument-unit-id", type=int, default=None, help="Filter by instrument unit")
    p_show_matches.add_argument("--limit", type=int, default=20, help="Maximum rows")
    p_show_matches.add_argument("--offset", type=int, default=0, help="Rows to skip")
    p_show_matches.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_show_matches.set_defaults(func=cmd_show_source_catalog_matches)

    p_frame_qc = sub.add_parser("frame-qc-summary", help="Print per-frame QC from detection-catalog crossmatch")
    p_frame_qc.add_argument("--catalog-name", default=None, help="Filter by catalog_name")
    p_frame_qc.add_argument("--instrument", default=None, help="Filter by instrument")
    p_frame_qc.add_argument("--instrument-unit-id", type=int, default=None, help="Filter by instrument unit")
    p_frame_qc.add_argument("--limit", type=int, default=50, help="Maximum rows")
    p_frame_qc.add_argument("--offset", type=int, default=0, help="Rows to skip")
    p_frame_qc.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_frame_qc.set_defaults(func=cmd_frame_qc_summary)

    p_object_qc_summary = sub.add_parser("object-qc-summary", help="Print object_qc flag counts and basic statistics")
    p_object_qc_summary.add_argument("--calibration-name", default=None, help="Filter by CalibrationVersion name")
    p_object_qc_summary.add_argument("--catalog-name", default=None, help="Filter by catalog namespace")
    p_object_qc_summary.add_argument("--instrument-unit-id", type=int, default=None, help="Filter by instrument unit")
    p_object_qc_summary.add_argument("--algorithm", default="object_qc_v1", help="object_qc.algorithm")
    p_object_qc_summary.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_object_qc_summary.set_defaults(func=cmd_object_qc_summary)

    p_show_object_qc = sub.add_parser("show-object-qc", help="Print stored object_qc rows")
    p_show_object_qc.add_argument("--calibration-name", default=None, help="Filter by CalibrationVersion name")
    p_show_object_qc.add_argument("--catalog-name", default=None, help="Filter by catalog namespace")
    p_show_object_qc.add_argument("--instrument-unit-id", type=int, default=None, help="Filter by instrument unit")
    p_show_object_qc.add_argument("--algorithm", default="object_qc_v1", help="object_qc.algorithm")
    p_show_object_qc.add_argument("--flag", action="append", default=[], help="Show only rows with this flag; may be repeated")
    p_show_object_qc.add_argument("--exclude-flag", action="append", default=[], help="Exclude rows with this flag; may be repeated")
    p_show_object_qc.add_argument("--limit", type=int, default=50, help="Maximum rows")
    p_show_object_qc.add_argument("--offset", type=int, default=0, help="Rows to skip")
    p_show_object_qc.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_show_object_qc.set_defaults(func=cmd_show_object_qc)

    p_status = sub.add_parser("db-summary", help="Print basic DB counters")
    p_status.set_defaults(func=cmd_db_summary)

    return parser


def cmd_init_db(args: argparse.Namespace) -> int:
    init_db(args.database_url)
    print("Database schema created or already exists.")
    return 0


def cmd_reset_db(args: argparse.Namespace) -> int:
    if not args.yes:
        print("Refusing to reset database without --yes.")
        return 2
    reset_db(args.database_url)
    print("Database schema dropped and recreated.")
    return 0


def cmd_index_archive(args: argparse.Namespace) -> int:
    profile = InstrumentProfile.from_yaml(args.instrument)
    Session = make_session_factory(args.database_url)
    with Session() as session:
        result = index_archive(
            session=session,
            root=Path(args.root),
            profile=profile,
            pattern=args.pattern,
            recursive=args.recursive,
            dry_run=args.dry_run,
            limit=args.limit,
        )
    print(
        "Index result: "
        f"discovered={result.discovered}, inserted={result.inserted}, "
        f"skipped_existing={result.skipped_existing}, failed={result.failed}"
    )
    return 0 if result.failed == 0 else 2



def cmd_check_wcs(args: argparse.Namespace) -> int:
    Session = make_session_factory(args.database_url)
    with Session() as session:
        result = check_frames_wcs(
            session=session,
            frame_ids=args.frame_id,
            instrument=args.instrument,
            instrument_unit_id=args.instrument_unit_id,
            current_status=None if args.all_statuses else args.status,
            limit=args.limit,
            dry_run=args.dry_run,
            update_center=not args.keep_indexed_center,
            record_events=args.record_events,
        )
    print(
        "WCS check result: "
        f"selected={result.selected}, checked={result.checked}, updated={result.updated}, "
        f"ok={result.ok}, missing={result.missing}, bad={result.bad}, "
        f"failed={result.failed}, skipped_missing_file={result.skipped_missing_file}"
    )
    return 0 if result.failed == 0 and result.bad == 0 else 2


def cmd_show_frames(args: argparse.Namespace) -> int:
    from sqlalchemy import select
    from grandgousier.db.models import Frame, FrameStatus, InstrumentUnit

    Session = make_session_factory(args.database_url)
    with Session() as session:
        stmt = (
            select(
                Frame.frame_id,
                Frame.instrument_unit_id,
                InstrumentUnit.instrument,
                InstrumentUnit.site,
                InstrumentUnit.tube,
                InstrumentUnit.camera,
                InstrumentUnit.filter_name,
                InstrumentUnit.observing_mode,
                Frame.object_name,
                Frame.date_obs,
                Frame.exptime,
                Frame.ra_center_deg,
                Frame.dec_center_deg,
                Frame.wcs_status,
                Frame.flat_status,
                Frame.dark_status,
                Frame.sexcat_status,
                Frame.processing_status,
            )
            .outerjoin(InstrumentUnit, InstrumentUnit.instrument_unit_id == Frame.instrument_unit_id)
            .order_by(Frame.frame_id)
        )
        if args.instrument:
            stmt = stmt.where(Frame.instrument == args.instrument)
        if args.instrument_unit_id is not None:
            stmt = stmt.where(Frame.instrument_unit_id == args.instrument_unit_id)
        if args.filter_name:
            stmt = stmt.where(Frame.filter_name == args.filter_name)
        if args.object_name:
            stmt = stmt.where(Frame.object_name.ilike(f"%{args.object_name}%"))
        if args.wcs_status:
            stmt = stmt.where(Frame.wcs_status == args.wcs_status)
        if args.processing_status:
            try:
                status = FrameStatus(args.processing_status)
            except ValueError:
                print(f"Unknown processing status: {args.processing_status}", file=sys.stderr)
                return 2
            stmt = stmt.where(Frame.processing_status == status)
        if args.offset:
            stmt = stmt.offset(args.offset)
        if args.limit is not None:
            stmt = stmt.limit(args.limit)

        rows = []
        if args.calibration_name:
            stmt = stmt.where(CalibrationVersion.name == args.calibration_name)
        if args.instrument_unit_id is not None:
            stmt = stmt.where(InstrumentUnit.instrument_unit_id == int(args.instrument_unit_id))

        for row in session.execute(stmt).all():
            d = dict(row._mapping)
            d["date_obs"] = _format_dt(d.get("date_obs"))
            d["processing_status"] = _enum_value(d.get("processing_status"))
            d["ra_center_deg"] = _round_float(d.get("ra_center_deg"), 8)
            d["dec_center_deg"] = _round_float(d.get("dec_center_deg"), 8)
            rows.append(d)

    columns = [
        "frame_id",
        "instrument_unit_id",
        "instrument",
        "site",
        "tube",
        "camera",
        "filter_name",
        "observing_mode",
        "object_name",
        "date_obs",
        "exptime",
        "ra_center_deg",
        "dec_center_deg",
        "wcs_status",
        "flat_status",
        "dark_status",
        "sexcat_status",
        "processing_status",
    ]
    _print_rows(rows, columns, args.format)
    return 0


def cmd_frame_summary(args: argparse.Namespace) -> int:
    from sqlalchemy import func, select
    from grandgousier.db.models import Frame, InstrumentUnit

    Session = make_session_factory(args.database_url)
    with Session() as session:
        rows = []
        stmt = (
            select(
                InstrumentUnit.instrument_unit_id,
                InstrumentUnit.instrument,
                InstrumentUnit.site,
                InstrumentUnit.tube,
                InstrumentUnit.camera,
                InstrumentUnit.filter_name,
                InstrumentUnit.observing_mode,
                func.count(Frame.frame_id).label("n_frames"),
                func.min(Frame.date_obs).label("date_min"),
                func.max(Frame.date_obs).label("date_max"),
            )
            .outerjoin(Frame, Frame.instrument_unit_id == InstrumentUnit.instrument_unit_id)
            .group_by(
                InstrumentUnit.instrument_unit_id,
                InstrumentUnit.instrument,
                InstrumentUnit.site,
                InstrumentUnit.tube,
                InstrumentUnit.camera,
                InstrumentUnit.filter_name,
                InstrumentUnit.observing_mode,
            )
            .order_by(InstrumentUnit.instrument_unit_id)
        )
        if args.calibration_name:
            stmt = stmt.where(CalibrationVersion.name == args.calibration_name)
        if args.instrument_unit_id is not None:
            stmt = stmt.where(InstrumentUnit.instrument_unit_id == int(args.instrument_unit_id))

        for row in session.execute(stmt).all():
            d = dict(row._mapping)
            d["date_min"] = _format_dt(d.get("date_min"))
            d["date_max"] = _format_dt(d.get("date_max"))
            rows.append(d)

    columns = [
        "instrument_unit_id",
        "instrument",
        "site",
        "tube",
        "camera",
        "filter_name",
        "observing_mode",
        "n_frames",
        "date_min",
        "date_max",
    ]
    _print_rows(rows, columns, args.format)
    return 0



def cmd_import_catalog(args: argparse.Namespace) -> int:
    from grandgousier.catalogs.import_catalog import import_catalog_sources

    mag_cols = _parse_name_column_args(args.mag_col)
    color_cols = _parse_name_column_args(args.color_col)
    Session = make_session_factory(args.database_url)
    with Session() as session:
        result = import_catalog_sources(
            session,
            path=args.file,
            catalog_name=args.catalog_name,
            ra_col=args.ra_col,
            dec_col=args.dec_col,
            source_id_col=args.source_id_col,
            pmra_col=args.pmra_col,
            pmdec_col=args.pmdec_col,
            epoch_col=args.epoch_col,
            mag_cols=mag_cols,
            color_cols=color_cols,
            limit=args.limit,
            dry_run=args.dry_run,
        )
    print(
        "Catalog import result: "
        f"catalog={result.catalog_name}, read_rows={result.read_rows}, valid_rows={result.valid_rows}, "
        f"inserted_or_updated={result.inserted_or_updated}, "
        f"skipped_bad_coordinates={result.skipped_bad_coordinates}, dry_run={result.dry_run}"
    )
    return 0


def cmd_catalog_summary(args: argparse.Namespace) -> int:
    from sqlalchemy import func, select
    from grandgousier.db.models import CatalogSource, FrameCatalogLink

    Session = make_session_factory(args.database_url)
    with Session() as session:
        source_counts = (
            select(
                CatalogSource.catalog_name,
                func.count(CatalogSource.catalog_source_id).label("n_sources"),
                func.min(CatalogSource.ra_deg).label("ra_min"),
                func.max(CatalogSource.ra_deg).label("ra_max"),
                func.min(CatalogSource.dec_deg).label("dec_min"),
                func.max(CatalogSource.dec_deg).label("dec_max"),
            )
            .group_by(CatalogSource.catalog_name)
            .order_by(CatalogSource.catalog_name)
        )
        link_counts = dict(
            session.execute(
                select(CatalogSource.catalog_name, func.count(FrameCatalogLink.frame_catalog_link_id))
                .join(FrameCatalogLink, FrameCatalogLink.catalog_source_id == CatalogSource.catalog_source_id)
                .group_by(CatalogSource.catalog_name)
            ).all()
        )
        rows = []
        for row in session.execute(source_counts).all():
            d = dict(row._mapping)
            d["n_frame_links"] = link_counts.get(d["catalog_name"], 0)
            d["ra_min"] = _round_float(d.get("ra_min"), 6)
            d["ra_max"] = _round_float(d.get("ra_max"), 6)
            d["dec_min"] = _round_float(d.get("dec_min"), 6)
            d["dec_max"] = _round_float(d.get("dec_max"), 6)
            rows.append(d)
    columns = ["catalog_name", "n_sources", "n_frame_links", "ra_min", "ra_max", "dec_min", "dec_max"]
    _print_rows(rows, columns, args.format)
    return 0


def cmd_show_catalog_sources(args: argparse.Namespace) -> int:
    from sqlalchemy import select
    from grandgousier.db.models import CatalogSource

    Session = make_session_factory(args.database_url)
    with Session() as session:
        stmt = select(
            CatalogSource.catalog_source_id,
            CatalogSource.catalog_name,
            CatalogSource.source_id,
            CatalogSource.ra_deg,
            CatalogSource.dec_deg,
            CatalogSource.pmra_masyr,
            CatalogSource.pmdec_masyr,
            CatalogSource.ref_mags,
            CatalogSource.colors,
        ).order_by(CatalogSource.catalog_source_id)
        if args.catalog_name:
            stmt = stmt.where(CatalogSource.catalog_name == args.catalog_name)
        if args.offset:
            stmt = stmt.offset(args.offset)
        if args.limit is not None:
            stmt = stmt.limit(args.limit)
        rows = []
        if args.calibration_name:
            stmt = stmt.where(CalibrationVersion.name == args.calibration_name)
        if args.instrument_unit_id is not None:
            stmt = stmt.where(InstrumentUnit.instrument_unit_id == int(args.instrument_unit_id))

        for row in session.execute(stmt).all():
            d = dict(row._mapping)
            d["ra_deg"] = _round_float(d.get("ra_deg"), 8)
            d["dec_deg"] = _round_float(d.get("dec_deg"), 8)
            rows.append(d)
        if args.offset:
            rows = rows[int(args.offset):]
        if args.limit is not None:
            rows = rows[: int(args.limit)]
    columns = [
        "catalog_source_id",
        "catalog_name",
        "source_id",
        "ra_deg",
        "dec_deg",
        "pmra_masyr",
        "pmdec_masyr",
        "ref_mags",
        "colors",
    ]
    _print_rows(rows, columns, args.format)
    return 0




def cmd_create_reference_photometry_csv(args: argparse.Namespace) -> int:
    from grandgousier.catalogs.reference_photometry import create_reference_photometry_csv

    min_mag = _parse_name_float_args(args.min_mag)
    max_mag = _parse_name_float_args(args.max_mag)
    max_mag_err = _parse_name_float_args(args.max_mag_err)

    Session = make_session_factory(args.database_url)
    with Session() as session:
        result = create_reference_photometry_csv(
            session,
            out_path=args.out,
            provider=args.provider,
            field_id=args.field_id,
            series_id=args.series_id,
            ra_deg=args.ra,
            dec_deg=args.dec,
            radius_arcmin=args.radius_arcmin,
            overwrite=args.overwrite,
            limit=args.limit,
            require_bands=args.require_band,
            min_mag=min_mag,
            max_mag=max_mag,
            max_mag_err=max_mag_err,
            min_detections=args.min_detections,
            use_remote_filters=not args.no_remote_filters,
        )
    print(
        "Reference photometry CSV result: "
        f"provider={result.provider}, path={result.path}, field_id={result.field_id}, "
        f"series_id={result.series_id}, ra={result.ra_deg}, dec={result.dec_deg}, "
        f"radius_arcmin={result.radius_arcmin}, remote_rows={result.raw_rows}, "
        f"standardized_rows={result.standardized_rows}, written_rows={result.written_rows}, "
        f"skipped_rows={result.skipped_rows}, filtered_rows={result.filtered_rows}"
    )
    if result.filter_summary:
        print(f"Filters: {json.dumps(result.filter_summary, ensure_ascii=False)}")
    if args.print_import_command and result.import_command:
        print("\nSuggested import command:")
        print(result.import_command)
    return 0


def cmd_import_reference_photometry(args: argparse.Namespace) -> int:
    from grandgousier.catalogs.reference_photometry import import_reference_photometry

    mag_cols = _parse_name_column_args(args.mag_col)
    mag_err_cols = _parse_name_column_args(args.mag_err_col)
    color_cols = _parse_name_column_args(args.color_col)
    quality_cols = _parse_name_column_args(args.quality_col)

    Session = make_session_factory(args.database_url)
    with Session() as session:
        result = import_reference_photometry(
            session,
            path=args.file,
            catalog_name=args.catalog_name,
            catalog_version=args.catalog_version,
            photometric_system=args.photometric_system,
            source_priority=args.source_priority,
            ra_col=args.ra_col,
            dec_col=args.dec_col,
            source_id_col=args.source_id_col,
            epoch_col=args.epoch_col,
            mag_cols=mag_cols,
            mag_err_cols=mag_err_cols,
            color_cols=color_cols,
            quality_cols=quality_cols,
            match_to_catalog=args.match_to_catalog,
            max_match_arcsec=args.max_match_arcsec,
            limit=args.limit,
            dry_run=args.dry_run,
        )
    print(
        "Reference photometry import result: "
        f"catalog={result.catalog_name}, read_rows={result.read_rows}, valid_rows={result.valid_rows}, "
        f"inserted_or_updated={result.inserted_or_updated}, matched_to_gaia={result.matched_to_gaia}, "
        f"unmatched_to_gaia={result.unmatched_to_gaia}, "
        f"skipped_bad_coordinates={result.skipped_bad_coordinates}, dry_run={result.dry_run}"
    )
    return 0


def cmd_show_reference_photometry_catalogs(args: argparse.Namespace) -> int:
    from grandgousier.catalogs.reference_photometry import summarize_reference_photometry_catalogs

    Session = make_session_factory(args.database_url)
    with Session() as session:
        rows = summarize_reference_photometry_catalogs(session)
    for row in rows:
        for key in ("n_sources", "n_g", "n_r", "n_i", "n_z", "n_y", "n_b", "n_v", "n_bp", "n_rp", "n_g_gaia", "n_matched_gaia"):
            row[key] = int(row.get(key) or 0)
    columns = [
        "catalog_name",
        "catalog_version",
        "photometric_system",
        "source_priority",
        "n_sources",
        "n_matched_gaia",
        "n_g",
        "n_r",
        "n_i",
        "n_z",
        "n_y",
        "n_b",
        "n_v",
        "n_bp",
        "n_rp",
        "n_g_gaia",
    ]
    _print_rows(rows, columns, args.format)
    return 0


def cmd_show_object_reference_photometry(args: argparse.Namespace) -> int:
    from grandgousier.catalogs.reference_photometry import find_object_reference_photometry

    Session = make_session_factory(args.database_url)
    with Session() as session:
        rows = find_object_reference_photometry(
            session,
            object_id=args.object_id,
            identity_catalog_name=args.identity_catalog_name,
            source_id=args.source_id,
            limit=args.limit,
        )
    columns = [
        "catalog_name",
        "catalog_source_id",
        "gaia_catalog_name",
        "gaia_source_id",
        "distance_to_gaia_arcsec",
        "mag_g",
        "mag_r",
        "mag_i",
        "mag_z",
        "mag_y",
        "mag_b",
        "mag_v",
        "mag_bp",
        "mag_rp",
        "mag_g_gaia",
        "colors",
        "quality_flags",
    ]
    _print_rows(rows, columns, args.format)
    return 0


def cmd_diagnose_reference_photometry(args: argparse.Namespace) -> int:
    from grandgousier.catalogs.reference_photometry import diagnose_reference_photometry

    Session = make_session_factory(args.database_url)
    with Session() as session:
        diag = diagnose_reference_photometry(
            session,
            field_id=args.field_id,
            identity_catalog_name=args.identity_catalog_name,
            reference_catalog_name=args.reference_catalog_name,
        )
    if args.format == "json":
        payload = {
            "field_id": diag.field_id,
            "identity_catalog_name": diag.identity_catalog_name,
            "reference_catalog_name": diag.catalog_name,
            "n_identity_sources": diag.n_identity_sources,
            "n_sources_with_any_reference": diag.n_sources_with_any_reference,
            "catalogs": diag.catalog_rows,
            "bands": diag.band_rows,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0

    summary_rows = [
        {
            "field_id": diag.field_id,
            "identity_catalog_name": diag.identity_catalog_name,
            "reference_catalog_name": diag.catalog_name or "*",
            "n_identity_sources": diag.n_identity_sources,
            "n_sources_with_any_reference": diag.n_sources_with_any_reference,
        }
    ]
    _print_rows(
        summary_rows,
        ["field_id", "identity_catalog_name", "reference_catalog_name", "n_identity_sources", "n_sources_with_any_reference"],
        args.format,
    )
    if args.format == "table":
        print("\nCatalog coverage:")
    _print_rows(
        diag.catalog_rows,
        ["catalog_name", "n_reference_rows", "n_identity_sources", "coverage_fraction", "median_distance_to_gaia_arcsec"],
        args.format,
    )
    if args.format == "table":
        print("\nBand coverage:")
    _print_rows(
        diag.band_rows,
        ["catalog_name", "band", "n_band_sources", "coverage_fraction"],
        args.format,
    )
    return 0


def cmd_compute_color_calibration(args: argparse.Namespace) -> int:
    from grandgousier.calibration.color_calibration import compute_color_calibration_diagnostics

    exclude_object_ids = _parse_int_list_arg(args.exclude_object_ids)
    exclude_object_ids.extend(int(v) for v in (args.exclude_object_id or []))
    min_reference_mag = _parse_optional_float_or_mapping(args.min_reference_mag, args.reference_band, "min-reference-mag")
    max_reference_mag = _parse_optional_float_or_mapping(args.max_reference_mag, args.reference_band, "max-reference-mag")
    min_color = _parse_optional_float_or_mapping(args.min_color, args.color_index, "min-color")
    max_color = _parse_optional_float_or_mapping(args.max_color, args.color_index, "max-color")

    Session = make_session_factory(args.database_url)
    with Session() as session:
        result = compute_color_calibration_diagnostics(
            session,
            calibration_name=args.calibration_name,
            identity_catalog_name=args.identity_catalog_name,
            reference_catalog_name=args.reference_catalog_name,
            reference_band=args.reference_band,
            color_index=args.color_index,
            series_id=args.series_id,
            out_dir=args.out_dir,
            min_measurements_per_object=args.min_measurements_per_object,
            min_stars_per_frame=args.min_stars_per_frame,
            max_measurement_mag_err=args.max_measurement_mag_err,
            max_reference_mag_err=args.max_reference_mag_err,
            min_reference_mag=min_reference_mag,
            max_reference_mag=max_reference_mag,
            min_color=min_color,
            max_color=max_color,
            include_flagged_calibrators=args.include_flagged_calibrators,
            exclude_object_ids=exclude_object_ids,
            clip_sigma=args.clip_sigma,
            n_iterations=args.n_iterations,
            exclude_saturated=not args.include_saturated,
            limit=args.limit,
        )

    payload = result.summary_dict()
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0

    rows = [
        {
            "calibration_name": result.calibration_name,
            "series_id": result.series_id,
            "reference_catalog": result.reference_catalog_name,
            "reference_band": result.reference_band,
            "color_index": result.color_index,
            "n_input": result.n_measurements_input,
            "n_ref_obj": result.n_reference_objects,
            "n_candidate_obj": result.n_calibrator_candidate_objects,
            "n_excl_qc": result.n_excluded_by_object_qc,
            "n_excl_total": result.n_excluded_objects,
            "n_used_meas": result.n_used_measurements,
            "n_used_obj": result.n_used_objects,
            "n_used_frames": result.n_used_frames,
            "color0": _round_float(result.color0, 5),
            "current_slope": _round_float(result.current_color_slope, 6),
            "current_rms_before": _round_float(result.current_residual_rms, 6),
            "current_rms_after": _round_float(result.current_residual_after_color_rms, 6),
            "frame_color_term": _round_float(result.frame_model_color_term, 6),
            "frame_model_rms": _round_float(result.frame_model_residual_rms, 6),
            "frame_model_mad": _round_float(result.frame_model_residual_mad, 6),
            "summary_json": result.summary_json,
        }
    ]
    _print_rows(
        rows,
        [
            "calibration_name",
            "series_id",
            "reference_catalog",
            "reference_band",
            "color_index",
            "n_input",
            "n_ref_obj",
            "n_candidate_obj",
            "n_excl_qc",
            "n_excl_total",
            "n_used_meas",
            "n_used_obj",
            "n_used_frames",
            "color0",
            "current_slope",
            "current_rms_before",
            "current_rms_after",
            "frame_color_term",
            "frame_model_rms",
            "frame_model_mad",
            "summary_json",
        ],
        "table",
    )
    if result.stars_csv or result.frames_csv:
        print(f"stars_csv={result.stars_csv}")
        print(f"frames_csv={result.frames_csv}")
    if result.warnings:
        print("Warnings:")
        for warning in result.warnings:
            print(f"  {warning}")
    return 0



def cmd_apply_color_calibration(args: argparse.Namespace) -> int:
    from grandgousier.calibration.color_calibration import apply_color_calibration

    exclude_object_ids = _parse_int_list_arg(args.exclude_object_ids)
    exclude_object_ids.extend(int(v) for v in (args.exclude_object_id or []))
    min_reference_mag = _parse_optional_float_or_mapping(args.min_reference_mag, args.reference_band, "min-reference-mag")
    max_reference_mag = _parse_optional_float_or_mapping(args.max_reference_mag, args.reference_band, "max-reference-mag")
    min_color = _parse_optional_float_or_mapping(args.min_color, args.color_index, "min-color")
    max_color = _parse_optional_float_or_mapping(args.max_color, args.color_index, "max-color")

    Session = make_session_factory(args.database_url)
    with Session() as session:
        result = apply_color_calibration(
            session,
            source_calibration_name=args.source_calibration_name,
            target_calibration_name=args.target_calibration_name,
            identity_catalog_name=args.identity_catalog_name,
            reference_catalog_name=args.reference_catalog_name,
            reference_band=args.reference_band,
            color_index=args.color_index,
            series_id=args.series_id,
            out_dir=args.out_dir,
            min_measurements_per_object=args.min_measurements_per_object,
            min_stars_per_frame=args.min_stars_per_frame,
            max_measurement_mag_err=args.max_measurement_mag_err,
            max_reference_mag_err=args.max_reference_mag_err,
            min_reference_mag=min_reference_mag,
            max_reference_mag=max_reference_mag,
            min_color=min_color,
            max_color=max_color,
            include_flagged_calibrators=args.include_flagged_calibrators,
            exclude_object_ids=exclude_object_ids,
            clip_sigma=args.clip_sigma,
            n_iterations=args.n_iterations,
            exclude_saturated=not args.include_saturated,
            limit=args.limit,
            dry_run=args.dry_run,
        )

    payload = result.summary_dict()
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0

    rows = [
        {
            "source_calibration": result.source_calibration_name,
            "target_calibration": result.target_calibration_name,
            "series_id": result.series_id,
            "reference_catalog": result.reference_catalog_name,
            "reference_band": result.reference_band,
            "color_index": result.color_index,
            "target_cv_id": result.target_calibration_version_id,
            "n_calib_meas": result.n_used_calibrator_measurements,
            "n_calib_obj": result.n_used_calibrator_objects,
            "n_frames_zp": result.n_frames_with_zp,
            "checked": result.checked_measurements,
            "calibrated": result.calibrated,
            "inserted_or_updated": result.inserted_or_updated,
            "deleted_existing": result.deleted_existing,
            "skipped_no_color": result.skipped_no_color,
            "skipped_no_zp": result.skipped_no_frame_zp,
            "color0": _round_float(result.color0, 5),
            "color_term": _round_float(result.color_term, 6),
            "model_rms": _round_float(result.frame_model_residual_rms, 6),
            "model_mad": _round_float(result.frame_model_residual_mad, 6),
            "dry_run": result.dry_run,
        }
    ]
    _print_rows(
        rows,
        [
            "source_calibration",
            "target_calibration",
            "series_id",
            "reference_catalog",
            "reference_band",
            "color_index",
            "target_cv_id",
            "n_calib_meas",
            "n_calib_obj",
            "n_frames_zp",
            "checked",
            "calibrated",
            "inserted_or_updated",
            "deleted_existing",
            "skipped_no_color",
            "skipped_no_zp",
            "color0",
            "color_term",
            "model_rms",
            "model_mad",
            "dry_run",
        ],
        "table",
    )
    if result.diagnostics_summary_json or result.diagnostics_stars_csv or result.diagnostics_frames_csv:
        print(f"summary_json={result.diagnostics_summary_json}")
        print(f"stars_csv={result.diagnostics_stars_csv}")
        print(f"frames_csv={result.diagnostics_frames_csv}")
    if result.warnings:
        print("Warnings:")
        for warning in result.warnings:
            print(f"  {warning}")
    return 0


def cmd_match_catalog_to_frames(args: argparse.Namespace) -> int:
    from grandgousier.catalogs.frame_match import match_catalog_to_frames

    Session = make_session_factory(args.database_url)
    with Session() as session:
        result = match_catalog_to_frames(
            session,
            catalog_name=args.catalog_name,
            frame_ids=args.frame_id,
            instrument=args.instrument,
            instrument_unit_id=args.instrument_unit_id,
            only_wcs_ok=not args.all_wcs_statuses,
            margin_pix=args.margin_pix,
            limit_frames=args.limit_frames,
            limit_sources=args.limit_sources,
            dry_run=args.dry_run,
        )
    print(
        "Catalog-frame match result: "
        f"selected_frames={result.selected_frames}, checked_frames={result.checked_frames}, "
        f"selected_sources={result.selected_sources}, linked={result.linked}, "
        f"skipped_missing_file={result.skipped_missing_file}, failed_frames={result.failed_frames}, "
        f"dry_run={result.dry_run}"
    )
    return 0 if result.failed_frames == 0 else 2



def cmd_run_forced_photometry(args: argparse.Namespace) -> int:
    from grandgousier.photometry.forced_aperture import run_forced_aperture_photometry

    Session = make_session_factory(args.database_url)
    with Session() as session:
        result = run_forced_aperture_photometry(
            session,
            catalog_name=args.catalog_name,
            frame_ids=args.frame_id,
            instrument=args.instrument,
            instrument_unit_id=args.instrument_unit_id,
            only_wcs_ok=not args.all_wcs_statuses,
            aperture_radius_pix=args.aperture_radius_pix,
            annulus_inner_pix=args.annulus_inner_pix,
            annulus_outer_pix=args.annulus_outer_pix,
            zero_point=args.zero_point,
            saturation_level_adu=args.saturation_level_adu,
            calibration_name=args.calibration_name,
            limit_frames=args.limit_frames,
            limit_links=args.limit_links,
            update_processing_status=args.update_processing_status,
            dry_run=args.dry_run,
            insert_batch_size=args.bulk_insert_batch_size,
        )
    print(
        "Forced photometry result: "
        f"selected_frames={result.selected_frames}, checked_frames={result.checked_frames}, "
        f"selected_links={result.selected_links}, measured={result.measured}, "
        f"inserted_or_updated={result.inserted_or_updated}, insert_batches={result.insert_batches}, "
        f"skipped_missing_file={result.skipped_missing_file}, failed_frames={result.failed_frames}, "
        f"failed_measurements={result.failed_measurements}, "
        f"calibration_version_id={result.calibration_version_id}, "
        f"calibration_name={result.calibration_name}, dry_run={result.dry_run}"
    )
    return 0 if result.failed_frames == 0 else 2


def cmd_measurement_summary(args: argparse.Namespace) -> int:
    from sqlalchemy import func, select
    from grandgousier.db.models import CalibrationVersion, Frame, InstrumentUnit, Measurement

    Session = make_session_factory(args.database_url)
    with Session() as session:
        rows = []
        stmt = (
            select(
                CalibrationVersion.calibration_version_id,
                CalibrationVersion.name.label("calibration_name"),
                InstrumentUnit.instrument_unit_id,
                InstrumentUnit.instrument,
                InstrumentUnit.site,
                InstrumentUnit.tube,
                InstrumentUnit.camera,
                InstrumentUnit.filter_name,
                InstrumentUnit.observing_mode,
                func.count(Measurement.measurement_id).label("n_measurements"),
                func.count(Measurement.flux).label("n_flux"),
                func.count(Measurement.mag_inst).label("n_mag_inst"),
                func.count(Measurement.mag_cal).label("n_mag_cal"),
                func.min(Measurement.mag_inst).label("mag_inst_min"),
                func.max(Measurement.mag_inst).label("mag_inst_max"),
                func.min(Measurement.mag_cal).label("mag_cal_min"),
                func.max(Measurement.mag_cal).label("mag_cal_max"),
            )
            .select_from(Measurement)
            .join(Frame, Frame.frame_id == Measurement.frame_id)
            .outerjoin(InstrumentUnit, InstrumentUnit.instrument_unit_id == Frame.instrument_unit_id)
            .outerjoin(CalibrationVersion, CalibrationVersion.calibration_version_id == Measurement.calibration_version_id)
            .group_by(
                CalibrationVersion.calibration_version_id,
                CalibrationVersion.name,
                InstrumentUnit.instrument_unit_id,
                InstrumentUnit.instrument,
                InstrumentUnit.site,
                InstrumentUnit.tube,
                InstrumentUnit.camera,
                InstrumentUnit.filter_name,
                InstrumentUnit.observing_mode,
            )
            .order_by(InstrumentUnit.instrument_unit_id, CalibrationVersion.calibration_version_id)
        )
        if args.calibration_name:
            stmt = stmt.where(CalibrationVersion.name == args.calibration_name)
        if args.instrument_unit_id is not None:
            stmt = stmt.where(InstrumentUnit.instrument_unit_id == int(args.instrument_unit_id))

        for row in session.execute(stmt).all():
            d = dict(row._mapping)
            d["mag_inst_min"] = _round_float(d.get("mag_inst_min"), 5)
            d["mag_inst_max"] = _round_float(d.get("mag_inst_max"), 5)
            d["mag_cal_min"] = _round_float(d.get("mag_cal_min"), 5)
            d["mag_cal_max"] = _round_float(d.get("mag_cal_max"), 5)
            rows.append(d)
        if args.offset:
            rows = rows[int(args.offset):]
        if args.limit is not None:
            rows = rows[: int(args.limit)]
    columns = [
        "calibration_version_id",
        "calibration_name",
        "instrument_unit_id",
        "instrument",
        "site",
        "tube",
        "camera",
        "filter_name",
        "observing_mode",
        "n_measurements",
        "n_flux",
        "n_mag_inst",
        "n_mag_cal",
        "mag_inst_min",
        "mag_inst_max",
        "mag_cal_min",
        "mag_cal_max",
    ]
    _print_rows(rows, columns, args.format)
    return 0


def cmd_show_measurements(args: argparse.Namespace) -> int:
    from sqlalchemy import select
    from grandgousier.db.models import CalibrationVersion, Frame, InstrumentUnit, Measurement, Object

    Session = make_session_factory(args.database_url)
    with Session() as session:
        stmt = (
            select(
                Measurement.measurement_id,
                Measurement.frame_id,
                Measurement.object_id,
                CalibrationVersion.name.label("calibration_name"),
                InstrumentUnit.instrument,
                InstrumentUnit.tube,
                InstrumentUnit.camera,
                InstrumentUnit.filter_name,
                Frame.object_name.label("frame_object"),
                Frame.date_obs,
                Object.ra_deg,
                Object.dec_deg,
                Measurement.x,
                Measurement.y,
                Measurement.flux,
                Measurement.flux_err,
                Measurement.mag_inst,
                Measurement.mag_cal,
                Measurement.mag_err,
                Measurement.local_background,
                Measurement.local_zp_correction,
                Measurement.aperture_radius_pix,
                Measurement.flags,
            )
            .select_from(Measurement)
            .join(Frame, Frame.frame_id == Measurement.frame_id)
            .join(Object, Object.object_id == Measurement.object_id)
            .outerjoin(InstrumentUnit, InstrumentUnit.instrument_unit_id == Frame.instrument_unit_id)
            .outerjoin(CalibrationVersion, CalibrationVersion.calibration_version_id == Measurement.calibration_version_id)
            .order_by(Measurement.measurement_id)
            .offset(args.offset)
            .limit(args.limit)
        )
        if getattr(args, "calibration_name", None):
            stmt = stmt.where(CalibrationVersion.name == args.calibration_name)
        if args.instrument:
            stmt = stmt.where(Frame.instrument == args.instrument)
        if args.instrument_unit_id is not None:
            stmt = stmt.where(Frame.instrument_unit_id == args.instrument_unit_id)
        if args.object_name:
            stmt = stmt.where(Frame.object_name.ilike(f"%{args.object_name}%"))
        rows = []
        if args.calibration_name:
            stmt = stmt.where(CalibrationVersion.name == args.calibration_name)
        if args.instrument_unit_id is not None:
            stmt = stmt.where(InstrumentUnit.instrument_unit_id == int(args.instrument_unit_id))

        for row in session.execute(stmt).all():
            d = dict(row._mapping)
            if args.catalog_name and (d.get("flags") or {}).get("catalog_name") != args.catalog_name:
                continue
            d["date_obs"] = _format_dt(d.get("date_obs"))
            for key, nd in (
                ("ra_deg", 8),
                ("dec_deg", 8),
                ("x", 3),
                ("y", 3),
                ("flux", 3),
                ("flux_err", 3),
                ("mag_inst", 5),
                ("mag_cal", 5),
                ("mag_err", 5),
                ("local_background", 3),
                ("local_zp_correction", 5),
                ("aperture_radius_pix", 2),
            ):
                d[key] = _round_float(d.get(key), nd)
            flags = d.get("flags") or {}
            d["catalog_name"] = flags.get("catalog_name")
            d["source_id"] = flags.get("source_id")
            d["flags"] = {k: v for k, v in flags.items() if k not in {"catalog_name", "source_id", "catalog_source_id"}}
            rows.append(d)
        if args.offset:
            rows = rows[int(args.offset):]
        if args.limit is not None:
            rows = rows[: int(args.limit)]
    columns = [
        "measurement_id",
        "frame_id",
        "object_id",
        "calibration_name",
        "instrument",
        "tube",
        "camera",
        "filter_name",
        "frame_object",
        "date_obs",
        "catalog_name",
        "source_id",
        "x",
        "y",
        "flux",
        "flux_err",
        "mag_inst",
        "mag_cal",
        "mag_err",
        "local_background",
        "local_zp_correction",
        "aperture_radius_pix",
        "flags",
    ]
    _print_rows(rows, columns, args.format)
    return 0



def cmd_calibrate_measurements(args: argparse.Namespace) -> int:
    from grandgousier.calibration.preliminary import calibrate_measurements_from_frame_qc

    Session = make_session_factory(args.database_url)
    with Session() as session:
        result = calibrate_measurements_from_frame_qc(
            session,
            source_calibration_name=args.source_calibration_name,
            target_calibration_name=args.target_calibration_name,
            catalog_name=args.catalog_name,
            source_catalog_name=args.source_catalog_name,
            instrument_unit_id=args.instrument_unit_id,
            frame_ids=args.frame_id,
            qc_algorithm=args.qc_algorithm,
            min_zp_n=args.min_zp_n,
            max_zp_mad=args.max_zp_mad,
            min_n_matched=args.min_n_matched,
            dry_run=args.dry_run,
            insert_batch_size=args.bulk_insert_batch_size,
        )
    print(
        "Calibration result: "
        f"source_calibration_version_id={result.source_calibration_version_id}, "
        f"target_calibration_version_id={result.target_calibration_version_id}, "
        f"target_calibration_name={result.target_calibration_name}, "
        f"selected_measurements={result.selected_measurements}, "
        f"checked_measurements={result.checked_measurements}, calibrated={result.calibrated}, "
        f"inserted_or_updated={result.inserted_or_updated}, insert_batches={result.insert_batches}, skipped_other_catalog={result.skipped_other_catalog}, "
        f"skipped_no_qc={result.skipped_no_qc}, skipped_bad_qc={result.skipped_bad_qc}, "
        f"skipped_no_mag_inst={result.skipped_no_mag_inst}, "
        f"skipped_nonfinite={result.skipped_nonfinite}, dry_run={result.dry_run}"
    )
    return 0


def cmd_ensemble_calibrate(args: argparse.Namespace) -> int:
    from grandgousier.calibration.ensemble import ensemble_calibrate_measurements

    max_ref_rms = None if args.max_ref_rms is not None and args.max_ref_rms < 0 else args.max_ref_rms
    max_ref_mad = None if args.max_ref_mad is not None and args.max_ref_mad < 0 else args.max_ref_mad
    Session = make_session_factory(args.database_url)
    with Session() as session:
        result = ensemble_calibrate_measurements(
            session,
            source_calibration_name=args.source_calibration_name,
            target_calibration_name=args.target_calibration_name,
            catalog_name=args.catalog_name,
            instrument_unit_id=args.instrument_unit_id,
            frame_ids=args.frame_id,
            min_measurements=args.min_measurements,
            min_ref_measurements=args.min_ref_measurements,
            min_ref_stars_per_frame=args.min_ref_stars_per_frame,
            max_ref_rms=max_ref_rms,
            max_ref_mad=max_ref_mad,
            min_mag=args.min_mag,
            max_mag=args.max_mag,
            n_iterations=args.n_iterations,
            prefer_mag_cal=not args.use_mag_inst,
            dry_run=args.dry_run,
            insert_batch_size=args.bulk_insert_batch_size,
        )
    print(
        "Ensemble calibration result: "
        f"source_calibration_version_id={result.source_calibration_version_id}, "
        f"target_calibration_version_id={result.target_calibration_version_id}, "
        f"target_calibration_name={result.target_calibration_name}, "
        f"selected_measurements={result.selected_measurements}, checked_measurements={result.checked_measurements}, "
        f"selected_objects={result.selected_objects}, reference_candidates={result.reference_candidates}, "
        f"usable_reference_objects={result.usable_reference_objects}, selected_frames={result.selected_frames}, "
        f"frames_with_offsets={result.frames_with_offsets}, calibrated={result.calibrated}, "
        f"inserted_or_updated={result.inserted_or_updated}, insert_batches={result.insert_batches}, skipped_other_catalog={result.skipped_other_catalog}, "
        f"skipped_no_input_mag={result.skipped_no_input_mag}, skipped_no_frame_offset={result.skipped_no_frame_offset}, "
        f"skipped_nonfinite={result.skipped_nonfinite}, frame_offset_median={_round_float(result.frame_offset_median, 6)}, "
        f"frame_offset_mad={_round_float(result.frame_offset_mad, 6)}, dry_run={result.dry_run}"
    )
    return 0


def cmd_ensemble_frame_summary(args: argparse.Namespace) -> int:
    from grandgousier.calibration.ensemble import build_ensemble_frame_summary

    Session = make_session_factory(args.database_url)
    with Session() as session:
        rows = build_ensemble_frame_summary(
            session,
            calibration_name=args.calibration_name,
            catalog_name=args.catalog_name,
            instrument_unit_id=args.instrument_unit_id,
            limit=args.limit,
        )
    for row in rows:
        for key, nd in (
            ("mjd", 6),
            ("ensemble_offset", 6),
            ("ensemble_scatter_mad", 6),
            ("mag_cal_median", 5),
            ("mag_cal_mad", 5),
        ):
            row[key] = _round_float(row.get(key), nd)
    columns = [
        "frame_id",
        "instrument_unit_id",
        "instrument",
        "filter_name",
        "date_obs",
        "mjd",
        "n_measurements",
        "ensemble_offset",
        "ensemble_n_ref",
        "ensemble_scatter_mad",
        "mag_cal_median",
        "mag_cal_mad",
    ]
    _print_rows(rows, columns, args.format)
    return 0




def cmd_find_object_frames(args: argparse.Namespace) -> int:
    from grandgousier.visualization.object_cutout import find_object_frames

    if args.source_id is None and args.object_id is None:
        raise SystemExit("Provide --source-id or --object-id")

    Session = make_session_factory(args.database_url)
    with Session() as session:
        rows_obj = find_object_frames(
            session,
            calibration_name=args.calibration_name,
            catalog_name=args.catalog_name,
            source_id=args.source_id,
            object_id=args.object_id,
            instrument_unit_id=args.instrument_unit_id,
            sort=args.sort,
            limit=args.limit,
        )

    rows = []
    for r in rows_obj:
        d = r.to_dict()
        for key, nd in (
            ("mjd", 6),
            ("x", 3),
            ("y", 3),
            ("flux", 3),
            ("flux_err", 3),
            ("mag_inst", 5),
            ("mag_cal", 5),
            ("mag_err", 5),
            ("local_zp_correction", 5),
            ("det_sep_arcsec", 4),
            ("det_snr", 3),
            ("det_x", 3),
            ("det_y", 3),
        ):
            d[key] = _round_float(d.get(key), nd)
        rows.append(d)

    columns = [
        "measurement_id",
        "frame_id",
        "object_id",
        "source_id",
        "catalog_name",
        "date_obs",
        "mjd",
        "filter_name",
        "x",
        "y",
        "flux",
        "flux_err",
        "mag_inst",
        "mag_cal",
        "mag_err",
        "local_zp_correction",
        "det_sep_arcsec",
        "det_snr",
        "det_x",
        "det_y",
        "raw_path",
    ]
    _print_rows(rows, columns, args.format)
    return 0


def cmd_export_object_cutout(args: argparse.Namespace) -> int:
    from grandgousier.visualization.object_cutout import export_object_cutout

    if args.source_id is None and args.object_id is None:
        raise SystemExit("Provide --source-id or --object-id")

    Session = make_session_factory(args.database_url)
    with Session() as session:
        result = export_object_cutout(
            session,
            calibration_name=args.calibration_name,
            catalog_name=args.catalog_name,
            source_id=args.source_id,
            object_id=args.object_id,
            frame_id=args.frame_id,
            out=args.out,
            size_pix=args.size_pix,
            show_detections=not args.no_detections,
            aperture=not args.no_aperture,
            vmin_percentile=args.vmin_percentile,
            vmax_percentile=args.vmax_percentile,
            cmap=args.cmap,
            dpi=args.dpi,
        )
    print(
        "Exported object cutout: "
        f"path={result['path']}, frame_id={result['frame_id']}, "
        f"measurement_id={result['measurement_id']}, object_id={result['object_id']}, "
        f"source_id={result['source_id']}, x={_round_float(result['x'], 3)}, y={_round_float(result['y'], 3)}, "
        f"cutout=[{result['x0']}:{result['x1']}, {result['y0']}:{result['y1']}], "
        f"detections_plotted={result['detections_plotted']}, matched_detection_id={result['matched_detection_id']}"
    )
    return 0


def cmd_aperture_zp_calibrate(args: argparse.Namespace) -> int:
    from grandgousier.calibration.aperture_zp import aperture_zp_calibrate

    max_mag_err = None if args.max_mag_err is not None and args.max_mag_err < 0 else args.max_mag_err

    Session = make_session_factory(args.database_url)
    with Session() as session:
        result = aperture_zp_calibrate(
            session,
            source_calibration_name=args.source_calibration_name,
            target_calibration_name=args.target_calibration_name,
            catalog_name=args.catalog_name,
            instrument_unit_id=args.instrument_unit_id,
            mag_key=args.mag_key,
            min_ref_stars_per_frame=args.min_ref_stars_per_frame,
            max_mag_err=max_mag_err,
            max_zp_mad=args.max_zp_mad,
            clip_sigma=args.clip_sigma,
            n_iterations=args.n_iterations,
            exclude_saturated=not args.include_saturated,
            dry_run=args.dry_run,
            insert_batch_size=args.bulk_insert_batch_size,
        )

    print(
        "Aperture ZP calibration result: "
        f"source_calibration_version_id={result.source_calibration_version_id}, "
        f"target_calibration_version_id={result.target_calibration_version_id}, "
        f"source_calibration_name={result.source_calibration_name}, "
        f"target_calibration_name={result.target_calibration_name}, "
        f"catalog_name={result.catalog_name}, instrument_unit_id={result.instrument_unit_id}, "
        f"selected_measurements={result.selected_measurements}, "
        f"checked_measurements={result.checked_measurements}, "
        f"selected_frames={result.selected_frames}, frames_with_zp={result.frames_with_zp}, "
        f"calibrated={result.calibrated}, inserted_or_updated={result.inserted_or_updated}, insert_batches={result.insert_batches}, "
        f"skipped_other_catalog={result.skipped_other_catalog}, skipped_no_ref_mag={result.skipped_no_ref_mag}, "
        f"skipped_no_mag_inst={result.skipped_no_mag_inst}, skipped_bad_mag_err={result.skipped_bad_mag_err}, "
        f"skipped_saturated={result.skipped_saturated}, skipped_no_frame_zp={result.skipped_no_frame_zp}, "
        f"skipped_nonfinite={result.skipped_nonfinite}, "
        f"frame_zp_median={_round_float(result.frame_zp_median, 6)}, "
        f"frame_zp_mad={_round_float(result.frame_zp_mad, 6)}, dry_run={result.dry_run}"
    )
    return 0


def cmd_object_summary(args: argparse.Namespace) -> int:
    from grandgousier.calibration.preliminary import build_object_summary

    Session = make_session_factory(args.database_url)
    with Session() as session:
        rows = build_object_summary(
            session,
            calibration_name=args.calibration_name,
            catalog_name=args.catalog_name,
            source_id=args.source_id,
            instrument_unit_id=args.instrument_unit_id,
            limit=args.limit,
        )
    for row in rows:
        for key, nd in (
            ("ra_deg", 8),
            ("dec_deg", 8),
            ("mag_mean", 5),
            ("mag_median", 5),
            ("mag_rms", 5),
            ("mag_mad", 5),
            ("mjd_min", 6),
            ("mjd_max", 6),
        ):
            row[key] = _round_float(row.get(key), nd)
    columns = [
        "object_id",
        "calibration_name",
        "instrument_unit_id",
        "instrument",
        "filter_name",
        "catalog_name",
        "source_id",
        "ra_deg",
        "dec_deg",
        "n_measurements",
        "n_mag_inst",
        "n_mag_cal",
        "mag_mean",
        "mag_median",
        "mag_rms",
        "mag_mad",
        "mjd_min",
        "mjd_max",
    ]
    _print_rows(rows, columns, args.format)
    return 0



def cmd_rms_mag_summary(args: argparse.Namespace) -> int:
    from grandgousier.quality.lightcurve_quality import build_rms_mag_summary, load_object_scatter_rows

    Session = make_session_factory(args.database_url)
    with Session() as session:
        object_rows = load_object_scatter_rows(
            session,
            calibration_name=args.calibration_name,
            catalog_name=args.catalog_name,
            instrument_unit_id=args.instrument_unit_id,
            min_measurements=args.min_measurements,
            mag_key=args.mag_key,
            color_key=args.color_key,
        )
        rows = build_rms_mag_summary(
            object_rows,
            bin_width=args.bin_width,
            min_objects_per_bin=args.min_objects_per_bin,
        )
    for row in rows:
        for key, nd in (
            ("mag_bin_left", 3),
            ("mag_bin_right", 3),
            ("mag_bin_center", 3),
            ("mag_median", 4),
            ("rms_median", 5),
            ("rms_p10", 5),
            ("rms_p90", 5),
            ("mad_median", 5),
            ("rms_over_mag_err", 3),
            ("mad_over_mag_err", 3),
            ("effective_mag_err_median", 5),
            ("intrinsic_variability_score", 3),
            ("mag_err_median", 5),
        ):
            row[key] = _round_float(row.get(key), nd)
    columns = [
        "mag_bin_left",
        "mag_bin_right",
        "mag_bin_center",
        "n_objects",
        "mag_median",
        "rms_median",
        "rms_p10",
        "rms_p90",
        "mad_median",
        "mag_err_median",
    ]
    _print_rows(rows, columns, args.format)
    print(f"objects_used={len(object_rows)}", file=sys.stderr if args.format != "table" else sys.stdout)
    return 0


def cmd_export_rms_mag_plot(args: argparse.Namespace) -> int:
    from grandgousier.quality.lightcurve_quality import load_object_scatter_rows
    from grandgousier.quality.object_quality import filter_object_rows_by_qc, load_object_qc_map
    from grandgousier.visualization.rms_mag_plot import export_rms_mag_plot

    Session = make_session_factory(args.database_url)
    with Session() as session:
        object_rows = load_object_scatter_rows(
            session,
            calibration_name=args.calibration_name,
            catalog_name=args.catalog_name,
            instrument_unit_id=args.instrument_unit_id,
            min_measurements=args.min_measurements,
        )
        if (
            args.exclude_flagged
            or args.exclude_crowded
            or args.exclude_edge
            or args.exclude_saturated
            or args.min_nearest_neighbor_arcsec is not None
        ):
            qc_map = load_object_qc_map(
                session,
                calibration_name=args.calibration_name,
                catalog_name=args.catalog_name,
                instrument_unit_id=args.instrument_unit_id,
            )
            object_rows = filter_object_rows_by_qc(
                object_rows,
                qc_map,
                exclude_flagged=args.exclude_flagged,
                exclude_crowded=args.exclude_crowded,
                exclude_edge=args.exclude_edge,
                exclude_saturated=args.exclude_saturated,
                min_nearest_neighbor_arcsec=args.min_nearest_neighbor_arcsec,
            )
    result = export_rms_mag_plot(
        object_rows,
        args.out,
        title=args.title,
        dpi=args.dpi,
        y_key=args.y,
        bin_width=args.bin_width,
        min_objects_per_bin=args.min_objects_per_bin,
        show_binned=not args.no_binned,
        ylim_max=args.ylim_max,
    )
    print(
        "Exported RMS--magnitude plot: "
        f"path={result.output_path}, objects={result.n_objects}, plotted={result.n_plotted}, "
        f"bins={result.n_bins}, calibration={result.calibration_name}, y={result.y_key}"
    )
    return 0


def cmd_variable_candidate_summary(args: argparse.Namespace) -> int:
    from grandgousier.quality.lightcurve_quality import build_variable_candidates, load_object_scatter_rows
    from grandgousier.quality.object_quality import filter_candidate_dicts_by_qc, load_object_qc_map

    Session = make_session_factory(args.database_url)
    with Session() as session:
        object_rows = load_object_scatter_rows(
            session,
            calibration_name=args.calibration_name,
            catalog_name=args.catalog_name,
            instrument_unit_id=args.instrument_unit_id,
            min_measurements=args.min_measurements,
        )
        rows = build_variable_candidates(
            object_rows,
            bin_width=args.bin_width,
            min_objects_per_bin=args.min_objects_per_bin,
            min_measurements=args.min_measurements,
            min_rms=args.min_rms,
            min_excess=args.min_excess,
            limit=None,
        )
        if (
            args.exclude_flagged
            or args.exclude_crowded
            or args.exclude_edge
            or args.exclude_saturated
            or args.min_nearest_neighbor_arcsec is not None
            or args.max_mag_median is not None
            or args.require_detection_support
            or args.max_ref_mag_delta is not None
            or args.exclude_catalog_outliers
            or args.exclude_aperture_unstable
        ):
            qc_map = load_object_qc_map(
                session,
                calibration_name=args.calibration_name,
                catalog_name=args.catalog_name,
                instrument_unit_id=args.instrument_unit_id,
            )
            rows = filter_candidate_dicts_by_qc(
                rows,
                qc_map,
                exclude_flagged=args.exclude_flagged,
                exclude_crowded=args.exclude_crowded,
                exclude_edge=args.exclude_edge,
                exclude_saturated=args.exclude_saturated,
                min_nearest_neighbor_arcsec=args.min_nearest_neighbor_arcsec,
                max_mag_median=args.max_mag_median,
                require_detection_support=args.require_detection_support,
                max_ref_mag_delta=args.max_ref_mag_delta,
                exclude_catalog_outliers=args.exclude_catalog_outliers,
                exclude_aperture_unstable=args.exclude_aperture_unstable,
            )
        rows = rows[: int(args.limit)] if args.limit is not None else rows
    for row in rows:
        for key, nd in (
            ("ra_deg", 8),
            ("dec_deg", 8),
            ("mag_median", 5),
            ("mag_rms", 5),
            ("mag_mad", 5),
            ("robust_peak_to_peak", 5),
            ("baseline_rms", 5),
            ("rms_excess", 3),
            ("rms_over_mag_err", 3),
            ("mad_over_mag_err", 3),
            ("effective_mag_err_median", 5),
            ("intrinsic_variability_score", 3),
            ("mag_err_median", 5),
            ("ref_mag", 5),
            ("color", 5),
            ("nearest_neighbor_arcsec", 4),
            ("dynamic_blend_arcsec", 4),
            ("dynamic_crowded_arcsec", 4),
            ("separation_index", 4),
            ("crowding_index", 4),
            ("edge_distance_pix_min", 3),
            ("detection_fraction", 3),
            ("detection_frame_fraction", 3),
            ("multiple_detection_frame_ratio", 3),
            ("det_sep_median_arcsec", 4),
            ("mag_ref_delta", 5),
            ("abs_mag_ref_delta", 5),
            ("aperture_min_corr_with_ref", 4),
            ("aperture_max_rms_delta_vs_ref", 5),
            ("aperture_max_abs_median_delta_vs_ref", 5),
        ):
            row[key] = _round_float(row.get(key), nd)
    columns = [
        "object_id",
        "catalog_name",
        "source_id",
        "ra_deg",
        "dec_deg",
        "n_measurements",
        "mag_median",
        "mag_rms",
        "mag_mad",
        "baseline_rms",
        "rms_excess",
        "mag_err_median",
        "ref_mag",
        "color",
        "nearest_neighbor_arcsec",
        "edge_distance_pix_min",
        "detection_fraction",
        "detection_frame_fraction",
        "n_detection_matches",
        "n_detection_match_frames",
        "n_detection_matches_raw",
        "n_extra_detection_matches",
        "n_multiple_detection_frames",
        "multiple_detection_frame_ratio",
        "det_sep_median_arcsec",
        "mag_ref_delta",
        "abs_mag_ref_delta",
        "object_qc_flags",
    ]
    _print_rows(rows, columns, args.format)
    return 0



def cmd_candidate_triage_summary(args: argparse.Namespace) -> int:
    def _none_if_negative(value):
        if value is None:
            return None
        return None if float(value) < 0 else value

    from grandgousier.quality.candidate_triage import (
        build_candidate_triage,
        candidate_triage_columns,
        export_candidate_triage_csv,
    )

    Session = make_session_factory(args.database_url)
    with Session() as session:
        result = build_candidate_triage(
            session,
            calibration_name=args.calibration_name,
            catalog_name=args.catalog_name,
            instrument_unit_id=args.instrument_unit_id,
            min_measurements=args.min_measurements,
            bin_width=args.bin_width,
            min_objects_per_bin=args.min_objects_per_bin,
            min_rms=args.min_rms,
            min_excess=args.min_excess,
            exclude_flagged=args.exclude_flagged,
            exclude_crowded=args.exclude_crowded,
            exclude_edge=args.exclude_edge,
            exclude_saturated=args.exclude_saturated,
            exclude_catalog_outliers=args.exclude_catalog_outliers,
            reject_aperture_unstable=args.reject_aperture_unstable,
            require_detection_support=args.require_detection_support,
            min_nearest_neighbor_arcsec=args.min_nearest_neighbor_arcsec,
            max_mag_median=args.max_mag_median,
            max_ref_mag_delta=args.max_ref_mag_delta,
            qc_algorithm=args.qc_algorithm,
            limit=None,
            enable_intrinsic_variability_candidates=args.enable_intrinsic_variability_candidates,
            min_intrinsic_rms=_none_if_negative(args.min_intrinsic_rms),
            min_rms_over_mag_err=_none_if_negative(args.min_rms_over_mag_err),
            min_mad_over_mag_err=_none_if_negative(args.min_mad_over_mag_err),
            intrinsic_mag_err_floor=_none_if_negative(args.intrinsic_mag_err_floor),
            min_robust_peak_to_peak=_none_if_negative(args.min_robust_peak_to_peak),
            enable_low_amplitude_intrinsic_candidates=args.enable_low_amplitude_intrinsic_candidates,
            low_amplitude_min_measurements=args.low_amplitude_min_measurements,
            low_amplitude_min_intrinsic_rms=_none_if_negative(args.low_amplitude_min_intrinsic_rms),
            low_amplitude_min_rms_over_mag_err=_none_if_negative(args.low_amplitude_min_rms_over_mag_err),
            low_amplitude_min_mad_over_mag_err=_none_if_negative(args.low_amplitude_min_mad_over_mag_err),
            low_amplitude_min_robust_peak_to_peak=_none_if_negative(args.low_amplitude_min_robust_peak_to_peak),
        )

    if args.out:
        count = export_candidate_triage_csv(result.rows, args.out)
        print(f"Exported candidate triage CSV: path={args.out}, rows={count}")

    rows = result.rows[: int(args.limit)] if args.limit is not None else result.rows
    for row in rows:
        for key, nd in (
            ("ra_deg", 8),
            ("dec_deg", 8),
            ("mag_median", 5),
            ("mag_rms", 5),
            ("mag_mad", 5),
            ("robust_peak_to_peak", 5),
            ("baseline_rms", 5),
            ("rms_excess", 3),
            ("rms_over_mag_err", 3),
            ("mad_over_mag_err", 3),
            ("effective_mag_err_median", 5),
            ("intrinsic_variability_score", 3),
            ("mag_err_median", 5),
            ("ref_mag", 5),
            ("color", 5),
            ("nearest_neighbor_arcsec", 4),
            ("edge_distance_pix_min", 3),
            ("detection_fraction", 3),
            ("detection_frame_fraction", 3),
            ("multiple_detection_frame_ratio", 3),
            ("det_sep_median_arcsec", 4),
            ("det_snr_median", 3),
            ("det_snr_max", 3),
            ("mag_ref_delta", 5),
            ("abs_mag_ref_delta", 5),
            ("aperture_min_corr_with_ref", 4),
            ("aperture_max_rms_delta_vs_ref", 5),
            ("aperture_max_abs_median_delta_vs_ref", 5),
        ):
            row[key] = _round_float(row.get(key), nd)

    summary = result.summary
    if args.format == "table":
        rejection_counts = summary.get("rejection_counts") or {}
        counts_text = ", ".join(f"{k}={v}" for k, v in rejection_counts.items()) if rejection_counts else "none"
        print(
            "Summary: "
            f"objects_used={summary.get('objects_used')}, raw_candidates={summary.get('raw_candidates')}, "
            f"accepted={summary.get('accepted')}, rejected={summary.get('rejected')}, "
            f"rejection_counts=[{counts_text}]"
        )

    _print_rows(rows, candidate_triage_columns(), args.format)
    return 0






def cmd_import_events(args: argparse.Namespace) -> int:
    from grandgousier.events.registry import import_events_csv

    Session = make_session_factory(args.database_url)
    with Session() as session:
        result = import_events_csv(
            session,
            path=args.file,
            default_event_type=args.default_event_type,
            default_source_catalog=args.default_source_catalog,
            dry_run=args.dry_run,
        )
    print(
        "Import events result: "
        f"read_rows={result.read_rows}, valid_rows={result.valid_rows}, "
        f"inserted_or_updated={result.inserted_or_updated}, skipped={result.skipped}, dry_run={result.dry_run}"
    )
    return 0


def cmd_show_events(args: argparse.Namespace) -> int:
    from grandgousier.events.registry import EVENT_SHOW_COLUMNS, list_events

    Session = make_session_factory(args.database_url)
    with Session() as session:
        rows = list_events(
            session,
            event_type=args.event_type,
            source_catalog=args.source_catalog,
            status=args.status,
            target=args.target,
            limit=args.limit,
        )
    rows = [_round_event_row(row) for row in rows]
    _print_rows(rows, EVENT_SHOW_COLUMNS, args.format)
    return 0


def cmd_match_events_to_objects(args: argparse.Namespace) -> int:
    from grandgousier.events.registry import match_events_to_objects

    Session = make_session_factory(args.database_url)
    with Session() as session:
        result = match_events_to_objects(
            session,
            event_type=args.event_type,
            event_uid=args.event_uid,
            source_catalog=args.source_catalog,
            external_catalog=args.external_catalog,
            external_id_column_preference=args.external_id_column_preference,
            coordinate_radius_arcsec=args.coordinate_radius_arcsec,
            dry_run=args.dry_run,
        )
    print(
        "Match events result: "
        f"selected_events={result.selected_events}, matched_links={result.matched_links}, "
        f"unmatched_events={result.unmatched_events}, inserted_or_updated={result.inserted_or_updated}, "
        f"dry_run={result.dry_run}"
    )
    return 0


def cmd_show_event_object_links(args: argparse.Namespace) -> int:
    from grandgousier.events.registry import EVENT_LINK_COLUMNS, list_event_object_links

    Session = make_session_factory(args.database_url)
    with Session() as session:
        rows = list_event_object_links(
            session,
            event_type=args.event_type,
            event_uid=args.event_uid,
            limit=args.limit,
        )
    rows = [_round_event_row(row) for row in rows]
    _print_rows(rows, EVENT_LINK_COLUMNS, args.format)
    return 0


def cmd_event_coverage_summary(args: argparse.Namespace) -> int:
    from grandgousier.events.registry import (
        EVENT_COVERAGE_COLUMNS,
        build_event_coverage_summary,
        export_event_coverage_csv,
    )

    Session = make_session_factory(args.database_url)
    with Session() as session:
        result = build_event_coverage_summary(
            session,
            calibration_name=args.calibration_name,
            event_type=args.event_type,
            event_uid=args.event_uid,
            catalog_name=args.catalog_name,
            instrument_unit_id=args.instrument_unit_id,
            window_padding_factor=args.window_padding_factor,
            pre_margin_hours=args.pre_margin_hours,
            post_margin_hours=args.post_margin_hours,
            event_window_source=args.event_window_source,
            min_depth_total_snr=args.min_depth_total_snr,
            min_event_points=args.min_event_points,
            min_pre_baseline_points=args.min_pre_baseline_points,
            min_post_baseline_points=args.min_post_baseline_points,
            bad_object_qc_flags=_split_comma_list(args.bad_object_qc_flags),
            ignore_object_qc=args.ignore_object_qc,
            write_frame_coverage=args.write_frame_coverage,
            limit=args.limit,
        )
    if args.out:
        count = export_event_coverage_csv(result.rows, args.out)
        print(f"Exported event coverage CSV: path={args.out}, rows={count}")
    rows = [_round_event_row(row) for row in result.rows]
    if args.format == "json":
        print(json.dumps({"summary": result.summary, "rows": rows}, ensure_ascii=False, indent=2))
    else:
        if args.format == "table":
            print(
                "Summary: "
                f"events_selected={result.summary.get('events_selected')}, "
                f"coverage_rows={result.summary.get('coverage_rows')}, "
                f"covered_with_baseline={result.summary.get('covered_with_baseline')}, "
                f"full_baseline={result.summary.get('full_baseline')}, "
                f"no_baseline={result.summary.get('no_baseline')}, "
                f"partial_coverage={result.summary.get('partial_coverage')}, "
                f"good_precision={result.summary.get('good_coverage_good_precision')}, "
                f"low_snr={result.summary.get('good_coverage_low_snr')}, "
                f"bad_object_qc={result.summary.get('good_coverage_bad_object_qc')}, "
                f"no_match={result.summary.get('no_match')}, no_points={result.summary.get('no_points')}"
            )
        _print_rows(rows, EVENT_COVERAGE_COLUMNS, args.format)
    return 0


def cmd_show_event_coverage(args: argparse.Namespace) -> int:
    from collections import Counter

    from grandgousier.events.registry import (
        EVENT_FRAME_COVERAGE_COLUMNS,
        export_event_frame_coverage_csv,
        list_event_frame_coverage,
    )

    Session = make_session_factory(args.database_url)
    with Session() as session:
        rows = list_event_frame_coverage(
            session,
            event_type=args.event_type,
            event_uid=args.event_uid,
            calibration_name=args.calibration_name,
            instrument_unit_id=args.instrument_unit_id,
            object_id=args.object_id,
            time_relation=args.time_relation,
            limit=args.limit,
        )
    if args.out:
        count = export_event_frame_coverage_csv(rows, args.out)
        print(f"Exported stored event frame coverage CSV: path={args.out}, rows={count}")
    rows = [_round_event_row(row) for row in rows]
    if args.format == "json":
        counts = Counter(row.get("time_relation") for row in rows)
        print(json.dumps({"summary": {"rows": len(rows), "time_relation_counts": dict(counts)}, "rows": rows}, ensure_ascii=False, indent=2))
    else:
        if args.format == "table":
            counts = Counter(row.get("time_relation") for row in rows)
            counts_text = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()) if k is not None) or "-"
            print(f"Summary: rows={len(rows)}, time_relation_counts={counts_text}")
        _print_rows(rows, EVENT_FRAME_COVERAGE_COLUMNS, args.format)
    return 0




def cmd_match_events_to_detections(args: argparse.Namespace) -> int:
    from grandgousier.events.registry import match_events_to_detections

    Session = make_session_factory(args.database_url)
    with Session() as session:
        result = match_events_to_detections(
            session,
            event_type=args.event_type,
            event_uid=args.event_uid,
            instrument_unit_id=args.instrument_unit_id,
            radius_arcsec=args.radius_arcsec,
            min_snr=args.min_snr,
            event_window_source=args.event_window_source,
            pre_margin_hours=args.pre_margin_hours,
            post_margin_hours=args.post_margin_hours,
            window_padding_factor=args.window_padding_factor,
            include_outside_window=args.include_outside_window,
            limit_events=args.limit_events,
            dry_run=args.dry_run,
        )
    print(
        "Event-detection match result: "
        f"selected_events={result.selected_events}, candidate_detections={result.candidate_detections}, "
        f"linked_detections={result.linked_detections}, catalog_matched_detections={result.catalog_matched_detections}, "
        f"catalog_unmatched_detections={result.catalog_unmatched_detections}, "
        f"skipped_no_position={result.skipped_no_position}, skipped_no_time_window={result.skipped_no_time_window}, "
        f"inserted_or_updated={result.inserted_or_updated}, dry_run={result.dry_run}"
    )
    return 0


def cmd_show_event_detection_links(args: argparse.Namespace) -> int:
    from collections import Counter

    from grandgousier.events.registry import (
        EVENT_DETECTION_LINK_COLUMNS,
        export_event_detection_links_csv,
        list_event_detection_links,
    )

    catalog_matched = None
    if args.catalog_matched == "yes":
        catalog_matched = True
    elif args.catalog_matched == "no":
        catalog_matched = False

    Session = make_session_factory(args.database_url)
    with Session() as session:
        rows = list_event_detection_links(
            session,
            event_type=args.event_type,
            event_uid=args.event_uid,
            instrument_unit_id=args.instrument_unit_id,
            time_relation=args.time_relation,
            catalog_matched=catalog_matched,
            min_snr=args.min_snr,
            limit=args.limit,
        )
    if args.out:
        count = export_event_detection_links_csv(rows, args.out)
        print(f"Exported event detection links CSV: path={args.out}, rows={count}")
    rows = [_round_event_row(row) for row in rows]
    if args.format == "json":
        rel_counts = Counter(row.get("time_relation") for row in rows)
        catalog_counts = Counter("catalog_matched" if row.get("catalog_matched") else "catalog_unmatched" for row in rows)
        print(json.dumps({"summary": {"rows": len(rows), "time_relation_counts": dict(rel_counts), "catalog_match_counts": dict(catalog_counts)}, "rows": rows}, ensure_ascii=False, indent=2))
    else:
        if args.format == "table":
            rel_counts = Counter(row.get("time_relation") for row in rows)
            catalog_counts = Counter("matched" if row.get("catalog_matched") else "unmatched" for row in rows)
            rel_text = ", ".join(f"{k}={v}" for k, v in sorted(rel_counts.items()) if k is not None) or "-"
            cat_text = ", ".join(f"{k}={v}" for k, v in sorted(catalog_counts.items())) or "-"
            print(f"Summary: rows={len(rows)}, time_relation_counts={rel_text}, catalog_match_counts={cat_text}")
        _print_rows(rows, EVENT_DETECTION_LINK_COLUMNS, args.format)
    return 0



def cmd_build_field_series(args: argparse.Namespace) -> int:
    from grandgousier.archive.fields_series import build_field_series

    Session = make_session_factory(args.database_url)
    with Session() as session:
        result = build_field_series(
            session,
            instrument_unit_id=args.instrument_unit_id,
            max_field_separation_arcmin=args.max_field_separation_arcmin,
            max_series_gap_minutes=args.max_series_gap_minutes,
            only_unassigned=args.only_unassigned,
            only_science=args.only_science,
            min_frames_per_series=args.min_frames_per_series,
            dry_run=args.dry_run,
        )
    if args.format == "json":
        print(json.dumps({"summary": result.summary(), "series": result.rows}, ensure_ascii=False, indent=2))
    else:
        print(
            "Field/series build result: "
            f"selected_frames={result.selected_frames}, created_fields={result.created_fields}, "
            f"created_series={result.created_series}, linked_frames={result.linked_frames}, "
            f"dry_run={result.dry_run}"
        )
        if result.selected_frames == 0 and result.frame_selection_diagnostics:
            print(f"Frame selection diagnostics: {result.frame_selection_diagnostics}")
        _print_rows(result.rows, FIELD_SERIES_BUILD_COLUMNS, args.format)
    return 0


def cmd_show_fields(args: argparse.Namespace) -> int:
    from grandgousier.archive.fields_series import list_fields

    Session = make_session_factory(args.database_url)
    with Session() as session:
        rows = list_fields(session, limit=args.limit, status=args.status, name_like=args.name)
    _print_rows(rows, FIELD_COLUMNS, args.format)
    return 0


def cmd_show_field_series(args: argparse.Namespace) -> int:
    from grandgousier.archive.fields_series import list_field_series

    Session = make_session_factory(args.database_url)
    with Session() as session:
        rows = list_field_series(
            session,
            field_id=args.field_id,
            series_id=args.series_id,
            instrument_unit_id=args.instrument_unit_id,
            status=args.status,
            limit=args.limit,
        )
    _print_rows(rows, FIELD_SERIES_COLUMNS, args.format)
    return 0


def cmd_show_series_frames(args: argparse.Namespace) -> int:
    from grandgousier.archive.fields_series import list_series_frames

    Session = make_session_factory(args.database_url)
    with Session() as session:
        rows = list_series_frames(session, series_id=args.series_id, limit=args.limit)
    _print_rows(rows, SERIES_FRAME_COLUMNS, args.format)
    return 0


def cmd_run_field_pipeline(args: argparse.Namespace) -> int:
    from dataclasses import asdict

    from grandgousier.pipeline.runner import FieldPipelineConfig, run_field_pipeline

    def _none_if_negative(value):
        if value is None:
            return None
        return None if float(value) < 0 else value

    config = FieldPipelineConfig(
        catalog_name=args.catalog_name,
        calibration_prefix=args.calibration_prefix,
        instrument_unit_id=args.instrument_unit_id,
        series_id=args.series_id,
        run_uid=args.run_uid,
        out_dir=args.out_dir,
        margin_pix=args.margin_pix,
        aperture_radius_pix=args.aperture_radius_pix,
        annulus_inner_pix=args.annulus_inner_pix,
        annulus_outer_pix=args.annulus_outer_pix,
        detection_threshold_sigma=args.detection_threshold_sigma,
        detection_max_sources_per_frame=args.detection_max_sources_per_frame,
        detection_min_separation_pix=args.detection_min_separation_pix,
        detection_match_radius_arcsec=args.detection_match_radius_arcsec,
        bulk_insert_batch_size=args.bulk_insert_batch_size,
        mag_key=args.mag_key,
        aperture_zp_min_ref_stars_per_frame=args.aperture_zp_min_ref_stars_per_frame,
        aperture_zp_max_mag_err=_none_if_negative(args.aperture_zp_max_mag_err),
        aperture_zp_clip_sigma=args.aperture_zp_clip_sigma,
        aperture_zp_n_iterations=args.aperture_zp_n_iterations,
        ensemble_min_measurements=args.ensemble_min_measurements,
        ensemble_min_ref_measurements=args.ensemble_min_ref_measurements,
        ensemble_min_ref_stars_per_frame=args.ensemble_min_ref_stars_per_frame,
        ensemble_max_ref_rms=_none_if_negative(args.ensemble_max_ref_rms),
        ensemble_max_ref_mad=_none_if_negative(args.ensemble_max_ref_mad),
        ensemble_n_iterations=args.ensemble_n_iterations,
        object_qc_min_measurements=args.object_qc_min_measurements,
        crowded_arcsec=args.crowded_arcsec,
        blended_arcsec=args.blended_arcsec,
        edge_distance_pix=args.edge_distance_pix,
        high_rms=args.high_rms,
        high_mag_err=args.high_mag_err,
        min_detection_fraction=args.min_detection_fraction,
        max_ref_mag_delta=_none_if_negative(args.max_ref_mag_delta),
        dynamic_separation=args.dynamic_separation,
        dynamic_aperture_factor=args.dynamic_aperture_factor,
        dynamic_crowding_factor=args.dynamic_crowding_factor,
        fwhm_header_unit=args.fwhm_header_unit,
        field_report_min_excess=args.field_report_min_excess,
        field_report_max_mag_median=_none_if_negative(args.field_report_max_mag_median),
        field_report_top_limit=args.field_report_top_limit,
        enable_intrinsic_variability_candidates=args.enable_intrinsic_variability_candidates,
        field_report_min_intrinsic_rms=_none_if_negative(args.field_report_min_intrinsic_rms),
        field_report_min_rms_over_mag_err=_none_if_negative(args.field_report_min_rms_over_mag_err),
        field_report_min_mad_over_mag_err=_none_if_negative(args.field_report_min_mad_over_mag_err),
        field_report_intrinsic_mag_err_floor=_none_if_negative(args.field_report_intrinsic_mag_err_floor),
        field_report_min_robust_peak_to_peak=_none_if_negative(args.field_report_min_robust_peak_to_peak),
        enable_low_amplitude_intrinsic_candidates=args.enable_low_amplitude_intrinsic_candidates,
        field_report_low_amplitude_min_measurements=args.field_report_low_amplitude_min_measurements,
        field_report_low_amplitude_min_intrinsic_rms=_none_if_negative(args.field_report_low_amplitude_min_intrinsic_rms),
        field_report_low_amplitude_min_rms_over_mag_err=_none_if_negative(args.field_report_low_amplitude_min_rms_over_mag_err),
        field_report_low_amplitude_min_mad_over_mag_err=_none_if_negative(args.field_report_low_amplitude_min_mad_over_mag_err),
        field_report_low_amplitude_min_robust_peak_to_peak=_none_if_negative(args.field_report_low_amplitude_min_robust_peak_to_peak),
        aperture_stability_radii=_parse_radii_for_cli(args.aperture_stability_radii),
        aperture_stability_max_candidates=args.aperture_stability_max_candidates,
        aperture_stability_min_corr=args.aperture_stability_min_corr,
        aperture_stability_max_rms_delta=args.aperture_stability_max_rms_delta,
        aperture_stability_max_median_shift=args.aperture_stability_max_median_shift,
        aperture_stability_sweep_prefix=args.aperture_stability_sweep_prefix,
        raw_calibration_name=args.raw_calibration_name,
        aperture_zp_calibration_name=args.aperture_zp_calibration_name,
        ensemble_calibration_name=args.ensemble_calibration_name,
        skip_wcs=args.skip_wcs,
        skip_catalog_frame_match=args.skip_catalog_frame_match,
        skip_detection=args.skip_detection,
        skip_detection_catalog_match=args.skip_detection_catalog_match,
        skip_forced_photometry=args.skip_forced_photometry,
        skip_aperture_zp=args.skip_aperture_zp,
        skip_ensemble=args.skip_ensemble,
        skip_object_qc=args.skip_object_qc,
        skip_aperture_stability_qc=args.skip_aperture_stability_qc,
        skip_field_report=args.skip_field_report,
        export_candidate_light_curves=args.export_candidate_light_curves,
        export_candidate_plots=args.export_candidate_plots,
        candidate_light_curve_limit=args.candidate_light_curve_limit,
        exclude_candidates_from_ensemble=args.exclude_candidates_from_ensemble,
        candidate_ensemble_max_iterations=args.candidate_ensemble_max_iterations,
        candidate_ensemble_until_converged=args.candidate_ensemble_until_converged,
        candidate_ensemble_convergence_max_iterations=args.candidate_ensemble_convergence_max_iterations,
        replace_detections=args.replace_detections,
        replace_detection_matches=args.replace_detection_matches,
        dry_run=args.dry_run,
        record_run=args.record_run,
        continue_on_error=args.continue_on_error,
        log_dir=args.log_dir,
        file_log_level=args.file_log_level or args.log_level or "INFO",
        no_file_log=args.no_file_log,
        command_json=_build_pipeline_command_snapshot(),
        environment_json=_build_pipeline_environment_snapshot(args.database_url),
    )

    Session = make_session_factory(args.database_url)
    try:
        with Session() as session:
            result = run_field_pipeline(session, config)
    except Exception as exc:  # noqa: BLE001 - CLI should print a concise failure after runner records it.
        print(f"Field pipeline failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    rows = []
    for step in result.steps:
        row = {
            "step_name": step.step_name,
            "status": step.status,
            "error_message": step.error_message,
        }
        row.update(_compact_pipeline_step_result(step.result))
        rows.append(row)

    summary = {
        "run_uid": result.run_uid,
        "pipeline_run_id": result.pipeline_run_id,
        "status": result.status,
        **{f"calibration_{k}": v for k, v in result.calibration_names.items()},
        **{f"output_{k}": v for k, v in result.outputs.items()},
        **{f"log_{k}": v for k, v in result.log_files.items()},
    }

    if args.format == "json":
        print(json.dumps({"summary": summary, "steps": [asdict(s) for s in result.steps]}, ensure_ascii=False, indent=2))
    else:
        print(
            "Field pipeline result: "
            f"run_uid={result.run_uid}, pipeline_run_id={result.pipeline_run_id}, status={result.status}, "
            f"raw={result.calibration_names.get('raw')}, "
            f"aperture_zp={result.calibration_names.get('aperture_zp')}, "
            f"ensemble={result.calibration_names.get('ensemble')}"
        )
        if result.outputs:
            print("Outputs: " + ", ".join(f"{k}={v}" for k, v in sorted(result.outputs.items())))
        if result.log_files:
            print("Logs: " + ", ".join(f"{k}={v}" for k, v in sorted(result.log_files.items())))
        _print_rows(rows, PIPELINE_STEP_COLUMNS, args.format)
    return 0 if result.status == "DONE" else 1



def cmd_pipeline_idempotency_report(args: argparse.Namespace) -> int:
    from grandgousier.pipeline.idempotency import (
        build_pipeline_idempotency_report,
        export_pipeline_idempotency_json,
        idempotency_summary_columns,
        idempotency_summary_rows,
    )

    frame_ids = [int(v) for v in (args.frame_id or [])]
    Session = make_session_factory(args.database_url)
    with Session() as session:
        report = build_pipeline_idempotency_report(
            session,
            calibration_name=args.calibration_name,
            catalog_name=args.catalog_name,
            instrument_unit_id=args.instrument_unit_id,
            series_id=args.series_id,
            field_series_name=args.field_series_name,
            frame_ids=frame_ids or None,
            object_qc_algorithm=args.object_qc_algorithm,
            detection_algorithm=args.detection_algorithm,
            detection_match_algorithm=args.detection_match_algorithm,
            frame_qc_algorithm=args.frame_qc_algorithm,
            example_limit=args.example_limit,
        )

    if args.out:
        export_pipeline_idempotency_json(report, args.out)
        print(f"Exported pipeline idempotency JSON: path={args.out}")

    if args.format == "json":
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(
            "Pipeline idempotency result: "
            f"status={report.status}, violations={report.violations}, "
            f"missing_checks={report.missing_checks}, frame_selection_mode={report.frame_selection_mode}, "
            f"selected_frame_ids_count={report.selected_frame_ids_count}"
        )
        _print_rows(idempotency_summary_rows(report), idempotency_summary_columns(), "table")
    return 0 if report.status != "VIOLATION" else 2



def cmd_run_archive_pipeline(args: argparse.Namespace) -> int:
    from grandgousier.archive.fields_series import list_series_for_archive_processing
    from grandgousier.pipeline.runner import FieldPipelineConfig, run_field_pipeline

    Session = make_session_factory(args.database_url)
    rows: list[dict] = []
    with Session() as session:
        series_rows = list_series_for_archive_processing(
            session,
            status=args.status,
            field_id=args.field_id,
            instrument_unit_id=args.instrument_unit_id,
            limit=args.limit,
        )

    if not series_rows:
        print(
            "Archive pipeline result: selected_series=0, processed=0, done=0, failed=0, status="
            f"{args.status}"
        )
        _print_rows([], ARCHIVE_PIPELINE_COLUMNS, args.format)
        return 0

    for series in series_rows:
        series_id = int(series["series_id"])
        prefix = _format_archive_calibration_prefix(args.calibration_prefix_template, series)
        out_dir = str(Path(args.out_root) / prefix) if args.out_root else None
        config = FieldPipelineConfig(
            catalog_name=args.catalog_name,
            calibration_prefix=prefix,
            series_id=series_id,
            out_dir=out_dir,
            aperture_radius_pix=args.aperture_radius_pix,
            annulus_inner_pix=args.annulus_inner_pix,
            annulus_outer_pix=args.annulus_outer_pix,
            dry_run=args.dry_run,
            record_run=True,
            continue_on_error=False,
            log_dir=args.log_dir,
            file_log_level=args.file_log_level or args.log_level or "INFO",
            no_file_log=args.no_file_log,
            command_json=_build_pipeline_command_snapshot(),
            environment_json=_build_pipeline_environment_snapshot(args.database_url),
        )
        try:
            with Session() as session:
                result = run_field_pipeline(session, config)
            row = {
                "series_id": series_id,
                "field_id": series.get("field_id"),
                "field_name": series.get("field_name"),
                "calibration_prefix": prefix,
                "pipeline_run_id": result.pipeline_run_id,
                "status": result.status,
                "raw_candidates": _step_metric(result, "field_qc_report", "raw_candidates"),
                "accepted": _step_metric(result, "field_qc_report", "accepted"),
                "rejected": _step_metric(result, "field_qc_report", "rejected"),
                "log_file": result.log_files.get("log_file"),
                "field_qc_report_json": result.outputs.get("field_qc_report_json"),
                "error_message": None,
            }
        except Exception as exc:  # noqa: BLE001 - batch mode must report failed series and optionally continue.
            row = {
                "series_id": series_id,
                "field_id": series.get("field_id"),
                "field_name": series.get("field_name"),
                "calibration_prefix": prefix,
                "pipeline_run_id": None,
                "status": "FAILED",
                "raw_candidates": None,
                "accepted": None,
                "rejected": None,
                "log_file": None,
                "field_qc_report_json": None,
                "error_message": f"{type(exc).__name__}: {exc}",
            }
            rows.append(row)
            if args.stop_on_error:
                break
            continue
        rows.append(row)
        if row["status"] != "DONE" and args.stop_on_error:
            break

    done = sum(1 for row in rows if row.get("status") == "DONE")
    failed = sum(1 for row in rows if row.get("status") != "DONE")
    if args.format == "json":
        print(
            json.dumps(
                {
                    "summary": {
                        "selected_series": len(series_rows),
                        "processed": len(rows),
                        "done": done,
                        "failed": failed,
                        "status_filter": args.status,
                    },
                    "rows": rows,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(
            "Archive pipeline result: "
            f"selected_series={len(series_rows)}, processed={len(rows)}, done={done}, failed={failed}, "
            f"status_filter={args.status}"
        )
        _print_rows(rows, ARCHIVE_PIPELINE_COLUMNS, args.format)
    return 0 if failed == 0 else 1



def cmd_process_archive_path(args: argparse.Namespace) -> int:
    from grandgousier.archive.auto_process import (
        process_archive_group_columns,
        process_archive_path,
        process_archive_summary_columns,
        process_archive_summary_rows,
    )

    if args.execute and args.plan_only:
        print("--execute and --plan-only cannot be combined", file=sys.stderr)
        return 2

    Session = make_session_factory(args.database_url)
    with Session() as session:
        result = process_archive_path(
            session,
            root=args.path,
            catalog_name=args.catalog_name,
            execute=bool(args.execute),
            instrument_profile=args.instrument_profile,
            master_profile=args.master_profile,
            robophot_profile=args.robophot_profile,
            recursive=args.recursive,
            extensions=_parse_extensions_arg(args.extensions),
            limit=args.limit,
            min_frames_per_series=args.min_frames_per_series,
            max_field_separation_arcmin=args.max_field_separation_arcmin,
            max_series_gap_minutes=args.max_series_gap_minutes,
            include_assigned=args.include_assigned,
            include_non_science=args.include_non_science,
            calibration_prefix_template=args.calibration_prefix_template,
            out_root=args.out_root,
            log_dir=args.log_dir,
            file_log_level=args.file_log_level or args.log_level or "INFO",
            no_file_log=args.no_file_log,
            aperture_radius_pix=args.aperture_radius_pix,
            annulus_inner_pix=args.annulus_inner_pix,
            annulus_outer_pix=args.annulus_outer_pix,
            bulk_insert_batch_size=args.bulk_insert_batch_size,
            make_light_curve_summary=args.make_light_curve_summary,
            enable_detection_branch=args.enable_detection_branch,
            enable_aperture_stability_qc=args.enable_aperture_stability_qc,
            require_detection_support=args.require_detection_support,
            target_source_id=args.target_source_id,
            target_object_id=args.target_object_id,
            target_max_sep_arcsec=args.target_max_sep_arcsec,
            export_target_light_curve=args.export_target_light_curve,
            export_candidate_light_curves=args.export_candidate_light_curves,
            export_candidate_plots=args.export_candidate_plots,
            candidate_light_curve_limit=args.candidate_light_curve_limit,
            exclude_candidates_from_ensemble=args.exclude_candidates_from_ensemble,
            candidate_ensemble_max_iterations=args.candidate_ensemble_max_iterations,
            candidate_ensemble_until_converged=args.candidate_ensemble_until_converged,
            candidate_ensemble_convergence_max_iterations=args.candidate_ensemble_convergence_max_iterations,
            enable_intrinsic_variability_candidates=args.enable_intrinsic_variability_candidates,
            field_report_min_intrinsic_rms=None if args.field_report_min_intrinsic_rms is not None and args.field_report_min_intrinsic_rms < 0 else args.field_report_min_intrinsic_rms,
            field_report_min_rms_over_mag_err=None if args.field_report_min_rms_over_mag_err is not None and args.field_report_min_rms_over_mag_err < 0 else args.field_report_min_rms_over_mag_err,
            field_report_min_mad_over_mag_err=None if args.field_report_min_mad_over_mag_err is not None and args.field_report_min_mad_over_mag_err < 0 else args.field_report_min_mad_over_mag_err,
            field_report_intrinsic_mag_err_floor=None if args.field_report_intrinsic_mag_err_floor is not None and args.field_report_intrinsic_mag_err_floor < 0 else args.field_report_intrinsic_mag_err_floor,
            field_report_min_robust_peak_to_peak=None if args.field_report_min_robust_peak_to_peak is not None and args.field_report_min_robust_peak_to_peak < 0 else args.field_report_min_robust_peak_to_peak,
            enable_low_amplitude_intrinsic_candidates=args.enable_low_amplitude_intrinsic_candidates,
            field_report_low_amplitude_min_measurements=args.field_report_low_amplitude_min_measurements,
            field_report_low_amplitude_min_intrinsic_rms=None if args.field_report_low_amplitude_min_intrinsic_rms is not None and args.field_report_low_amplitude_min_intrinsic_rms < 0 else args.field_report_low_amplitude_min_intrinsic_rms,
            field_report_low_amplitude_min_rms_over_mag_err=None if args.field_report_low_amplitude_min_rms_over_mag_err is not None and args.field_report_low_amplitude_min_rms_over_mag_err < 0 else args.field_report_low_amplitude_min_rms_over_mag_err,
            field_report_low_amplitude_min_mad_over_mag_err=None if args.field_report_low_amplitude_min_mad_over_mag_err is not None and args.field_report_low_amplitude_min_mad_over_mag_err < 0 else args.field_report_low_amplitude_min_mad_over_mag_err,
            field_report_low_amplitude_min_robust_peak_to_peak=None if args.field_report_low_amplitude_min_robust_peak_to_peak is not None and args.field_report_low_amplitude_min_robust_peak_to_peak < 0 else args.field_report_low_amplitude_min_robust_peak_to_peak,
            exclude_bad_pre_qc_frames=args.exclude_bad_pre_qc_frames,
            working_catalog_mag_limit=None if args.working_catalog_mag_limit is not None and args.working_catalog_mag_limit < 0 else args.working_catalog_mag_limit,
            max_working_sources_per_frame=None if args.max_working_sources_per_frame is not None and args.max_working_sources_per_frame <= 0 else args.max_working_sources_per_frame,
            catalog_edge_margin_pix=args.catalog_edge_margin_pix,
            force_recompute_photometry=args.force_recompute_photometry,
            auto_catalog_query=args.auto_catalog_query,
            auto_catalog_margin_arcmin=args.auto_catalog_margin_arcmin,
            auto_catalog_mag_limit=args.auto_catalog_mag_limit,
            auto_catalog_max_rows_per_frame=args.auto_catalog_max_rows_per_frame,
            auto_catalog_query_mode=args.auto_catalog_query_mode,
            auto_catalog_max_radius_deg=args.auto_catalog_max_radius_deg,
            auto_catalog_timeout_sec=args.auto_catalog_timeout_sec,
            allow_empty_catalog=args.allow_empty_catalog,
            manifest_out=args.manifest_out,
        )

    if args.format == "json":
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str))
    else:
        mode = "execute" if args.execute else "plan-only"
        print(
            "Process archive path result: "
            f"mode={mode}, status={result.status}, discovered={result.discovered_files}, "
            f"detected={result.detected_files}, groups={len(result.groups)}, "
            f"pipelines={len(result.pipeline_results)}"
        )
        if result.groups:
            print("Detected FITS groups:")
            _print_rows([g.to_dict() for g in result.groups], process_archive_group_columns(), "table")
        summary_rows = process_archive_summary_rows(result)
        if summary_rows:
            print("Summary:")
            _print_rows(summary_rows, process_archive_summary_columns(), "table")
        if result.warnings:
            print("Warnings:")
            for warning in result.warnings[:20]:
                print(f"  {warning}")
            if len(result.warnings) > 20:
                print(f"  ... {len(result.warnings) - 20} more warnings")
    return 0 if result.status in {"PLAN", "DONE", "DONE_WITH_WARNINGS"} else 1


def cmd_show_pipeline_runs(args: argparse.Namespace) -> int:
    from sqlalchemy import text

    Session = make_session_factory(args.database_url)
    with Session() as session:
        where = []
        params = {"limit": args.limit}
        if args.status:
            where.append("status = :status")
            params["status"] = args.status
        if args.catalog_name:
            where.append("catalog_name = :catalog_name")
            params["catalog_name"] = args.catalog_name
        if args.instrument_unit_id is not None:
            where.append("instrument_unit_id = :instrument_unit_id")
            params["instrument_unit_id"] = args.instrument_unit_id
        if getattr(args, "field_id", None) is not None:
            where.append("field_id = :field_id")
            params["field_id"] = args.field_id
        if getattr(args, "series_id", None) is not None:
            where.append("series_id = :series_id")
            params["series_id"] = args.series_id
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        rows = [dict(r._mapping) for r in session.execute(text(f"""
            SELECT
                pipeline_run_id,
                run_uid,
                instrument_unit_id,
                field_id,
                series_id,
                catalog_name,
                calibration_prefix,
                status,
                started_at,
                finished_at,
                summary_json -> 'calibration_names' AS calibration_names,
                summary_json -> 'outputs' AS outputs,
                log_file,
                error_log_file
            FROM pipeline_runs
            {where_sql}
            ORDER BY pipeline_run_id DESC
            LIMIT :limit
        """), params).all()]
    _print_rows(rows, PIPELINE_RUN_COLUMNS, args.format)
    return 0


def cmd_show_pipeline_steps(args: argparse.Namespace) -> int:
    from sqlalchemy import text

    if args.pipeline_run_id is None and not args.run_uid:
        print("Specify --pipeline-run-id or --run-uid", file=sys.stderr)
        return 2

    Session = make_session_factory(args.database_url)
    with Session() as session:
        params = {"limit": args.limit}
        where = []
        if args.pipeline_run_id is not None:
            where.append("r.pipeline_run_id = :pipeline_run_id")
            params["pipeline_run_id"] = args.pipeline_run_id
        if args.run_uid:
            where.append("r.run_uid = :run_uid")
            params["run_uid"] = args.run_uid
        rows = [dict(r._mapping) for r in session.execute(text(f"""
            SELECT
                s.pipeline_run_step_id,
                s.pipeline_run_id,
                r.run_uid,
                s.order_index,
                s.step_name,
                s.status,
                s.started_at,
                s.finished_at,
                s.error_message,
                s.result_json
            FROM pipeline_run_steps s
            JOIN pipeline_runs r ON r.pipeline_run_id = s.pipeline_run_id
            WHERE {' AND '.join(where)}
            ORDER BY s.order_index, s.pipeline_run_step_id
            LIMIT :limit
        """), params).all()]
    for row in rows:
        row.update(_compact_pipeline_step_result(row.get("result_json") or {}))
        row.pop("result_json", None)
    _print_rows(rows, PIPELINE_STEP_DETAIL_COLUMNS, args.format)
    return 0


def cmd_show_pipeline_log_events(args: argparse.Namespace) -> int:
    from sqlalchemy import text

    if args.pipeline_run_id is None and not args.run_uid:
        print("Specify --pipeline-run-id or --run-uid", file=sys.stderr)
        return 2

    Session = make_session_factory(args.database_url)
    with Session() as session:
        params = {"limit": args.limit}
        where = []
        if args.pipeline_run_id is not None:
            where.append("r.pipeline_run_id = :pipeline_run_id")
            params["pipeline_run_id"] = args.pipeline_run_id
        if args.run_uid:
            where.append("r.run_uid = :run_uid")
            params["run_uid"] = args.run_uid
        if args.level:
            where.append("e.level = :level")
            params["level"] = args.level.upper()
        if args.event_type:
            where.append("e.event_type = :event_type")
            params["event_type"] = args.event_type
        rows = [dict(r._mapping) for r in session.execute(text(f"""
            SELECT
                e.pipeline_log_event_id,
                e.pipeline_run_id,
                r.run_uid,
                e.pipeline_run_step_id,
                s.step_name,
                e.created_at,
                e.level,
                e.event_type,
                e.message,
                e.context_json
            FROM pipeline_log_events e
            JOIN pipeline_runs r ON r.pipeline_run_id = e.pipeline_run_id
            LEFT JOIN pipeline_run_steps s ON s.pipeline_run_step_id = e.pipeline_run_step_id
            WHERE {' AND '.join(where)}
            ORDER BY e.pipeline_log_event_id DESC
            LIMIT :limit
        """), params).all()]
    _print_rows(rows, PIPELINE_LOG_EVENT_COLUMNS, args.format)
    return 0



def _format_archive_calibration_prefix(template: str, series: dict) -> str:
    values = {
        "field_name": series.get("field_name") or f"field_{series.get('field_id')}",
        "field_id": series.get("field_id"),
        "series_id": series.get("series_id"),
        "filter_name": series.get("filter_name") or "filter",
        "instrument_unit_id": series.get("instrument_unit_id"),
    }
    try:
        text_value = template.format(**values)
    except Exception:
        text_value = f"field_{series.get('field_id')}_s{series.get('series_id')}"
    text_value = re.sub(r"[^0-9A-Za-zА-Яа-я_.-]+", "_", str(text_value)).strip("_.-")
    return text_value or f"series_{series.get('series_id')}"


def _step_metric(result, step_name: str, metric: str):
    for step in result.steps:
        if step.step_name != step_name:
            continue
        data = step.result or {}
        triage = data.get("candidate_triage") or {}
        if metric in triage:
            return triage.get(metric)
        if metric in data:
            return data.get(metric)
    return None



def _build_pipeline_command_snapshot() -> dict:
    from grandgousier.logging_utils import build_command_snapshot

    return build_command_snapshot(sys.argv)


def _build_pipeline_environment_snapshot(database_url: str | None) -> dict:
    from grandgousier.logging_utils import build_environment_snapshot

    return build_environment_snapshot(database_url)


FIELD_SERIES_BUILD_COLUMNS = [
    "field_id",
    "series_id",
    "series_uid",
    "instrument_unit_id",
    "filter_name",
    "n_frames",
    "mjd_start",
    "mjd_end",
    "time_start",
    "time_end",
    "frame_id_first",
    "frame_id_last",
    "dry_run",
]

FIELD_COLUMNS = [
    "field_id",
    "field_uid",
    "field_name",
    "primary_object_name",
    "ra_center_deg",
    "dec_center_deg",
    "radius_arcmin",
    "status",
    "n_series",
    "n_frames",
    "mjd_start",
    "mjd_end",
    "time_start",
    "time_end",
]

FIELD_SERIES_COLUMNS = [
    "series_id",
    "series_uid",
    "field_id",
    "field_name",
    "instrument_unit_id",
    "filter_name",
    "time_start",
    "time_end",
    "mjd_start",
    "mjd_end",
    "ra_center_deg",
    "dec_center_deg",
    "n_frames",
    "grouping_method",
    "status",
    "last_pipeline_run_id",
    "last_pipeline_status",
    "last_calibration_prefix",
    "raw_candidates",
    "accepted",
    "rejected",
    "field_qc_report_json",
    "log_file",
    "error_log_file",
    "updated_at",
]

ARCHIVE_PIPELINE_COLUMNS = [
    "series_id",
    "field_id",
    "field_name",
    "calibration_prefix",
    "pipeline_run_id",
    "status",
    "raw_candidates",
    "accepted",
    "rejected",
    "log_file",
    "field_qc_report_json",
    "error_message",
]

SERIES_FRAME_COLUMNS = [
    "series_id",
    "order_index",
    "frame_id",
    "object_name",
    "date_obs",
    "mjd",
    "instrument_unit_id",
    "instrument",
    "site",
    "tube",
    "camera",
    "filter_name",
    "ra_center_deg",
    "dec_center_deg",
    "wcs_status",
    "processing_status",
    "raw_path",
]

PIPELINE_STEP_COLUMNS = [
    "step_name",
    "status",
    "selected_frames",
    "checked_frames",
    "selected_links",
    "measured",
    "inserted_or_updated",
    "matched",
    "written",
    "calibrated",
    "frames_with_zp",
    "frames_with_offsets",
    "raw_candidates",
    "selected_candidates",
    "checked_candidates",
    "updated",
    "aperture_unstable",
    "aperture_median_shift",
    "possible_blend",
    "failed",
    "prepared_sweep_calibrations",
    "accepted",
    "rejected",
    "error_message",
]

PIPELINE_RUN_COLUMNS = [
    "pipeline_run_id",
    "run_uid",
    "instrument_unit_id",
    "field_id",
    "series_id",
    "catalog_name",
    "calibration_prefix",
    "status",
    "started_at",
    "finished_at",
    "calibration_names",
    "outputs",
    "log_file",
    "error_log_file",
]

PIPELINE_LOG_EVENT_COLUMNS = [
    "pipeline_log_event_id",
    "pipeline_run_id",
    "run_uid",
    "pipeline_run_step_id",
    "step_name",
    "created_at",
    "level",
    "event_type",
    "message",
    "context_json",
]

PIPELINE_STEP_DETAIL_COLUMNS = [
    "pipeline_run_step_id",
    "pipeline_run_id",
    "run_uid",
    "order_index",
    "step_name",
    "status",
    "selected_frames",
    "checked_frames",
    "selected_links",
    "measured",
    "inserted_or_updated",
    "matched",
    "written",
    "calibrated",
    "frames_with_zp",
    "frames_with_offsets",
    "raw_candidates",
    "selected_candidates",
    "checked_candidates",
    "updated",
    "aperture_unstable",
    "aperture_median_shift",
    "possible_blend",
    "failed",
    "prepared_sweep_calibrations",
    "accepted",
    "rejected",
    "started_at",
    "finished_at",
    "error_message",
]


def _compact_pipeline_step_result(result: dict | None) -> dict:
    result = result or {}
    compact = {}
    summary = result.get("summary") if isinstance(result, dict) else None
    if isinstance(summary, dict):
        for key, value in summary.items():
            result.setdefault(key, value)

    for key in (
        "selected_frames",
        "checked_frames",
        "selected_links",
        "measured",
        "inserted_or_updated",
        "matched",
        "written",
        "calibrated",
        "frames_with_zp",
        "frames_with_offsets",
        "raw_candidates",
        "selected_candidates",
        "checked_candidates",
        "updated",
        "aperture_unstable",
        "aperture_median_shift",
        "possible_blend",
        "failed",
        "prepared_sweep_calibrations",
    ):
        if key in result:
            compact[key] = result.get(key)
    candidate_summary = result.get("candidate_triage") if isinstance(result, dict) else None
    if isinstance(candidate_summary, dict):
        compact["raw_candidates"] = candidate_summary.get("raw_candidates")
        compact["accepted"] = candidate_summary.get("accepted")
        compact["rejected"] = candidate_summary.get("rejected")
    return compact


def _round_event_row(row: dict) -> dict:
    row = dict(row)
    for key, nd in (
        ("ra_deg", 8),
        ("dec_deg", 8),
        ("position_error_arcsec", 3),
        ("separation_arcsec", 4),
        ("confidence", 3),
        ("t_start_bjd_tdb", 6),
        ("t_peak_bjd_tdb", 6),
        ("t_end_bjd_tdb", 6),
        ("t0_bjd_tdb", 6),
        ("period_days", 8),
        ("duration_hours", 4),
        ("expected_depth_ppt", 4),
        ("expected_amplitude_mag", 4),
        ("priority", 3),
        ("window_start_bjd_tdb", 6),
        ("window_end_bjd_tdb", 6),
        ("window_duration_hours", 4),
        ("pre_baseline_start_bjd_tdb", 6),
        ("post_baseline_end_bjd_tdb", 6),
        ("pre_margin_hours", 4),
        ("post_margin_hours", 4),
        ("mjd_min", 6),
        ("mjd_max", 6),
        ("mag_median", 5),
        ("mag_rms", 5),
        ("mag_mad", 5),
        ("median_mag_err", 5),
        ("depth_per_point_snr_est", 3),
        ("depth_total_snr_est", 3),
        ("frame_time_jd_approx", 6),
        ("mjd", 6),
        ("mag_cal", 5),
        ("mag_err", 5),
        ("separation_arcsec", 4),
        ("detection_snr", 3),
        ("detection_flux", 3),
        ("peak_adu", 3),
        ("nearest_catalog_sep_arcsec", 4),
    ):
        if key in row:
            row[key] = _round_float(row.get(key), nd)
    return row


def cmd_field_qc_report(args: argparse.Namespace) -> int:
    def _none_if_negative(value):
        if value is None:
            return None
        return None if float(value) < 0 else value

    from grandgousier.quality.field_report import (
        build_field_qc_report,
        export_field_qc_json,
        export_field_qc_tables,
        field_qc_summary_columns,
    )

    Session = make_session_factory(args.database_url)
    with Session() as session:
        result = build_field_qc_report(
            session,
            calibration_name=args.calibration_name,
            catalog_name=args.catalog_name,
            instrument_unit_id=args.instrument_unit_id,
            series_id=args.series_id,
            field_series_name=args.field_series_name,
            frame_qc_algorithm=args.frame_qc_algorithm,
            object_qc_algorithm=args.object_qc_algorithm,
            min_measurements=args.min_measurements,
            bin_width=args.bin_width,
            min_objects_per_bin=args.min_objects_per_bin,
            min_rms=args.min_rms,
            min_excess=args.min_excess,
            exclude_flagged=args.exclude_flagged,
            exclude_crowded=args.exclude_crowded,
            exclude_edge=args.exclude_edge,
            exclude_saturated=args.exclude_saturated,
            exclude_catalog_outliers=args.exclude_catalog_outliers,
            reject_aperture_unstable=args.reject_aperture_unstable,
            require_detection_support=args.require_detection_support,
            max_mag_median=args.max_mag_median,
            max_ref_mag_delta=args.max_ref_mag_delta,
            top_limit=args.top_limit,
            enable_intrinsic_variability_candidates=args.enable_intrinsic_variability_candidates,
            min_intrinsic_rms=_none_if_negative(args.min_intrinsic_rms),
            min_rms_over_mag_err=_none_if_negative(args.min_rms_over_mag_err),
            min_mad_over_mag_err=_none_if_negative(args.min_mad_over_mag_err),
            intrinsic_mag_err_floor=_none_if_negative(args.intrinsic_mag_err_floor),
            min_robust_peak_to_peak=_none_if_negative(args.min_robust_peak_to_peak),
            enable_low_amplitude_intrinsic_candidates=args.enable_low_amplitude_intrinsic_candidates,
            low_amplitude_min_measurements=args.low_amplitude_min_measurements,
            low_amplitude_min_intrinsic_rms=_none_if_negative(args.low_amplitude_min_intrinsic_rms),
            low_amplitude_min_rms_over_mag_err=_none_if_negative(args.low_amplitude_min_rms_over_mag_err),
            low_amplitude_min_mad_over_mag_err=_none_if_negative(args.low_amplitude_min_mad_over_mag_err),
            low_amplitude_min_robust_peak_to_peak=_none_if_negative(args.low_amplitude_min_robust_peak_to_peak),
        )

    if args.out:
        export_field_qc_json(result.report, args.out)
        print(f"Exported field QC JSON: path={args.out}")
    if args.tables_prefix:
        outputs = export_field_qc_tables(
            prefix=args.tables_prefix,
            rms_bins=result.rms_bins,
            candidate_rows=result.candidate_rows,
            blend_rows=result.blend_rows,
        )
        print(
            "Exported field QC CSV tables: "
            f"rms_bins={outputs['rms_bins']}, "
            f"candidate_triage={outputs['candidate_triage']}, "
            f"blend_diagnostics={outputs['blend_diagnostics']}"
        )

    if args.format == "json":
        print(json.dumps(result.report, ensure_ascii=False, indent=2))
    else:
        _print_rows(result.summary_rows, field_qc_summary_columns(), "table")
    return 0


def cmd_blend_diagnostic_summary(args: argparse.Namespace) -> int:
    from grandgousier.quality.blend_diagnostics import (
        blend_diagnostic_columns,
        build_blend_diagnostic_rows,
        export_blend_diagnostic_csv,
        summarize_blend_diagnostics,
    )

    Session = make_session_factory(args.database_url)
    with Session() as session:
        full_rows = build_blend_diagnostic_rows(
            session,
            calibration_name=args.calibration_name,
            catalog_name=args.catalog_name,
            instrument_unit_id=args.instrument_unit_id,
            algorithm=args.qc_algorithm,
            only_flagged=args.only_flagged,
            only_possible_blends=args.only_possible_blends,
            max_separation_index=args.max_separation_index,
            min_mag_rms=args.min_mag_rms,
            sort=args.sort,
            limit=None,
        )
    if args.out:
        count = export_blend_diagnostic_csv(full_rows, args.out)
        print(f"Exported blend diagnostics CSV: path={args.out}, rows={count}")

    summary = summarize_blend_diagnostics(full_rows)
    rows = full_rows[: int(args.limit)] if args.limit is not None else full_rows
    for row in rows:
        for key, nd in (
            ("ra_deg", 8),
            ("dec_deg", 8),
            ("mag_median", 5),
            ("mag_rms", 5),
            ("mag_mad", 5),
            ("nearest_neighbor_arcsec", 4),
            ("dynamic_blend_arcsec", 4),
            ("dynamic_crowded_arcsec", 4),
            ("separation_index", 4),
            ("crowding_index", 4),
            ("fwhm_arcsec_used", 4),
            ("aperture_radius_pix_used", 3),
            ("aperture_radius_arcsec_used", 4),
            ("pixel_scale_arcsec_used", 4),
            ("detection_fraction", 3),
            ("detection_frame_fraction", 3),
            ("multiple_detection_frame_ratio", 3),
            ("det_sep_median_arcsec", 4),
            ("det_snr_median", 3),
            ("aperture_min_corr_with_ref", 4),
            ("aperture_max_rms_delta_vs_ref", 5),
            ("aperture_max_abs_median_delta_vs_ref", 5),
        ):
            row[key] = _round_float(row.get(key), nd)

    if args.format == "table":
        print(
            "Summary: "
            f"total={summary.get('total')}, possible_blend={summary.get('possible_blend')}, "
            f"dynamic_crowded={summary.get('dynamic_crowded')}, dynamic_blend_risk={summary.get('dynamic_blend_risk')}, "
            f"aperture_unstable={summary.get('aperture_unstable')}, aperture_median_shift={summary.get('aperture_median_shift')}, "
            f"multiple_detections_per_frame={summary.get('multiple_detections_per_frame')}, "
            f"separation_index_lt_1={summary.get('separation_index_lt_1')}, "
            f"separation_index_lt_1p5={summary.get('separation_index_lt_1p5')}"
        )
    _print_rows(rows, blend_diagnostic_columns(), args.format)
    return 0


def cmd_aperture_radius_sweep(args: argparse.Namespace) -> int:
    from grandgousier.quality.aperture_sweep import (
        aperture_sweep_columns,
        export_aperture_sweep_csv,
        parse_radii,
        run_aperture_radius_sweep,
    )

    if args.source_id is None and args.object_id is None:
        raise SystemExit("Provide --source-id or --object-id")
    max_mag_err = None if args.max_mag_err is not None and args.max_mag_err < 0 else args.max_mag_err
    radii = parse_radii(args.radii)

    Session = make_session_factory(args.database_url)
    with Session() as session:
        result = run_aperture_radius_sweep(
            session,
            catalog_name=args.catalog_name,
            source_id=args.source_id,
            object_id=args.object_id,
            instrument_unit_id=args.instrument_unit_id,
            radii=radii,
            calibration_prefix=args.calibration_prefix,
            annulus_inner_pix=args.annulus_inner_pix,
            annulus_outer_pix=args.annulus_outer_pix,
            mag_key=args.mag_key,
            min_ref_stars_per_frame=args.min_ref_stars_per_frame,
            max_mag_err=max_mag_err,
            max_zp_mad=args.max_zp_mad,
            clip_sigma=args.clip_sigma,
            n_iterations=args.n_iterations,
            min_ref_measurements=args.min_ref_measurements,
            ensemble_min_ref_stars_per_frame=args.ensemble_min_ref_stars_per_frame,
            max_ref_rms=args.max_ref_rms,
            max_ref_mad=args.max_ref_mad,
            skip_photometry=args.skip_photometry,
            skip_calibration=args.skip_calibration,
            dry_run=args.dry_run,
        )

    if args.out:
        count = export_aperture_sweep_csv(result.rows, args.out)
        print(f"Exported aperture-radius sweep CSV: path={args.out}, rows={count}")

    rows = result.rows
    for row in rows:
        for key, nd in (
            ("radius_pix", 3),
            ("mag_median", 5),
            ("mag_rms", 5),
            ("mag_mad", 5),
            ("rms_over_mag_err", 3),
            ("mad_over_mag_err", 3),
            ("effective_mag_err_median", 5),
            ("intrinsic_variability_score", 3),
            ("mag_err_median", 5),
            ("corr_with_ref", 4),
            ("median_delta_vs_ref", 5),
            ("rms_delta_vs_ref", 5),
            ("aperture_zp_frame_zp_median", 5),
            ("aperture_zp_frame_zp_mad", 5),
        ):
            row[key] = _round_float(row.get(key), nd)

    if args.format == "table":
        print(
            "Summary: "
            f"catalog_name={result.summary.get('catalog_name')}, source_id={result.summary.get('source_id')}, "
            f"object_id={result.summary.get('object_id')}, radii={result.summary.get('radii')}, "
            f"reference_radius_pix={result.summary.get('reference_radius_pix')}, "
            f"dry_run={result.summary.get('dry_run')}"
        )
    _print_rows(rows, aperture_sweep_columns(), args.format)
    return 0



def cmd_compute_aperture_stability_qc(args: argparse.Namespace) -> int:
    from grandgousier.quality.aperture_stability import compute_aperture_stability_qc
    from grandgousier.quality.aperture_sweep import parse_radii

    if args.source_id is None and args.object_id is None:
        raise SystemExit("Provide --source-id or --object-id")

    Session = make_session_factory(args.database_url)
    with Session() as session:
        result = compute_aperture_stability_qc(
            session,
            calibration_name=args.calibration_name,
            catalog_name=args.catalog_name,
            source_id=args.source_id,
            object_id=args.object_id,
            instrument_unit_id=args.instrument_unit_id,
            radii=parse_radii(args.radii),
            sweep_calibration_prefix=args.sweep_calibration_prefix,
            annulus_inner_pix=args.annulus_inner_pix,
            annulus_outer_pix=args.annulus_outer_pix,
            mag_key=args.mag_key,
            algorithm=args.algorithm,
            min_stable_corr=args.min_stable_corr,
            max_stable_rms_delta=args.max_stable_rms_delta,
            max_median_shift=args.max_median_shift,
            possible_blend_neighbor_arcsec=args.possible_blend_neighbor_arcsec,
            dry_run=args.dry_run,
        )
    m = result.metrics or {}
    print(
        "Aperture stability QC result: "
        f"calibration_name={result.calibration_name}, catalog_name={result.catalog_name}, "
        f"instrument_unit_id={result.instrument_unit_id}, object_id={result.object_id}, "
        f"source_id={result.source_id}, updated={result.updated}, "
        f"aperture_unstable={result.aperture_unstable}, aperture_median_shift={result.aperture_median_shift}, "
        f"possible_blend={result.possible_blend}, "
        f"min_corr_with_ref={_round_float(m.get('min_corr_with_ref'), 4)}, "
        f"max_rms_delta_vs_ref={_round_float(m.get('max_rms_delta_vs_ref'), 5)}, "
        f"max_abs_median_delta_vs_ref={_round_float(m.get('max_abs_median_delta_vs_ref'), 5)}, "
        f"dry_run={result.dry_run}"
    )
    return 0



def cmd_transfer_aperture_stability_qc(args: argparse.Namespace) -> int:
    from grandgousier.quality.object_quality import transfer_aperture_stability_qc

    Session = make_session_factory(args.database_url)
    with Session() as session:
        result = transfer_aperture_stability_qc(
            session,
            source_calibration_name=args.source_calibration_name,
            target_calibration_name=args.target_calibration_name,
            catalog_name=args.catalog_name,
            instrument_unit_id=args.instrument_unit_id,
            algorithm=args.algorithm,
            create_missing=args.create_missing,
            dry_run=args.dry_run,
        )

    payload = result.__dict__
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(
            "Aperture-stability transfer result: "
            f"source_calibration={result.source_calibration_name}, "
            f"target_calibration={result.target_calibration_name}, "
            f"source_rows={result.source_rows}, target_rows={result.target_rows}, "
            f"updated={result.updated}, created={result.created}, "
            f"skipped_no_aperture_stability={result.skipped_no_aperture_stability}, "
            f"skipped_missing_target_qc={result.skipped_missing_target_qc}, "
            f"dry_run={result.dry_run}"
        )
    return 0


def cmd_compute_candidate_aperture_stability_qc(args: argparse.Namespace) -> int:
    from grandgousier.quality.candidate_aperture_stability import (
        candidate_aperture_stability_columns,
        compute_candidate_aperture_stability_qc,
        export_candidate_aperture_stability_csv,
    )

    def _none_if_negative(value):
        if value is None:
            return None
        return None if float(value) < 0 else value

    Session = make_session_factory(args.database_url)
    with Session() as session:
        result = compute_candidate_aperture_stability_qc(
            session,
            calibration_name=args.calibration_name,
            catalog_name=args.catalog_name,
            instrument_unit_id=args.instrument_unit_id,
            radii=_parse_radii_for_cli(args.radii),
            sweep_calibration_prefix=args.sweep_calibration_prefix,
            annulus_inner_pix=args.annulus_inner_pix,
            annulus_outer_pix=args.annulus_outer_pix,
            mag_key=args.mag_key,
            min_measurements=args.min_measurements,
            min_excess=args.min_excess,
            max_mag_median=_none_if_negative(args.max_mag_median),
            max_ref_mag_delta=_none_if_negative(args.max_ref_mag_delta),
            max_candidates=args.max_candidates,
            min_stable_corr=args.min_stable_corr,
            max_stable_rms_delta=args.max_stable_rms_delta,
            max_median_shift=args.max_median_shift,
            possible_blend_neighbor_arcsec=args.possible_blend_neighbor_arcsec,
            min_ref_stars_per_frame=args.min_ref_stars_per_frame,
            max_mag_err=_none_if_negative(args.max_mag_err),
            clip_sigma=args.clip_sigma,
            n_iterations=args.n_iterations,
            min_ref_measurements=args.min_ref_measurements,
            ensemble_min_ref_stars_per_frame=args.ensemble_min_ref_stars_per_frame,
            max_ref_rms=_none_if_negative(args.max_ref_rms),
            max_ref_mad=_none_if_negative(args.max_ref_mad),
            skip_sweep_preparation=args.skip_sweep_preparation,
            continue_on_error=args.continue_on_error,
            dry_run=args.dry_run,
        )

    if args.out:
        count = export_candidate_aperture_stability_csv(result.rows, args.out)
        print(f"Exported candidate aperture-stability CSV: path={args.out}, rows={count}")

    rows = [dict(row) for row in result.rows]
    for row in rows:
        for key, nd in (
            ("mag_median", 5),
            ("mag_rms", 5),
            ("rms_excess", 3),
            ("min_corr_with_ref", 4),
            ("max_rms_delta_vs_ref", 5),
            ("max_abs_median_delta_vs_ref", 5),
        ):
            row[key] = _round_float(row.get(key), nd)

    if args.format == "json":
        print(json.dumps({"summary": result.summary, "rows": rows}, ensure_ascii=False, indent=2))
    else:
        if args.format == "table":
            print(
                "Candidate aperture-stability QC result: "
                f"calibration_name={result.summary.get('calibration_name')}, "
                f"raw_candidates={result.summary.get('raw_candidates')}, "
                f"selected_candidates={result.summary.get('selected_candidates')}, "
                f"updated={result.summary.get('updated')}, "
                f"aperture_unstable={result.summary.get('aperture_unstable')}, "
                f"aperture_median_shift={result.summary.get('aperture_median_shift')}, "
                f"possible_blend={result.summary.get('possible_blend')}, "
                f"failed={result.summary.get('failed')}, "
                f"prepared_sweep_calibrations={result.summary.get('prepared_sweep_calibrations')}, "
                f"sweep_calibration_prefix={result.summary.get('sweep_calibration_prefix')}, "
                f"dry_run={result.summary.get('dry_run')}"
            )
        _print_rows(rows, candidate_aperture_stability_columns(), args.format)
    return 0

def cmd_compare_calibrations(args: argparse.Namespace) -> int:
    from grandgousier.quality.lightcurve_quality import compare_calibration_scatter

    Session = make_session_factory(args.database_url)
    with Session() as session:
        rows, summary = compare_calibration_scatter(
            session,
            left_calibration_name=args.left,
            right_calibration_name=args.right,
            catalog_name=args.catalog_name,
            instrument_unit_id=args.instrument_unit_id,
            min_measurements=args.min_measurements,
            limit=args.limit,
        )
    for row in rows:
        for key, nd in (
            ("mag_median", 5),
            ("left_rms", 5),
            ("right_rms", 5),
            ("rms_ratio", 4),
            ("rms_delta", 5),
            ("left_mad", 5),
            ("right_mad", 5),
        ):
            row[key] = _round_float(row.get(key), nd)
    columns = [
        "object_id",
        "catalog_name",
        "source_id",
        "mag_median",
        "n_left",
        "n_right",
        "left_rms",
        "right_rms",
        "rms_ratio",
        "rms_delta",
        "left_mad",
        "right_mad",
    ]
    if args.format == "json":
        print(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2))
        return 0
    summary_text = (
        "Summary: "
        f"left={summary.get('left_calibration_name')}, right={summary.get('right_calibration_name')}, "
        f"common={summary.get('n_common_objects')}, "
        f"left_rms_median={_round_float(summary.get('left_rms_median'), 5)}, "
        f"right_rms_median={_round_float(summary.get('right_rms_median'), 5)}, "
        f"ratio_median={_round_float(summary.get('rms_ratio_median'), 4)}, "
        f"improved_fraction={_round_float(summary.get('improved_fraction'), 3)}"
    )
    if args.format == "csv":
        print(summary_text, file=sys.stderr)
    else:
        print(summary_text)
    _print_rows(rows, columns, args.format)
    return 0


def cmd_show_lightcurve(args: argparse.Namespace) -> int:
    from grandgousier.calibration.preliminary import lightcurve_rows_to_dicts, load_lightcurve_rows

    if args.source_id is None and args.object_id is None:
        raise SystemExit("Provide --source-id or --object-id")
    Session = make_session_factory(args.database_url)
    with Session() as session:
        rows0 = load_lightcurve_rows(
            session,
            calibration_name=args.calibration_name,
            source_id=args.source_id,
            object_id=args.object_id,
            catalog_name=args.catalog_name,
            instrument_unit_id=args.instrument_unit_id,
            limit=args.limit,
        )
    rows = lightcurve_rows_to_dicts(rows0)
    for row in rows:
        for key, nd in (
            ("mjd", 6),
            ("x", 3),
            ("y", 3),
            ("flux", 3),
            ("flux_err", 3),
            ("mag_inst", 5),
            ("mag_cal", 5),
            ("mag_err", 5),
            ("local_zp_correction", 5),
        ):
            row[key] = _round_float(row.get(key), nd)
    columns = [
        "measurement_id",
        "frame_id",
        "object_id",
        "source_id",
        "catalog_name",
        "date_obs",
        "mjd",
        "filter_name",
        "x",
        "y",
        "flux",
        "flux_err",
        "mag_inst",
        "mag_cal",
        "mag_err",
        "local_zp_correction",
    ]
    _print_rows(rows, columns, args.format)
    return 0


def cmd_export_lightcurve(args: argparse.Namespace) -> int:
    from grandgousier.calibration.preliminary import export_lightcurve_csv, load_lightcurve_rows

    if args.source_id is None and args.object_id is None:
        raise SystemExit("Provide --source-id or --object-id")
    Session = make_session_factory(args.database_url)
    with Session() as session:
        rows = load_lightcurve_rows(
            session,
            calibration_name=args.calibration_name,
            source_id=args.source_id,
            object_id=args.object_id,
            catalog_name=args.catalog_name,
            instrument_unit_id=args.instrument_unit_id,
            limit=args.limit,
        )
    n = export_lightcurve_csv(rows, args.out)
    print(f"Exported {n} light-curve rows to {args.out}")
    return 0


def cmd_list_light_curve_objects(args: argparse.Namespace) -> int:
    from grandgousier.lightcurves.product import (
        light_curve_object_columns,
        list_light_curve_objects,
        parse_reject_flags,
    )

    reject_flags = _science_reject_flags_from_args(args)
    frame_ids = [int(v) for v in (args.frame_id or [])]
    Session = make_session_factory(args.database_url)
    with Session() as session:
        _scope, rows = list_light_curve_objects(
            session,
            calibration_names=args.calibration_name or [],
            calibration_prefixes=args.calibration_prefix or [],
            model_name=args.model_name,
            catalog_name=args.catalog_name,
            instrument_unit_id=args.instrument_unit_id,
            series_id=args.series_id,
            field_series_name=args.field_series_name,
            frame_ids=frame_ids or None,
            object_qc_algorithm=args.object_qc_algorithm,
            offset_algorithm=args.offset_algorithm,
            reject_flags=reject_flags,
            min_points=args.min_points,
            exclude_rejected=args.exclude_rejected,
            limit=args.limit,
        )
    for row in rows:
        for key, nd in (
            ("mjd_min", 6),
            ("mjd_max", 6),
            ("mag_global_median", 5),
            ("mag_global_rms", 5),
            ("mag_global_mad", 5),
            ("rms_over_mag_err", 3),
            ("mad_over_mag_err", 3),
            ("effective_mag_err_median", 5),
            ("intrinsic_variability_score", 3),
            ("mag_err_median", 5),
        ):
            row[key] = _round_float(row.get(key), nd)
    _print_rows(rows, light_curve_object_columns(), args.format)
    return 0


def cmd_light_curve_summary(args: argparse.Namespace) -> int:
    from grandgousier.lightcurves.product import (
        build_light_curve_summary,
        export_light_curve_summary_json,
        light_curve_summary_columns,
        light_curve_summary_rows,
    )

    reject_flags = _science_reject_flags_from_args(args)
    frame_ids = [int(v) for v in (args.frame_id or [])]
    Session = make_session_factory(args.database_url)
    with Session() as session:
        summary = build_light_curve_summary(
            session,
            calibration_names=args.calibration_name or [],
            calibration_prefixes=args.calibration_prefix or [],
            model_name=args.model_name,
            catalog_name=args.catalog_name,
            instrument_unit_id=args.instrument_unit_id,
            series_id=args.series_id,
            field_series_name=args.field_series_name,
            frame_ids=frame_ids or None,
            object_qc_algorithm=args.object_qc_algorithm,
            offset_algorithm=args.offset_algorithm,
            reject_flags=reject_flags,
        )
    if args.out:
        export_light_curve_summary_json(summary, args.out)
        print(f"Exported light-curve summary JSON: path={args.out}")
    if args.format == "json":
        print(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(
            "Light-curve summary: "
            f"points={summary.total_points}, accepted={summary.accepted_points}, "
            f"rejected={summary.rejected_points}, objects={summary.n_objects}, "
            f"series={summary.n_series}, calibrations={summary.n_calibrations}"
        )
        _print_rows(light_curve_summary_rows(summary), light_curve_summary_columns(), "table")
    return 0


def cmd_export_science_light_curve(args: argparse.Namespace) -> int:
    from grandgousier.lightcurves.product import (
        export_science_light_curve_csv,
        load_science_light_curve_rows,
    )

    if args.source_id is None and args.object_id is None:
        raise SystemExit("Provide --source-id or --object-id")
    reject_flags = _science_reject_flags_from_args(args)
    frame_ids = [int(v) for v in (args.frame_id or [])]
    Session = make_session_factory(args.database_url)
    with Session() as session:
        scope, rows = load_science_light_curve_rows(
            session,
            calibration_names=args.calibration_name or [],
            calibration_prefixes=args.calibration_prefix or [],
            model_name=args.model_name,
            catalog_name=args.catalog_name,
            source_id=args.source_id,
            object_id=args.object_id,
            instrument_unit_id=args.instrument_unit_id,
            series_id=args.series_id,
            field_series_name=args.field_series_name,
            frame_ids=frame_ids or None,
            object_qc_algorithm=args.object_qc_algorithm,
            offset_algorithm=args.offset_algorithm,
            reject_flags=reject_flags,
            exclude_rejected=args.exclude_rejected,
            limit=args.limit,
        )
    n = export_science_light_curve_csv(rows, args.out)
    if args.format == "json":
        print(
            json.dumps(
                {
                    "out": args.out,
                    "rows": n,
                    "accepted": sum(1 for row in rows if not row.get("is_rejected")),
                    "rejected": sum(1 for row in rows if row.get("is_rejected")),
                    "scope": scope.to_dict(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(
            f"Exported {n} science light-curve rows to {args.out}; "
            f"accepted={sum(1 for row in rows if not row.get('is_rejected'))}, "
            f"rejected={sum(1 for row in rows if row.get('is_rejected'))}, "
            f"frame_selection_mode={scope.frame_selection_mode}, "
            f"selected_frame_ids_count={scope.selected_frame_ids_count}"
        )
    return 0


def cmd_export_lightcurve_plot(args: argparse.Namespace) -> int:
    from grandgousier.calibration.preliminary import load_lightcurve_rows
    from grandgousier.visualization.lightcurve_plot import export_lightcurve_plot

    if args.source_id is None and args.object_id is None:
        raise SystemExit("Provide --source-id or --object-id")
    Session = make_session_factory(args.database_url)
    with Session() as session:
        rows = load_lightcurve_rows(
            session,
            calibration_name=args.calibration_name,
            source_id=args.source_id,
            object_id=args.object_id,
            catalog_name=args.catalog_name,
            instrument_unit_id=args.instrument_unit_id,
            limit=args.limit,
        )
    result = export_lightcurve_plot(
        rows,
        args.out,
        x_key=args.x,
        y_key=args.y,
        title=args.title,
        dpi=args.dpi,
        invert_y=False if args.no_invert_y else None,
        show_errors=not args.no_errors,
        show_stats=args.show_stats,
        observatory_name=args.observatory,
        observatory_lat_deg=args.observatory_lat_deg,
        observatory_lon_deg=args.observatory_lon_deg,
        observatory_height_m=args.observatory_height_m,
    )
    offset_text = f", x_offset={result.x_offset}" if result.x_offset is not None else ""
    warning_text = f", warning={result.time_warning}" if result.time_warning else ""
    print(
        "Exported light-curve plot: "
        f"path={result.output_path}, rows={result.n_rows}, points={result.n_points}, "
        f"x={result.x_key}{offset_text}, y={result.y_key}, inverted_y={result.inverted_y}{warning_text}"
    )
    return 0


def _parse_name_column_args(items: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"Expected name=column mapping, got: {item}")
        name, col = item.split("=", 1)
        name = name.strip()
        col = col.strip()
        if not name or not col:
            raise SystemExit(f"Expected non-empty name=column mapping, got: {item}")
        out[name] = col
    return out


def _parse_name_float_args(items: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"Expected name=value mapping, got: {item}")
        name, value = item.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name or not value:
            raise SystemExit(f"Expected non-empty name=value mapping, got: {item}")
        try:
            out[name] = float(value)
        except ValueError as exc:
            raise SystemExit(f"Expected numeric value in mapping {item!r}") from exc
    return out



def _parse_extensions_arg(value: str | None) -> list[str]:
    if value is None or str(value).strip() == "":
        return [".fits", ".fit", ".fts"]
    out: list[str] = []
    for part in str(value).replace(";", ",").split(","):
        item = part.strip()
        if not item:
            continue
        if not item.startswith("."):
            item = "." + item
        out.append(item.lower())
    return out or [".fits", ".fit", ".fts"]


def _parse_int_list_arg(value: str | None) -> list[int]:
    if value is None or str(value).strip() == "":
        return []
    out: list[int] = []
    for part in str(value).replace(";", ",").split(","):
        item = part.strip()
        if not item:
            continue
        try:
            out.append(int(item))
        except ValueError as exc:
            raise SystemExit(f"Expected integer object_id in list, got: {item!r}") from exc
    return out


def _parse_optional_float_or_mapping(value: str | None, expected_name: str, label: str) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    text_value = str(value).strip()
    if "=" in text_value:
        name, raw = text_value.split("=", 1)
        norm_name = _normalize_cli_name(name)
        norm_expected = _normalize_cli_name(expected_name)
        if norm_name != norm_expected:
            raise SystemExit(f"Expected {label} for {expected_name!r}, got mapping for {name!r}")
        text_value = raw.strip()
    try:
        return float(text_value)
    except ValueError as exc:
        raise SystemExit(f"Expected numeric value for {label}, got: {value!r}") from exc


def _normalize_cli_name(value: str) -> str:
    return str(value).strip().lower().replace(" ", "").replace("-", "_")


def _print_rows(rows: list[dict], columns: list[str], fmt: str) -> None:
    if fmt == "json":
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if fmt == "csv":
        writer = csv.DictWriter(sys.stdout, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        return
    _print_table(rows, columns)


def _print_table(rows: list[dict], columns: list[str]) -> None:
    if not rows:
        print("No rows.")
        return
    max_width = 32
    widths = {}
    for col in columns:
        values = ["" if row.get(col) is None else str(row.get(col)) for row in rows]
        widths[col] = min(max([len(col), *(len(v) for v in values)]), max_width)

    def cell(value, width):
        text = "" if value is None else str(value)
        if len(text) > width:
            text = text[: max(0, width - 1)] + "…"
        return text.ljust(width)

    header = "  ".join(cell(col, widths[col]) for col in columns)
    sep = "  ".join("-" * widths[col] for col in columns)
    print(header)
    print(sep)
    for row in rows:
        print("  ".join(cell(row.get(col), widths[col]) for col in columns))


def _format_dt(value) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _enum_value(value):
    return getattr(value, "value", value)


def _round_float(value, ndigits: int):
    if value is None:
        return None
    try:
        return round(float(value), ndigits)
    except Exception:
        return value




def cmd_detect_sources(args: argparse.Namespace) -> int:
    from grandgousier.photometry.source_detection import detect_sources_in_frames

    Session = make_session_factory(args.database_url)
    with Session() as session:
        result = detect_sources_in_frames(
            session,
            frame_ids=args.frame_id,
            instrument=args.instrument,
            instrument_unit_id=args.instrument_unit_id,
            only_wcs_ok=not args.all_wcs_statuses,
            threshold_sigma=args.threshold_sigma,
            max_sources_per_frame=args.max_sources_per_frame,
            min_separation_pix=args.min_separation_pix,
            aperture_radius_pix=args.aperture_radius_pix,
            annulus_inner_pix=args.annulus_inner_pix,
            annulus_outer_pix=args.annulus_outer_pix,
            centroid_box_radius_pix=args.centroid_box_radius_pix,
            replace_existing=args.replace_existing,
            limit_frames=args.limit_frames,
            dry_run=args.dry_run,
        )
    print(
        "Source detection result: "
        f"selected_frames={result.selected_frames}, checked_frames={result.checked_frames}, "
        f"detected={result.detected}, inserted={result.inserted}, "
        f"deleted_existing={result.deleted_existing}, skipped_missing_file={result.skipped_missing_file}, "
        f"failed_frames={result.failed_frames}, dry_run={result.dry_run}"
    )
    return 0 if result.failed_frames == 0 else 2


def cmd_source_detection_summary(args: argparse.Namespace) -> int:
    from sqlalchemy import func, select
    from grandgousier.db.models import Frame, InstrumentUnit, SourceDetection

    Session = make_session_factory(args.database_url)
    with Session() as session:
        stmt = (
            select(
                InstrumentUnit.instrument_unit_id,
                InstrumentUnit.instrument,
                InstrumentUnit.site,
                InstrumentUnit.tube,
                InstrumentUnit.camera,
                InstrumentUnit.filter_name,
                InstrumentUnit.observing_mode,
                SourceDetection.detection_status,
                SourceDetection.algorithm,
                func.count(SourceDetection.source_detection_id).label("n_detections"),
                func.min(SourceDetection.snr).label("snr_min"),
                func.max(SourceDetection.snr).label("snr_max"),
            )
            .select_from(SourceDetection)
            .join(Frame, Frame.frame_id == SourceDetection.frame_id)
            .outerjoin(InstrumentUnit, InstrumentUnit.instrument_unit_id == Frame.instrument_unit_id)
            .group_by(
                InstrumentUnit.instrument_unit_id,
                InstrumentUnit.instrument,
                InstrumentUnit.site,
                InstrumentUnit.tube,
                InstrumentUnit.camera,
                InstrumentUnit.filter_name,
                InstrumentUnit.observing_mode,
                SourceDetection.detection_status,
                SourceDetection.algorithm,
            )
            .order_by(InstrumentUnit.instrument_unit_id, SourceDetection.detection_status)
        )
        rows = []
        if args.calibration_name:
            stmt = stmt.where(CalibrationVersion.name == args.calibration_name)
        if args.instrument_unit_id is not None:
            stmt = stmt.where(InstrumentUnit.instrument_unit_id == int(args.instrument_unit_id))

        for row in session.execute(stmt).all():
            d = dict(row._mapping)
            d["snr_min"] = _round_float(d.get("snr_min"), 3)
            d["snr_max"] = _round_float(d.get("snr_max"), 3)
            rows.append(d)
        if args.offset:
            rows = rows[int(args.offset):]
        if args.limit is not None:
            rows = rows[: int(args.limit)]
    columns = [
        "instrument_unit_id",
        "instrument",
        "site",
        "tube",
        "camera",
        "filter_name",
        "observing_mode",
        "detection_status",
        "algorithm",
        "n_detections",
        "snr_min",
        "snr_max",
    ]
    _print_rows(rows, columns, args.format)
    return 0


def cmd_show_source_detections(args: argparse.Namespace) -> int:
    from sqlalchemy import select
    from grandgousier.db.models import Frame, InstrumentUnit, SourceDetection

    Session = make_session_factory(args.database_url)
    with Session() as session:
        stmt = (
            select(
                SourceDetection.source_detection_id,
                SourceDetection.frame_id,
                InstrumentUnit.instrument,
                InstrumentUnit.tube,
                InstrumentUnit.camera,
                InstrumentUnit.filter_name,
                Frame.object_name.label("frame_object"),
                Frame.date_obs,
                SourceDetection.x,
                SourceDetection.y,
                SourceDetection.ra_deg,
                SourceDetection.dec_deg,
                SourceDetection.peak_adu,
                SourceDetection.flux,
                SourceDetection.snr,
                SourceDetection.local_background,
                SourceDetection.detection_status,
                SourceDetection.algorithm,
            )
            .select_from(SourceDetection)
            .join(Frame, Frame.frame_id == SourceDetection.frame_id)
            .outerjoin(InstrumentUnit, InstrumentUnit.instrument_unit_id == Frame.instrument_unit_id)
            .order_by(SourceDetection.source_detection_id)
            .offset(args.offset)
            .limit(args.limit)
        )
        if args.frame_id is not None:
            stmt = stmt.where(SourceDetection.frame_id == args.frame_id)
        if args.instrument:
            stmt = stmt.where(Frame.instrument == args.instrument)
        if args.instrument_unit_id is not None:
            stmt = stmt.where(Frame.instrument_unit_id == args.instrument_unit_id)
        rows = []
        if args.calibration_name:
            stmt = stmt.where(CalibrationVersion.name == args.calibration_name)
        if args.instrument_unit_id is not None:
            stmt = stmt.where(InstrumentUnit.instrument_unit_id == int(args.instrument_unit_id))

        for row in session.execute(stmt).all():
            d = dict(row._mapping)
            d["date_obs"] = _format_dt(d.get("date_obs"))
            for key, nd in (("x", 3), ("y", 3), ("ra_deg", 8), ("dec_deg", 8), ("peak_adu", 3), ("flux", 3), ("snr", 3), ("local_background", 3)):
                d[key] = _round_float(d.get(key), nd)
            rows.append(d)
        if args.offset:
            rows = rows[int(args.offset):]
        if args.limit is not None:
            rows = rows[: int(args.limit)]
    columns = [
        "source_detection_id",
        "frame_id",
        "instrument",
        "tube",
        "camera",
        "filter_name",
        "frame_object",
        "date_obs",
        "x",
        "y",
        "ra_deg",
        "dec_deg",
        "peak_adu",
        "flux",
        "snr",
        "local_background",
        "detection_status",
        "algorithm",
    ]
    _print_rows(rows, columns, args.format)
    return 0


def cmd_query_gaia_for_frames(args: argparse.Namespace) -> int:
    from grandgousier.catalogs.gaia_query import query_gaia_for_frames

    Session = make_session_factory(args.database_url)
    with Session() as session:
        result = query_gaia_for_frames(
            session,
            catalog_name=args.catalog_name,
            frame_ids=args.frame_id,
            instrument=args.instrument,
            instrument_unit_id=args.instrument_unit_id,
            only_wcs_ok=not args.all_wcs_statuses,
            margin_arcmin=args.margin_arcmin,
            mag_limit=args.mag_limit,
            max_rows_per_frame=args.max_rows_per_frame,
            query_mode=args.query_mode,
            max_radius_deg=args.max_radius_deg,
            gaia_timeout_sec=args.gaia_timeout_sec,
            match=args.match,
            match_margin_pix=args.match_margin_pix,
            limit_frames=args.limit_frames,
            dry_run=args.dry_run,
        )
    print(
        "Gaia query result: "
        f"catalog_name={result.catalog_name}, query_mode={result.query_mode}, selected_frames={result.selected_frames}, "
        f"queried_frames={result.queried_frames}, queried_cones={result.queried_cones}, read_rows={result.read_rows}, "
        f"inserted_or_updated={result.inserted_or_updated}, linked={result.linked}, "
        f"skipped_missing_file={result.skipped_missing_file}, skipped_oversized_cones={result.skipped_oversized_cones}, failed_frames={result.failed_frames}, "
        f"failed_footprint_reasons={result.failed_footprint_reasons}, "
        f"dry_run={result.dry_run}"
    )
    return 0 if result.failed_frames == 0 else 2



def cmd_match_detections_to_catalog(args: argparse.Namespace) -> int:
    from grandgousier.quality.detection_catalog_qc import match_detections_to_catalog

    Session = make_session_factory(args.database_url)
    with Session() as session:
        result = match_detections_to_catalog(
            session,
            catalog_name=args.catalog_name,
            frame_ids=args.frame_id,
            instrument=args.instrument,
            instrument_unit_id=args.instrument_unit_id,
            only_wcs_ok=not args.all_wcs_statuses,
            detection_algorithm=args.detection_algorithm,
            radius_arcsec=args.radius_arcsec,
            mag_key=args.mag_key,
            min_snr=args.min_snr,
            limit_frames=args.limit_frames,
            replace_existing=args.replace_existing,
            dry_run=args.dry_run,
        )
    print(
        "Detection-catalog match result: "
        f"catalog_name={result.catalog_name}, selected_frames={result.selected_frames}, "
        f"checked_frames={result.checked_frames}, selected_detections={result.selected_detections}, "
        f"selected_catalog_sources={result.selected_catalog_sources}, matched={result.matched}, "
        f"frame_qc_written={result.frame_qc_written}, deleted_existing_matches={result.deleted_existing_matches}, "
        f"failed_frames={result.failed_frames}, dry_run={result.dry_run}"
    )
    return 0 if result.failed_frames == 0 else 2


def cmd_cleanup_duplicate_detection_matches(args: argparse.Namespace) -> int:
    from grandgousier.quality.detection_cleanup import cleanup_duplicate_detection_matches

    Session = make_session_factory(args.database_url)
    with Session() as session:
        result = cleanup_duplicate_detection_matches(
            session,
            catalog_name=args.catalog_name,
            instrument_unit_id=args.instrument_unit_id,
            algorithm=args.algorithm,
            delete_orphans=not args.keep_orphans,
            dry_run=args.dry_run,
        )
    print(
        "Duplicate detection-match cleanup result: "
        f"catalog_name={result.catalog_name}, instrument_unit_id={result.instrument_unit_id}, "
        f"algorithm={result.algorithm}, duplicate_groups={result.duplicate_groups}, "
        f"duplicate_rows_deleted={result.duplicate_rows_deleted}, "
        f"orphan_matches_deleted={result.orphan_matches_deleted}, dry_run={result.dry_run}"
    )
    return 0


def cmd_source_catalog_match_summary(args: argparse.Namespace) -> int:
    from sqlalchemy import func, select
    from grandgousier.db.models import DetectionCatalogMatch, Frame, InstrumentUnit

    Session = make_session_factory(args.database_url)
    with Session() as session:
        stmt = (
            select(
                InstrumentUnit.instrument_unit_id,
                InstrumentUnit.instrument,
                InstrumentUnit.site,
                InstrumentUnit.tube,
                InstrumentUnit.camera,
                InstrumentUnit.filter_name,
                InstrumentUnit.observing_mode,
                DetectionCatalogMatch.catalog_name,
                DetectionCatalogMatch.algorithm,
                func.count(DetectionCatalogMatch.detection_catalog_match_id).label("n_matches"),
                func.avg(DetectionCatalogMatch.sep_arcsec).label("sep_mean_arcsec"),
                func.min(DetectionCatalogMatch.sep_arcsec).label("sep_min_arcsec"),
                func.max(DetectionCatalogMatch.sep_arcsec).label("sep_max_arcsec"),
                func.avg(DetectionCatalogMatch.zero_point).label("zp_mean"),
            )
            .select_from(DetectionCatalogMatch)
            .join(Frame, Frame.frame_id == DetectionCatalogMatch.frame_id)
            .outerjoin(InstrumentUnit, InstrumentUnit.instrument_unit_id == Frame.instrument_unit_id)
            .group_by(
                InstrumentUnit.instrument_unit_id,
                InstrumentUnit.instrument,
                InstrumentUnit.site,
                InstrumentUnit.tube,
                InstrumentUnit.camera,
                InstrumentUnit.filter_name,
                InstrumentUnit.observing_mode,
                DetectionCatalogMatch.catalog_name,
                DetectionCatalogMatch.algorithm,
            )
            .order_by(InstrumentUnit.instrument_unit_id, DetectionCatalogMatch.catalog_name)
        )
        if args.catalog_name:
            stmt = stmt.where(DetectionCatalogMatch.catalog_name == args.catalog_name)
        if args.instrument_unit_id is not None:
            stmt = stmt.where(Frame.instrument_unit_id == args.instrument_unit_id)
        rows = []
        if args.calibration_name:
            stmt = stmt.where(CalibrationVersion.name == args.calibration_name)
        if args.instrument_unit_id is not None:
            stmt = stmt.where(InstrumentUnit.instrument_unit_id == int(args.instrument_unit_id))

        for row in session.execute(stmt).all():
            d = dict(row._mapping)
            for key in ("sep_mean_arcsec", "sep_min_arcsec", "sep_max_arcsec"):
                d[key] = _round_float(d.get(key), 4)
            d["zp_mean"] = _round_float(d.get("zp_mean"), 5)
            rows.append(d)
        if args.offset:
            rows = rows[int(args.offset):]
        if args.limit is not None:
            rows = rows[: int(args.limit)]
    columns = [
        "instrument_unit_id",
        "instrument",
        "site",
        "tube",
        "camera",
        "filter_name",
        "observing_mode",
        "catalog_name",
        "algorithm",
        "n_matches",
        "sep_mean_arcsec",
        "sep_min_arcsec",
        "sep_max_arcsec",
        "zp_mean",
    ]
    _print_rows(rows, columns, args.format)
    return 0


def cmd_show_source_catalog_matches(args: argparse.Namespace) -> int:
    from sqlalchemy import select
    from grandgousier.db.models import CatalogSource, DetectionCatalogMatch, Frame, InstrumentUnit, SourceDetection

    Session = make_session_factory(args.database_url)
    with Session() as session:
        stmt = (
            select(
                DetectionCatalogMatch.detection_catalog_match_id,
                DetectionCatalogMatch.frame_id,
                InstrumentUnit.instrument,
                InstrumentUnit.tube,
                InstrumentUnit.camera,
                InstrumentUnit.filter_name,
                Frame.object_name.label("frame_object"),
                Frame.date_obs,
                DetectionCatalogMatch.catalog_name,
                CatalogSource.source_id,
                SourceDetection.x.label("det_x"),
                SourceDetection.y.label("det_y"),
                DetectionCatalogMatch.dx_pix,
                DetectionCatalogMatch.dy_pix,
                DetectionCatalogMatch.sep_arcsec,
                SourceDetection.snr,
                DetectionCatalogMatch.detection_mag_inst,
                DetectionCatalogMatch.catalog_mag,
                DetectionCatalogMatch.zero_point,
                DetectionCatalogMatch.match_status,
            )
            .select_from(DetectionCatalogMatch)
            .join(Frame, Frame.frame_id == DetectionCatalogMatch.frame_id)
            .join(SourceDetection, SourceDetection.source_detection_id == DetectionCatalogMatch.source_detection_id)
            .join(CatalogSource, CatalogSource.catalog_source_id == DetectionCatalogMatch.catalog_source_id)
            .outerjoin(InstrumentUnit, InstrumentUnit.instrument_unit_id == Frame.instrument_unit_id)
            .order_by(DetectionCatalogMatch.detection_catalog_match_id)
            .offset(args.offset)
            .limit(args.limit)
        )
        if args.catalog_name:
            stmt = stmt.where(DetectionCatalogMatch.catalog_name == args.catalog_name)
        if args.frame_id is not None:
            stmt = stmt.where(DetectionCatalogMatch.frame_id == args.frame_id)
        if args.instrument:
            stmt = stmt.where(Frame.instrument == args.instrument)
        if args.instrument_unit_id is not None:
            stmt = stmt.where(Frame.instrument_unit_id == args.instrument_unit_id)
        rows = []
        if args.calibration_name:
            stmt = stmt.where(CalibrationVersion.name == args.calibration_name)
        if args.instrument_unit_id is not None:
            stmt = stmt.where(InstrumentUnit.instrument_unit_id == int(args.instrument_unit_id))

        for row in session.execute(stmt).all():
            d = dict(row._mapping)
            d["date_obs"] = _format_dt(d.get("date_obs"))
            for key, nd in (
                ("det_x", 3),
                ("det_y", 3),
                ("dx_pix", 4),
                ("dy_pix", 4),
                ("sep_arcsec", 4),
                ("snr", 3),
                ("detection_mag_inst", 5),
                ("catalog_mag", 5),
                ("zero_point", 5),
            ):
                d[key] = _round_float(d.get(key), nd)
            rows.append(d)
        if args.offset:
            rows = rows[int(args.offset):]
        if args.limit is not None:
            rows = rows[: int(args.limit)]
    columns = [
        "detection_catalog_match_id",
        "frame_id",
        "instrument",
        "tube",
        "camera",
        "filter_name",
        "frame_object",
        "date_obs",
        "catalog_name",
        "source_id",
        "det_x",
        "det_y",
        "dx_pix",
        "dy_pix",
        "sep_arcsec",
        "snr",
        "detection_mag_inst",
        "catalog_mag",
        "zero_point",
        "match_status",
    ]
    _print_rows(rows, columns, args.format)
    return 0


def cmd_frame_qc_summary(args: argparse.Namespace) -> int:
    from sqlalchemy import select
    from grandgousier.db.models import Frame, FrameQc, InstrumentUnit

    Session = make_session_factory(args.database_url)
    with Session() as session:
        stmt = (
            select(
                FrameQc.frame_qc_id,
                FrameQc.frame_id,
                InstrumentUnit.instrument,
                InstrumentUnit.tube,
                InstrumentUnit.camera,
                InstrumentUnit.filter_name,
                Frame.object_name.label("frame_object"),
                Frame.date_obs,
                FrameQc.catalog_name,
                FrameQc.n_detections,
                FrameQc.n_catalog_sources,
                FrameQc.n_matched,
                FrameQc.astrom_rms_arcsec,
                FrameQc.sep_median_arcsec,
                FrameQc.sep_p95_arcsec,
                FrameQc.dx_median_pix,
                FrameQc.dy_median_pix,
                FrameQc.zp_n,
                FrameQc.zp_median,
                FrameQc.zp_mad,
            )
            .select_from(FrameQc)
            .join(Frame, Frame.frame_id == FrameQc.frame_id)
            .outerjoin(InstrumentUnit, InstrumentUnit.instrument_unit_id == Frame.instrument_unit_id)
            .order_by(FrameQc.frame_id)
            .offset(args.offset)
            .limit(args.limit)
        )
        if args.catalog_name:
            stmt = stmt.where(FrameQc.catalog_name == args.catalog_name)
        if args.instrument:
            stmt = stmt.where(Frame.instrument == args.instrument)
        if args.instrument_unit_id is not None:
            stmt = stmt.where(Frame.instrument_unit_id == args.instrument_unit_id)
        rows = []
        if args.calibration_name:
            stmt = stmt.where(CalibrationVersion.name == args.calibration_name)
        if args.instrument_unit_id is not None:
            stmt = stmt.where(InstrumentUnit.instrument_unit_id == int(args.instrument_unit_id))

        for row in session.execute(stmt).all():
            d = dict(row._mapping)
            d["date_obs"] = _format_dt(d.get("date_obs"))
            for key, nd in (
                ("astrom_rms_arcsec", 4),
                ("sep_median_arcsec", 4),
                ("sep_p95_arcsec", 4),
                ("dx_median_pix", 4),
                ("dy_median_pix", 4),
                ("zp_median", 5),
                ("zp_mad", 5),
            ):
                d[key] = _round_float(d.get(key), nd)
            rows.append(d)
        if args.offset:
            rows = rows[int(args.offset):]
        if args.limit is not None:
            rows = rows[: int(args.limit)]
    columns = [
        "frame_qc_id",
        "frame_id",
        "instrument",
        "tube",
        "camera",
        "filter_name",
        "frame_object",
        "date_obs",
        "catalog_name",
        "n_detections",
        "n_catalog_sources",
        "n_matched",
        "astrom_rms_arcsec",
        "sep_median_arcsec",
        "sep_p95_arcsec",
        "dx_median_pix",
        "dy_median_pix",
        "zp_n",
        "zp_median",
        "zp_mad",
    ]
    _print_rows(rows, columns, args.format)
    return 0


def cmd_compute_object_qc(args: argparse.Namespace) -> int:
    from grandgousier.quality.object_quality import compute_object_qc

    Session = make_session_factory(args.database_url)
    with Session() as session:
        result = compute_object_qc(
            session,
            calibration_name=args.calibration_name,
            catalog_name=args.catalog_name,
            instrument_unit_id=args.instrument_unit_id,
            min_measurements=args.min_measurements,
            algorithm=args.algorithm,
            crowded_arcsec=args.crowded_arcsec,
            blended_arcsec=args.blended_arcsec,
            edge_distance_pix=args.edge_distance_pix,
            high_rms=args.high_rms,
            high_mag_err=args.high_mag_err,
            min_detection_fraction=args.min_detection_fraction,
            max_ref_mag_delta=args.max_ref_mag_delta,
            proper_motion_sep_arcsec=args.proper_motion_sep_arcsec,
            dynamic_separation=args.dynamic_separation,
            dynamic_seeing_factor=args.dynamic_seeing_factor,
            dynamic_aperture_factor=args.dynamic_aperture_factor,
            dynamic_crowding_factor=args.dynamic_crowding_factor,
            min_dynamic_blend_arcsec=args.min_dynamic_blend_arcsec,
            fwhm_header_unit=args.fwhm_header_unit,
            dry_run=args.dry_run,
        )
    print(
        "Object QC result: "
        f"calibration_version_id={result.calibration_version_id}, "
        f"calibration_name={result.calibration_name}, catalog_name={result.catalog_name}, "
        f"instrument_unit_id={result.instrument_unit_id}, selected_objects={result.selected_objects}, "
        f"checked_objects={result.checked_objects}, written={result.written}, "
        f"crowded={result.crowded}, blended_neighbor={result.blended_neighbor}, "
        f"edge={result.edge}, saturated_like={result.saturated_like}, "
        f"high_rms={result.high_rms}, faint_noisy={result.faint_noisy}, "
        f"no_detection_support={result.no_detection_support}, "
        f"weak_detection_support={result.weak_detection_support}, "
        f"large_catalog_mag_residual={result.large_catalog_mag_residual}, "
        f"possible_proper_motion_mismatch={result.possible_proper_motion_mismatch}, "
        f"dynamic_crowded={result.dynamic_crowded}, dynamic_blend_risk={result.dynamic_blend_risk}, "
        f"possible_blend={result.possible_blend}, "
        f"aperture_unstable={result.aperture_unstable}, "
        f"aperture_median_shift={result.aperture_median_shift}, "
        f"multiple_detections_per_frame={getattr(result, 'multiple_detections_per_frame', 0)}, "
        f"preserved_aperture_stability={result.preserved_aperture_stability}, "
        f"dry_run={result.dry_run}"
    )
    return 0


def cmd_object_qc_summary(args: argparse.Namespace) -> int:
    from sqlalchemy import select
    from grandgousier.db.models import CalibrationVersion, InstrumentUnit, ObjectQc

    Session = make_session_factory(args.database_url)
    with Session() as session:
        stmt = (
            select(ObjectQc, CalibrationVersion, InstrumentUnit)
            .outerjoin(CalibrationVersion, CalibrationVersion.calibration_version_id == ObjectQc.calibration_version_id)
            .outerjoin(InstrumentUnit, InstrumentUnit.instrument_unit_id == ObjectQc.instrument_unit_id)
        )
        if args.calibration_name:
            stmt = stmt.where(CalibrationVersion.name == args.calibration_name)
        if args.catalog_name:
            stmt = stmt.where(ObjectQc.catalog_name == args.catalog_name)
        if args.instrument_unit_id is not None:
            stmt = stmt.where(ObjectQc.instrument_unit_id == args.instrument_unit_id)
        if args.algorithm:
            stmt = stmt.where(ObjectQc.algorithm == args.algorithm)
        rows_raw = session.execute(stmt).all()

    total = len(rows_raw)
    flag_names = [
        "CROWDED",
        "BLENDED_NEIGHBOR",
        "EDGE",
        "SATURATED_LIKE",
        "HIGH_RMS",
        "FAINT_NOISY",
        "NEIGHBOR_WITHIN_3ARCSEC",
        "NEIGHBOR_WITHIN_5ARCSEC",
        "NO_DETECTION_SUPPORT",
        "WEAK_DETECTION_SUPPORT",
        "LARGE_CATALOG_MAG_RESIDUAL",
        "POSSIBLE_PROPER_MOTION_MISMATCH",
    ]
    counts = {name: 0 for name in flag_names}
    nn_values = []
    edge_values = []
    rms_values = []
    for qc, _cv, _iu in rows_raw:
        flags = qc.flags or {}
        for name in flag_names:
            counts[name] += int(bool(flags.get(name)))
        if qc.nearest_neighbor_arcsec is not None:
            nn_values.append(float(qc.nearest_neighbor_arcsec))
        if qc.edge_distance_pix_min is not None:
            edge_values.append(float(qc.edge_distance_pix_min))
        if qc.mag_rms is not None:
            rms_values.append(float(qc.mag_rms))

    rows = [
        {
            "metric": "total_objects",
            "count": total,
            "fraction": 1.0 if total else None,
        }
    ]
    for name in flag_names:
        rows.append({"metric": name, "count": counts[name], "fraction": (counts[name] / total) if total else None})
    rows.extend(
        [
            {"metric": "nearest_neighbor_median_arcsec", "count": None, "fraction": _median_float(nn_values)},
            {"metric": "edge_distance_median_pix", "count": None, "fraction": _median_float(edge_values)},
            {"metric": "mag_rms_median", "count": None, "fraction": _median_float(rms_values)},
        ]
    )
    for row in rows:
        row["fraction"] = _round_float(row.get("fraction"), 5)
    _print_rows(rows, ["metric", "count", "fraction"], args.format)
    return 0


def cmd_show_object_qc(args: argparse.Namespace) -> int:
    from sqlalchemy import select
    from grandgousier.db.models import CalibrationVersion, InstrumentUnit, Object, ObjectQc

    Session = make_session_factory(args.database_url)
    with Session() as session:
        stmt = (
            select(ObjectQc, Object, CalibrationVersion, InstrumentUnit)
            .join(Object, Object.object_id == ObjectQc.object_id)
            .outerjoin(CalibrationVersion, CalibrationVersion.calibration_version_id == ObjectQc.calibration_version_id)
            .outerjoin(InstrumentUnit, InstrumentUnit.instrument_unit_id == ObjectQc.instrument_unit_id)
            .order_by(ObjectQc.object_qc_id)
        )
        if args.calibration_name:
            stmt = stmt.where(CalibrationVersion.name == args.calibration_name)
        if args.catalog_name:
            stmt = stmt.where(ObjectQc.catalog_name == args.catalog_name)
        if args.instrument_unit_id is not None:
            stmt = stmt.where(ObjectQc.instrument_unit_id == args.instrument_unit_id)
        if args.algorithm:
            stmt = stmt.where(ObjectQc.algorithm == args.algorithm)
        rows = []
        for qc, obj, cv, iu in session.execute(stmt).all():
            flags = qc.flags or {}
            if args.flag and not all(bool(flags.get(name)) for name in args.flag):
                continue
            if args.exclude_flag and any(bool(flags.get(name)) for name in args.exclude_flag):
                continue
            details = qc.details or {}
            det = details.get("detection_support", {}) if isinstance(details, dict) else {}
            phot = details.get("photometric_consistency", {}) if isinstance(details, dict) else {}
            d = {
                "object_qc_id": qc.object_qc_id,
                "object_id": qc.object_id,
                "calibration_name": getattr(cv, "name", None),
                "instrument_unit_id": qc.instrument_unit_id,
                "instrument": getattr(iu, "instrument", None),
                "filter_name": getattr(iu, "filter_name", None),
                "catalog_name": qc.catalog_name,
                "source_id": (qc.details or {}).get("source_id"),
                "ra_deg": obj.ra_deg,
                "dec_deg": obj.dec_deg,
                "nearest_neighbor_arcsec": qc.nearest_neighbor_arcsec,
                "neighbors_3": qc.neighbors_within_3arcsec,
                "neighbors_5": qc.neighbors_within_5arcsec,
                "neighbors_10": qc.neighbors_within_10arcsec,
                "edge_distance_pix_min": qc.edge_distance_pix_min,
                "n_measurements": qc.n_measurements,
                "mag_median": qc.mag_median,
                "mag_rms": qc.mag_rms,
                "mag_mad": qc.mag_mad,
                "mag_err_median": qc.mag_err_median,
                "n_detection_matches": det.get("n_detection_matches"),
                "n_detection_match_frames": det.get("n_detection_match_frames", det.get("n_detection_matches")),
                "n_detection_matches_raw": det.get("n_detection_matches_raw", det.get("n_detection_matches")),
                "n_extra_detection_matches": det.get("n_extra_detection_matches"),
                "n_multiple_detection_frames": det.get("n_multiple_detection_frames"),
                "detection_fraction": det.get("detection_fraction"),
                "detection_frame_fraction": det.get("detection_frame_fraction", det.get("detection_fraction")),
                "multiple_detection_frame_ratio": det.get("multiple_detection_frame_ratio"),
                "det_sep_median_arcsec": det.get("det_sep_median_arcsec"),
                "det_snr_median": det.get("det_snr_median"),
                "det_snr_max": det.get("det_snr_max"),
                "ref_mag": phot.get("ref_mag"),
                "mag_ref_delta": phot.get("mag_ref_delta"),
                "abs_mag_ref_delta": phot.get("abs_mag_ref_delta"),
                "flags": ",".join(sorted(k for k, v in flags.items() if v)),
            }
            for key, nd in (
                ("ra_deg", 8),
                ("dec_deg", 8),
                ("nearest_neighbor_arcsec", 4),
                ("edge_distance_pix_min", 3),
                ("mag_median", 5),
                ("mag_rms", 5),
                ("mag_mad", 5),
                ("rms_over_mag_err", 3),
            ("mad_over_mag_err", 3),
            ("effective_mag_err_median", 5),
            ("intrinsic_variability_score", 3),
            ("mag_err_median", 5),
                ("detection_fraction", 3),
                ("det_sep_median_arcsec", 4),
                ("det_snr_median", 3),
                ("det_snr_max", 3),
                ("ref_mag", 5),
                ("mag_ref_delta", 5),
                ("abs_mag_ref_delta", 5),
            ):
                d[key] = _round_float(d.get(key), nd)
            rows.append(d)
        if args.offset:
            rows = rows[int(args.offset):]
        if args.limit is not None:
            rows = rows[: int(args.limit)]
    columns = [
        "object_qc_id",
        "object_id",
        "calibration_name",
        "instrument_unit_id",
        "instrument",
        "filter_name",
        "catalog_name",
        "source_id",
        "ra_deg",
        "dec_deg",
        "nearest_neighbor_arcsec",
        "neighbors_3",
        "neighbors_5",
        "neighbors_10",
        "edge_distance_pix_min",
        "n_measurements",
        "mag_median",
        "mag_rms",
        "mag_mad",
        "mag_err_median",
        "n_detection_matches",
        "n_detection_match_frames",
        "n_detection_matches_raw",
        "n_extra_detection_matches",
        "n_multiple_detection_frames",
        "detection_fraction",
        "detection_frame_fraction",
        "multiple_detection_frame_ratio",
        "det_sep_median_arcsec",
        "det_snr_median",
        "det_snr_max",
        "ref_mag",
        "mag_ref_delta",
        "abs_mag_ref_delta",
        "flags",
    ]
    _print_rows(rows, columns, args.format)
    return 0


def _parse_radii_for_cli(value: str | None) -> list[float]:
    from grandgousier.quality.aperture_sweep import parse_radii

    return parse_radii(value or "2,3,4,5")


def _split_comma_list(value: str | None) -> set[str] | None:
    if value is None:
        return None
    items = {part.strip() for part in str(value).split(",") if part.strip()}
    return items or None


def _add_science_lc_scope_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--calibration-name",
        action="append",
        default=[],
        help="CalibrationVersion name; may be repeated for multi-series/global light curves",
    )
    parser.add_argument(
        "--calibration-prefix",
        action="append",
        default=[],
        help="CalibrationVersion name prefix; may be repeated",
    )
    parser.add_argument("--model-name", default=None, help="CalibrationVersion.model_name filter")
    parser.add_argument("--catalog-name", default=None, help="Catalog namespace in measurement flags")
    parser.add_argument("--instrument-unit-id", type=int, default=None, help="Restrict to one instrument unit")
    parser.add_argument("--series-id", type=int, default=None, help="Restrict to one field_series.series_id")
    parser.add_argument(
        "--field-series-name",
        default=None,
        help="Restrict to one field_series identifier/pipeline/calibration prefix",
    )
    parser.add_argument("--frame-id", type=int, action="append", default=[], help="Explicit frame_id; may be repeated")
    parser.add_argument("--object-qc-algorithm", default="object_qc_v1", help="object_qc.algorithm")
    parser.add_argument("--offset-algorithm", default="interseries_median_v1", help="interseries_calibration_offsets.algorithm")
    parser.add_argument(
        "--reject-flag",
        action="append",
        default=[],
        help="Additional reject flag; may be repeated or comma-separated",
    )
    parser.add_argument(
        "--reject-flags",
        default=None,
        help="Comma-separated additional reject flags",
    )
    parser.add_argument(
        "--no-default-reject-flags",
        action="store_true",
        help="Do not use built-in science reject flags; only use --reject-flag/--reject-flags",
    )


def _science_reject_flags_from_args(args: argparse.Namespace) -> set[str]:
    from grandgousier.lightcurves.product import parse_reject_flags

    extra: list[str] = []
    if getattr(args, "reject_flags", None):
        extra.append(str(args.reject_flags))
    for item in getattr(args, "reject_flag", None) or []:
        extra.append(str(item))
    return parse_reject_flags(",".join(extra), use_defaults=not getattr(args, "no_default_reject_flags", False))


def _median_float(values: list[float]) -> float | None:
    if not values:
        return None
    return float(sorted(values)[len(values) // 2]) if len(values) % 2 else float((sorted(values)[len(values)//2 - 1] + sorted(values)[len(values)//2]) / 2.0)


def cmd_db_summary(args: argparse.Namespace) -> int:
    from sqlalchemy import func, select
    from grandgousier.db.models import CatalogSource, DetectionCatalogMatch, Frame, FrameCatalogLink, FrameQc, InstrumentUnit, ProcessingEvent, Object, ObjectQc, Measurement, CalibrationVersion, SourceDetection

    Session = make_session_factory(args.database_url)
    with Session() as session:
        n_frames = session.scalar(select(func.count(Frame.frame_id)))
        n_events = session.scalar(select(func.count(ProcessingEvent.event_id)))
        n_units = session.scalar(select(func.count(InstrumentUnit.instrument_unit_id)))
        n_catalog_sources = session.scalar(select(func.count(CatalogSource.catalog_source_id)))
        n_frame_catalog_links = session.scalar(select(func.count(FrameCatalogLink.frame_catalog_link_id)))
        n_objects = session.scalar(select(func.count(Object.object_id)))
        n_measurements = session.scalar(select(func.count(Measurement.measurement_id)))
        n_calibration_versions = session.scalar(select(func.count(CalibrationVersion.calibration_version_id)))
        n_source_detections = session.scalar(select(func.count(SourceDetection.source_detection_id)))
        n_detection_catalog_matches = session.scalar(select(func.count(DetectionCatalogMatch.detection_catalog_match_id)))
        n_frame_qc = session.scalar(select(func.count(FrameQc.frame_qc_id)))
        n_object_qc = session.scalar(select(func.count(ObjectQc.object_qc_id)))
        by_status = session.execute(
            select(Frame.processing_status, func.count(Frame.frame_id)).group_by(Frame.processing_status)
        ).all()
        by_wcs = session.execute(
            select(Frame.wcs_status, func.count(Frame.frame_id)).group_by(Frame.wcs_status)
        ).all()
    print(f"frames={n_frames}")
    print(f"instrument_units={n_units}")
    print(f"catalog_sources={n_catalog_sources}")
    print(f"frame_catalog_links={n_frame_catalog_links}")
    print(f"objects={n_objects}")
    print(f"measurements={n_measurements}")
    print(f"calibration_versions={n_calibration_versions}")
    print(f"source_detections={n_source_detections}")
    print(f"detection_catalog_matches={n_detection_catalog_matches}")
    print(f"frame_qc={n_frame_qc}")
    print(f"object_qc={n_object_qc}")
    print(f"processing_events={n_events}")
    print("processing_status:")
    for status, count in by_status:
        print(f"  {status.value}: {count}")
    print("wcs_status:")
    for status, count in by_wcs:
        print(f"  {status}: {count}")
    return 0


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.log_level or settings.log_level)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
