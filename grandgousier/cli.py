from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import traceback
from pathlib import Path

from grandgousier import __version__
from grandgousier.db.init_db import init_db, reset_db
from grandgousier.db.session import make_session_factory
from grandgousier.logging_config import configure_logging
from grandgousier.logging_utils import sanitize_diagnostic_text
from grandgousier.settings import get_settings

logger = logging.getLogger(__name__)

_P207_GUARDED_LEDGER_FLAG = "--guard-p207-test-readonly-ledger"
_P207_PRIVATE_CONFIG_ENV = "GRANDGOUSIER_P207_PRIVATE_CONFIG"
_P207_LEGACY_PRIVATE_CONFIG_BASENAME = "grandgousier_p207_test_ro.private.yaml"
_P207_GUARDED_PROBE_FLAG = "--guard-p207-test-readonly-probe"
_P207_GUARDED_INTEGRITY_FLAG = "--guard-p207-test-readonly-integrity"
_P207_PROBE_FAILURE_CODES = frozenset(
    {f"P{position:02d}" for position in range(15)} | {"ROW_SHAPE", "ROW_LIST_SLICING"}
)


class _SanitizingStderr:
    """Sanitize CLI stderr writes while forwarding to the currently active stream."""

    def __init__(self, original_stream):
        self._original_stream = original_stream

    def _target(self):
        target = sys.stderr
        return self._original_stream if target is self else target

    def write(self, value):
        return self._target().write(sanitize_diagnostic_text(value))

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def flush(self):
        return self._target().flush()

    def __getattr__(self, name):
        return getattr(self._original_stream, name)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="grandgousier",
        description="Grandgousier archive photometry engine",
    )
    parser.add_argument("--database-url", default=None, help="SQLAlchemy PostgreSQL URL")
    parser.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING, ERROR")
    parser.add_argument(
        "--config",
        dest="config_file",
        default=None,
        help="Optional Grandgousier YAML/JSON application config (place before the subcommand, or use the command-local alias where available)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init-db", help="Create PostgreSQL tables")
    p_init.set_defaults(func=cmd_init_db)

    p_reset = sub.add_parser("reset-db", help="Drop and recreate all Grandgousier tables")
    p_reset.add_argument("--yes", action="store_true", help="Confirm destructive reset")
    p_reset.set_defaults(func=cmd_reset_db)

    p_reset_derived = sub.add_parser(
        "reset-derived-data",
        help="Audit or reset derived Stage-2/operational data while preserving archive identity",
    )
    p_reset_derived.add_argument(
        "--scope",
        choices=("stage2",),
        required=True,
        help="Derived-data scope; currently only stage2 is supported",
    )
    p_reset_derived.add_argument(
        "--all-series",
        action="store_true",
        help="Required safety acknowledgement that every observing series is in scope",
    )
    p_reset_derived.add_argument(
        "--execute",
        action="store_true",
        help="Execute the reset; without this flag the command is read-only",
    )
    p_reset_derived.add_argument(
        "--yes",
        action="store_true",
        help="Confirm destructive execution together with --execute",
    )
    p_reset_derived.add_argument(
        "--format", choices=("table", "json"), default="table", help="Audit/result output format"
    )
    p_reset_derived.set_defaults(func=cmd_reset_derived_data)

    p_parquet = sub.add_parser(
        "export-science-parquet", help="Export immutable final SCIENCE products as read-only Parquet"
    )
    p_parquet.add_argument("--database-url", required=True, help="Explicit PostgreSQL URL (read-only transaction)")
    p_parquet.add_argument("--series-id", type=int, action="append", required=True, help="field_series.series_id; repeatable")
    p_parquet.add_argument("--calibration-version-id", type=int, default=None, help="Disambiguate a series with multiple published SCIENCE heads")
    p_parquet.add_argument("--interseries-generation-id", type=int, default=None, help="Explicit Stage 3 generation; omitted exports Stage 2 only")
    p_parquet.add_argument("--out", required=True, help="Absolute output path; must not already exist")
    p_parquet.add_argument("--db-logical-id", default="unspecified", help="Non-secret logical DB label for manifest")
    p_parquet.add_argument("--batch-size", type=int, default=4096)
    p_parquet.set_defaults(func=cmd_export_science_parquet)

    p_reclaim = sub.add_parser(
        "reclaim-stage1-products",
        help="Plan or execute safe reclaim of managed Stage-1 products for one completed series",
    )
    p_reclaim.add_argument("--series-id", type=int, required=True, help="Exactly one field_series.series_id")
    p_reclaim.add_argument("--processed-root", required=True, help="Managed Stage-1 product root")
    p_reclaim.add_argument(
        "--manifest-root", required=True, help="Directory for reclaim intent/final manifests"
    )
    p_reclaim.add_argument(
        "--report-out", default=None, help="Machine-readable PLAN report path"
    )
    p_reclaim.add_argument("--execute", action="store_true", help="Delete after all safety checks pass")
    p_reclaim.add_argument("--format", choices=("table", "json"), default="table")
    p_reclaim.set_defaults(func=cmd_reclaim_stage1_products)

    p_legacy_cleanup = sub.add_parser(
        "legacy-prune-stage1-products",
        help="Plan or execute explicit cleanup of audited legacy Stage-1 FITS; never uses strict reclaim eligibility",
    )
    p_legacy_cleanup.add_argument("--processed-root", required=True, help="Exact legacy Stage-1 product root")
    p_legacy_cleanup.add_argument("--manifest-root", required=True, help="Directory for frozen cleanup evidence")
    p_legacy_cleanup.add_argument("--field-id", type=int, default=None, help="Expected legacy field id")
    p_legacy_cleanup.add_argument("--object-name", default="GPX_TF17_2", help="Expected legacy GPX object")
    p_legacy_cleanup.add_argument("--execute", action="store_true", help="Delete only the frozen DB-linked allowlist")
    p_legacy_cleanup.add_argument("--format", choices=("table", "json"), default="table")
    p_legacy_cleanup.set_defaults(func=cmd_legacy_prune_stage1_products)

    p_schema_status = sub.add_parser(
        "schema-status", help="Show schema migration ledger and checksum status"
    )
    p_schema_status.add_argument(
        "--guard-p207-test-readonly-ledger", action="store_true",
        help="Opt in to the fixed TEST-only guarded read-only ledger check",
    )
    p_schema_status.add_argument(
        "--guard-p207-test-readonly-probe", action="store_true",
        help="Opt in to the fixed TEST-only backend probe without querying the ledger",
    )
    p_schema_status.add_argument(
        _P207_GUARDED_INTEGRITY_FLAG, action="store_true",
        help="Opt in to the fixed TEST-only bounded read-only integrity preflight",
    )
    p_schema_status.set_defaults(func=cmd_schema_status)

    p_config_example = sub.add_parser(
        "config-example",
        help="Write the packaged self-service Grandgousier YAML configuration template",
    )
    p_config_example.add_argument("--out", default="grandgousier.yaml", help="Destination YAML path")
    p_config_example.add_argument("--force", action="store_true", help="Overwrite an existing destination")
    p_config_example.set_defaults(func=cmd_config_example)

    p_doctor = sub.add_parser(
        "doctor",
        help="Run a non-destructive installation, package, PostgreSQL, schema and path preflight",
    )
    p_doctor.add_argument(
        "--config", dest="config_file", default=argparse.SUPPRESS,
        help="Grandgousier YAML/JSON application config (may also be specified before the subcommand)",
    )
    p_doctor.add_argument("--processed-root", default=None, help="Optional processed FITS root to validate")
    p_doctor.add_argument("--out-root", default=None, help="Optional output root to validate for writability")
    p_doctor.add_argument("--instrument-profile", default=None, help="auto, MASTER or RoboPhot; defaults to config/auto")
    p_doctor.add_argument("--strict-optional", action="store_true", help="Treat missing optional photometry/Gaia dependencies as failure")
    p_doctor.add_argument("--format", choices=("table", "json"), default="table")
    p_doctor.set_defaults(func=cmd_doctor)

    p_diagnose_run = sub.add_parser(
        "diagnose-run",
        help="Inspect a field-archive run bundle and print normalized recovery guidance",
    )
    p_diagnose_run.add_argument(
        "path",
        help="Path to a _jobs/<job_id> directory or its job_summary.json",
    )
    p_diagnose_run.add_argument("--format", choices=("table", "json"), default="table")
    p_diagnose_run.set_defaults(func=cmd_diagnose_run)

    p_operational_status = sub.add_parser(
        "status",
        help="Show compact read-only field, Stage-2 provenance and latest Stage-3 operational status",
    )
    p_operational_status.add_argument(
        "--config", dest="config_file", default=argparse.SUPPRESS,
        help="Grandgousier YAML/JSON application config (may also be specified before the subcommand)",
    )
    p_operational_status.add_argument("--field-id", type=int, default=None, help="Field id; defaults to field_archive.field_id in config")
    p_operational_status.add_argument("--catalog-name", default=None, help="Catalog namespace; defaults to field_archive.catalog_name in config")
    p_operational_status.add_argument(
        "--required-stage2-recipe", choices=("stage3_scale_v1",), default=None,
        help="Stage-2 provenance contract; defaults to field_archive.stage2_recipe/current production recipe",
    )
    p_operational_status.add_argument("--min-points-per-object", type=int, default=5)
    p_operational_status.add_argument("--min-common-objects", type=int, default=20)
    p_operational_status.add_argument("--verbose", action="store_true", help="Also print per-series readiness rows")
    p_operational_status.add_argument("--format", choices=("table", "json"), default="table")
    p_operational_status.set_defaults(func=cmd_operational_status)

    p_acceptance = sub.add_parser(
        "acceptance-check",
        help="Run frozen read-only scientific acceptance checks against a validated dataset",
    )
    p_acceptance.add_argument(
        "--config", dest="config_file", default=argparse.SUPPRESS,
        help="Grandgousier YAML/JSON application config (may also be specified before the subcommand)",
    )
    p_acceptance.add_argument("--dataset", default="gpx_tf17_2", help="Packaged dataset name or acceptance JSON path")
    p_acceptance.add_argument("--out-root", default=None, help="Keep temporary M5 acceptance products under this directory")
    p_acceptance.add_argument("--report-out", default=None, help="Optional machine-readable JSON report")
    p_acceptance.add_argument("--format", choices=("table", "json"), default="table")
    p_acceptance.set_defaults(func=cmd_acceptance_check)

    p_migrate = sub.add_parser(
        "migrate-db", help="Apply pending managed database migrations"
    )
    p_migrate.add_argument(
        "--adopt-legacy",
        action="store_true",
        help="Validate and baseline the pre-0.5.39.1 migration history without re-running it",
    )
    p_migrate.add_argument(
        "--dry-run", action="store_true", help="Show the managed migrations that would be applied"
    )
    p_migrate.set_defaults(func=cmd_migrate_db)

    p_observation_identity = sub.add_parser(
        "backfill-observation-identity",
        help="Plan or execute path-independent physical-observation identity backfill",
    )
    p_observation_identity.add_argument(
        "--instrument",
        action="append",
        choices=("MASTER", "RoboPhot"),
        default=None,
        help="Instrument to include; repeatable; default MASTER and RoboPhot",
    )
    p_observation_identity.add_argument(
        "--execute",
        action="store_true",
        help="Create observations and link frames; default is read-only plan",
    )
    p_observation_identity.add_argument(
        "--out-root",
        default="reports/observation_identity_backfill",
        help="Directory for summary.json and CSV diagnostics",
    )
    p_observation_identity.add_argument(
        "--format", choices=("table", "json"), default="table", help="Console output format"
    )
    p_observation_identity.set_defaults(func=cmd_backfill_observation_identity)

    p_stage1_plan = sub.add_parser(
        "stage1-plan",
        help="Build a canonical metadata-only Stage-1 archive/product plan",
        description=(
            "Configure stage1_batch_size in archives.<source> (preferred) or at the top level; "
            "the allowed range is 1..512 and the default is 128."
        ),
    )
    p_stage1_plan.add_argument(
        "--config", dest="config_file", default=argparse.SUPPRESS,
        help="Grandgousier YAML/JSON config containing archives/products sections",
    )
    p_stage1_plan.add_argument("--source", required=True, help="Configured archives.<source-id>")
    p_stage1_plan.add_argument("--native-observation-id", action="append", default=[],
                               help="Exact ordered MASTER UNIQNUM; repeat for every selected observation")
    p_stage1_plan.add_argument(
        "--filter", dest="stage1_filters", action="append", default=[],
        help="Normalized filter; repeatable",
    )
    p_stage1_plan.add_argument(
        "--datetime-start", default=None, help="Optional inclusive observation-time lower bound"
    )
    p_stage1_plan.add_argument(
        "--datetime-end", default=None, help="Optional exclusive observation-time upper bound"
    )
    p_stage1_plan.add_argument(
        "--object", dest="stage1_object_name", default=None, help="Optional exact normalized object name"
    )
    p_stage1_plan.add_argument("--ra", type=float, default=None, help="Optional ICRS search center RA in degrees")
    p_stage1_plan.add_argument("--dec", type=float, default=None, help="Optional ICRS search center Dec in degrees")
    p_stage1_plan.add_argument(
        "--radius-deg", type=float, default=None, help="Optional angular radius in degrees; requires --ra/--dec"
    )
    p_stage1_plan.add_argument(
        "--limit", type=int, default=None, help="Optional maximum number of matching metadata records"
    )
    p_stage1_plan.add_argument(
        "--out", default="reports/stage1_plan.json", help="Canonical plan JSON output"
    )
    p_stage1_plan.add_argument("--csv-out", default=None, help="Optional CSV summary; default alongside JSON")
    p_stage1_plan.add_argument("--format", choices=("table", "json"), default="table")
    p_stage1_plan.set_defaults(func=cmd_stage1_plan)

    p_stage1_execute = sub.add_parser(
        "stage1-execute",
        help="Execute a canonical Stage-1 plan into ProductStore without Stage-2 DB writes",
    )
    p_stage1_execute.add_argument(
        "--config", dest="config_file", default=argparse.SUPPRESS,
        help="Grandgousier YAML/JSON config containing archives/products sections",
    )
    p_stage1_execute.add_argument("--source", required=True, help="Configured archives.<source-id>")
    p_stage1_execute.add_argument("--native-observation-id", action="append", default=[],
                                  help="Exact ordered MASTER UNIQNUM; repeat for every selected observation")
    p_stage1_execute.add_argument(
        "--filter", dest="stage1_filters", action="append", default=[],
        help="Normalized filter; repeatable",
    )
    p_stage1_execute.add_argument(
        "--datetime-start", default=None, help="Optional inclusive observation-time lower bound"
    )
    p_stage1_execute.add_argument(
        "--datetime-end", default=None, help="Optional exclusive observation-time upper bound"
    )
    p_stage1_execute.add_argument(
        "--object", dest="stage1_object_name", default=None, help="Optional exact normalized object name"
    )
    p_stage1_execute.add_argument("--ra", type=float, default=None, help="Optional ICRS search center RA in degrees")
    p_stage1_execute.add_argument("--dec", type=float, default=None, help="Optional ICRS search center Dec in degrees")
    p_stage1_execute.add_argument(
        "--radius-deg", type=float, default=None, help="Optional angular radius in degrees; requires --ra/--dec"
    )
    p_stage1_execute.add_argument(
        "--limit", type=int, default=None, help="Optional maximum number of matching metadata records"
    )
    p_stage1_execute.add_argument(
        "--plan-out", default="reports/stage1_execute_plan.json",
        help="Exact canonical plan JSON executed by this command",
    )
    p_stage1_execute.add_argument(
        "--report-out", default="reports/stage1_execution.json",
        help="Execution report JSON output",
    )
    p_stage1_execute.add_argument(
        "--csv-out", default=None, help="Optional execution CSV; default alongside report JSON"
    )
    p_stage1_execute.add_argument(
        "--overwrite", action="store_true",
        help="Replace an existing ProductStore product after current inputs have been materialized and fingerprinted",
    )
    p_stage1_execute.add_argument(
        "--max-dark-temperature-delta-c", type=float, default=5.0,
        help="MASTER only: maximum allowed |science-dark| CCD temperature difference in C; default 5",
    )
    p_stage1_execute.add_argument(
        "--batch-size", dest="stage1_batch_size", type=int, default=None,
        help=(
            "Stage-1 batch size 1..512; overrides archives.<source>.stage1_batch_size and "
            "top-level stage1_batch_size (default 128)"
        ),
    )
    p_stage1_execute.add_argument(
        "--stop-on-first-failure", action="store_true",
        help="Stop after the first failed or blocked item; finalize partial plan/report and allow --resume",
    )
    p_stage1_execute.add_argument(
        "--checkpoint", default=None,
        help="Durable Stage-1 checkpoint path; default is next to --report-out",
    )
    resume_group = p_stage1_execute.add_mutually_exclusive_group()
    resume_group.add_argument("--resume", action="store_true", help="Resume the matching incomplete Stage-1 run")
    resume_group.add_argument(
        "--restart-run", action="store_true",
        help="Start a new operational run without deleting existing products",
    )
    p_stage1_execute.add_argument("--format", choices=("table", "json"), default="table")
    p_stage1_execute.set_defaults(func=cmd_stage1_execute)

    p_stage1_handoff = sub.add_parser(
        "stage1-handoff",
        help="Preflight or persist exact products from one Stage-1 execution report",
    )
    p_stage1_handoff.add_argument(
        "--config", dest="config_file", default=argparse.SUPPRESS,
        help="Grandgousier YAML/JSON application config; field_archive values provide safe Stage-2 defaults",
    )
    p_stage1_handoff.add_argument(
        "--execution-report", required=True,
        help="Stage-1 execution JSON whose exact ProductStore FITS products are eligible for handoff",
    )
    handoff_mode = p_stage1_handoff.add_mutually_exclusive_group()
    handoff_mode.add_argument(
        "--execute", action="store_true",
        help="Index/select exact products and run existing Stage 2; default is read-only P4 preflight",
    )
    handoff_mode.add_argument(
        "--persist-only", action="store_true",
        help="Persist selected P4 identities and Stage-1 evidence only; do not invoke Stage 2",
    )
    p_stage1_handoff.add_argument(
        "--catalog-name", default=None,
        help="Existing catalog namespace; defaults to field_archive.catalog_name, otherwise Stage 2 keeps auto-catalog behavior",
    )
    p_stage1_handoff.add_argument(
        "--instrument-profile", default=None, choices=("auto", "MASTER", "RoboPhot"),
        help="FITS profile selection; defaults to field_archive.instrument_profile or auto",
    )
    p_stage1_handoff.add_argument("--master-profile", default=None, help="Override MASTER YAML profile path")
    p_stage1_handoff.add_argument("--robophot-profile", default=None, help="Override RoboPhot YAML profile path")
    p_stage1_handoff.add_argument("--min-frames-per-series", type=int, default=10)
    p_stage1_handoff.add_argument(
        "--ensemble-min-ref-stars-per-frame", type=int, default=30,
        help="Stage-2 ensemble reference threshold; specify 20 for the canonical GPX stage3_scale_v1 run",
    )
    p_stage1_handoff.add_argument("--max-field-separation-arcmin", type=float, default=30.0)
    p_stage1_handoff.add_argument("--max-series-gap-minutes", type=float, default=60.0)
    p_stage1_handoff.add_argument(
        "--out-root", default=None,
        help="Stage-2 output root; defaults to field_archive/paths out_root or exports",
    )
    p_stage1_handoff.add_argument("--log-dir", default="logs", help="Stage-2 pipeline log root")
    p_stage1_handoff.add_argument(
        "--stage2-manifest-out", default=None,
        help="Optional nested process-archive-path manifest for selected products",
    )
    p_stage1_handoff.add_argument(
        "--stage2-profile",
        choices=("bulk", "diagnostic"),
        default="bulk",
        help="Nested Stage-2 optional-work profile (default: bulk)",
    )
    p_stage1_handoff.add_argument(
        "--report-out", default="reports/stage1_handoff.json",
        help="Stage-1 handoff result report JSON",
    )
    p_stage1_handoff.add_argument("--format", choices=("table", "json"), default="table")
    p_stage1_handoff.set_defaults(func=cmd_stage1_handoff)

    p_index = sub.add_parser("index-archive", help="Index FITS archive into frames table")
    p_index.add_argument("--root", required=True, help="Root directory with raw FITS files")
    p_index.add_argument("--instrument", required=True, help="Instrument YAML profile")
    p_index.add_argument("--pattern", default="*.fits", help="Glob pattern, default: *.fits")
    p_index.add_argument("--recursive", action="store_true", help="Search recursively")
    p_index.add_argument("--dry-run", action="store_true", help="Do not write to DB")
    p_index.add_argument("--limit", type=int, default=None, help="Limit number of discovered files")
    p_index.set_defaults(func=cmd_index_archive)

    p_master_reduce = sub.add_parser(
        "master-archive-reduce",
        help="Build calibrated MASTER FITS as (raw-SDARK)/SFLAT using downloaded Stage 1A files",
    )
    p_master_reduce.add_argument("--manifest", required=True, help="Prepared local reduction manifest CSV")
    p_master_reduce.add_argument(
        "--cache-root", default="archive_cache", help="Local prepared-data cache root; default ./archive_cache"
    )
    p_master_reduce.add_argument(
        "--out-root", default=None, help="Processed FITS root; default <cache-root>/processed"
    )
    p_master_reduce.add_argument("--limit", type=int, default=None, help="Optional number of unique science frames for smoke tests")
    p_master_reduce.add_argument("--execute", action="store_true", help="Actually write processed FITS; default is plan-only")
    p_master_reduce.add_argument("--overwrite", action="store_true", help="Replace existing processed FITS")
    p_master_reduce.add_argument(
        "--compact-existing",
        action="store_true",
        help="Rewrite validated legacy processed FITS with uint8 DQ without recalibrating science pixels",
    )
    p_master_reduce.add_argument(
        "--delete-raw-after-success",
        action="store_true",
        help="Delete each science raw after a validated processed FITS exists; header/SDARK/SFLAT are retained",
    )
    p_master_reduce.add_argument(
        "--max-dark-temperature-delta-c",
        type=float,
        default=5.0,
        help="Maximum allowed |science-dark| CCD temperature difference in C; default 5",
    )
    p_master_reduce.add_argument(
        "--report-out", default=None, help="Reduction report CSV; default <out-root>/master_reduction_report.csv"
    )
    p_master_reduce.add_argument(
        "--format", choices=("table", "json"), default="table", help="Console plan/result format"
    )
    p_master_reduce.set_defaults(func=cmd_master_archive_reduce)

    p_master_physical_probe = sub.add_parser(
        "probe-master-physical-archive",
        help="Build a bounded, metadata-only, plan-only inventory of a local MASTER imdata tree",
    )
    p_master_physical_probe.add_argument(
        "--imdata-root", required=True,
        help="Local directory containing MASTER rawdata/ and procdata/ trees",
    )
    p_master_physical_probe.add_argument(
        "--max-files", type=int, default=100,
        help="Maximum number of raw FITS files whose headers may be inspected (default: 100; max: 10000)",
    )
    p_master_physical_probe.set_defaults(func=cmd_probe_master_physical_archive)

    p_master_prune = sub.add_parser(
        "master-archive-prune",
        help="Safely remove local MASTER raw or processed FITS after downstream products are verified",
    )
    p_master_prune.add_argument("--manifest", required=True, help="Prepared local reduction manifest CSV")
    p_master_prune.add_argument(
        "--cache-root", default="archive_cache", help="Local prepared-data cache root; default ./archive_cache"
    )
    p_master_prune.add_argument(
        "--out-root", default=None, help="Processed FITS root; default <cache-root>/processed"
    )
    p_master_prune.add_argument(
        "--scope", choices=("raw", "processed", "all"), required=True,
        help="raw: require valid Stage-1B output; processed: require PHOTOMETRY_DONE + compact science cache or legacy science_measurements; all: both",
    )
    p_master_prune.add_argument("--limit", type=int, default=None, help="Optional number of unique science frames")
    p_master_prune.add_argument("--execute", action="store_true", help="Actually delete eligible files; default plan-only")
    p_master_prune.add_argument("--yes", action="store_true", help="Required together with --execute")
    p_master_prune.add_argument(
        "--report-out", default=None, help="Prune report CSV; default <cache-root>/master_prune_report.csv"
    )
    p_master_prune.add_argument("--format", choices=("table", "json"), default="table")
    p_master_prune.set_defaults(func=cmd_master_archive_prune)

    # Repository-local archive transport plugin. It is intentionally excluded from
    # wheel/sdist builds; public installations therefore do not expose private
    # archive discovery/fetch commands or site topology.
    try:
        from internal_tools.master_cli import register_master_remote_commands
    except ModuleNotFoundError as exc:
        if exc.name not in {"internal_tools", "internal_tools.master_cli"}:
            raise
        register_master_remote_commands = None
    if register_master_remote_commands is not None:
        register_master_remote_commands(sub)


    p_wcs = sub.add_parser("check-wcs", help="Validate header WCS for indexed FITS frames")
    p_wcs.add_argument("--frame-id", type=int, action="append", help="Specific frame_id; may be repeated")
    p_wcs.add_argument("--instrument", default=None, help="Filter by instrument name, e.g. MASTER or RoboPhot")
    p_wcs.add_argument("--instrument-unit-id", type=int, default=None, help="Filter by instrument_units.instrument_unit_id")
    p_wcs.add_argument(
        "--status",
        default="NOT_CHECKED",
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
    p_build_series.add_argument(
        "--min-frames-per-series",
        type=int,
        default=1,
        help="Minimum Frames for READY status; one-shot groups below this are dropped",
    )
    p_build_series.add_argument(
        "--no-finalize-tail",
        dest="finalize_tail",
        action="store_false",
        default=True,
        help="Keep the final group OPEN for incremental additions",
    )
    p_build_series.add_argument("--include-assigned", dest="only_unassigned", action="store_false", help="Reassign frames already present in field_series_frames")
    p_build_series.set_defaults(only_unassigned=True)
    p_build_series.add_argument("--include-non-science", dest="only_science", action="store_false", help="Include non-science frames")
    p_build_series.set_defaults(only_science=True)
    p_build_series.add_argument("--dry-run", action="store_true", help="Show proposed fields/series without writing")
    p_build_series.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_build_series.set_defaults(func=cmd_build_field_series)

    p_finalize_series = sub.add_parser(
        "finalize-field-series",
        help="Finalize one eligible OPEN series; does not run Stage 2",
    )
    p_finalize_series.add_argument("--series-id", type=int, required=True)
    p_finalize_series.set_defaults(func=cmd_finalize_field_series)

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
    p_apply_color.add_argument("--clip-sigma", type=float, default=4.0, help="Robust clipping threshold for object-level color transform")
    p_apply_color.add_argument("--n-iterations", type=int, default=5, help="Maximum object-level color-transform iterations")
    p_apply_color.add_argument("--min-calibrator-objects", type=int, default=20, help="Minimum calibrator objects required to write the target calibration")
    p_apply_color.add_argument("--min-color-span", type=float, default=0.3, help="Minimum calibrator color span")
    p_apply_color.add_argument("--max-transform-residual-mad", type=float, default=0.10, help="Maximum transform residual MAD; use negative to disable")
    p_apply_color.add_argument("--max-pivot-color-fraction", type=float, default=0.25, help="Maximum pivot-color measurement fraction; use negative to disable")
    p_apply_color.add_argument("--max-shape-metric-delta", type=float, default=1e-10, help="Maximum allowed RMS/MAD/robust-amplitude change")
    p_apply_color.add_argument("--min-color-term-significance", type=float, default=2.0, help="Minimum |C|/sigma(C) required to select the color model")
    p_apply_color.add_argument("--min-color-model-bic-improvement", type=float, default=6.0, help="Minimum BIC improvement required to select the color model")
    p_apply_color.add_argument("--min-color-model-mad-improvement", type=float, default=0.0, help="Minimum residual-MAD improvement required to select the color model")
    p_apply_color.add_argument("--include-saturated", action="store_true", help="Do not reject saturated_or_near_saturated measurement flags")
    p_apply_color.add_argument("--limit", type=int, default=None, help="Limit loaded measurements for debugging")
    p_apply_color.add_argument("--dry-run", action="store_true", help="Fit model and count rows without writing target calibration")
    p_apply_color.add_argument("--diagnose-performance", action="store_true", help="Collect detailed color-calibration phase timing diagnostics")
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
    p_phot.add_argument("--aperture-radius-pix", type=float, default=3.0, help="Circular aperture radius in pixels; floor/equal-area baseline for adaptive_ellipse")
    p_phot.add_argument("--photometry-estimator", choices=("circular", "adaptive_ellipse", "spatial_psf", "empirical_psf"), default="adaptive_ellipse", help="Base flux estimator")
    p_phot.add_argument("--adaptive-min-psf-snr", type=float, default=20.0, help="Minimum detection S/N for adaptive-aperture PSF references")
    p_phot.add_argument("--adaptive-max-psf-sources-per-frame", type=int, default=500, help="Maximum PSF references used per frame")
    p_phot.add_argument("--adaptive-psf-cutout-radius-pix", type=int, default=7, help="PSF second-moment cutout radius used by adaptive photometry")
    p_phot.add_argument("--adaptive-psf-min-isolation-pix", type=float, default=12.0, help="Minimum PSF-reference isolation distance")
    p_phot.add_argument("--adaptive-interpolation-neighbors", type=int, default=24, help="Nearest PSF references used for local covariance interpolation")
    p_phot.add_argument("--adaptive-min-psf-references", type=int, default=20, help="Minimum PSF references required per frame")
    p_phot.add_argument("--adaptive-aperture-fwhm-factor", type=float, default=1.0, help="Equal-area adaptive radius in local geometric FWHM units, with --aperture-radius-pix as floor")
    p_phot.add_argument("--adaptive-max-axis-ratio", type=float, default=3.0, help="Safety clamp for adaptive ellipse axis ratio")
    p_phot.add_argument("--adaptive-max-radius-scale", type=float, default=2.5, help="Safety clamp for adaptive equivalent radius relative to --aperture-radius-pix")
    p_phot.add_argument("--no-adaptive-circular-fallback", dest="adaptive_fallback_to_circular", action="store_false", help="Fail instead of falling back to circular photometry when local PSF geometry is unavailable")
    p_phot.set_defaults(adaptive_fallback_to_circular=True)
    p_phot.add_argument("--psf-profile", choices=("moffat", "gaussian"), default="moffat", help="Spatial-PSF profile family")
    p_phot.add_argument("--psf-moffat-beta", type=float, default=2.5, help="Fixed Moffat beta for spatial_psf")
    p_phot.add_argument("--psf-fit-radius-fwhm", type=float, default=2.5, help="PSF fit radius in local major-axis FWHM units")
    p_phot.add_argument("--psf-min-fit-radius-pix", type=float, default=5.0, help="Minimum spatial-PSF fit radius")
    p_phot.add_argument("--psf-max-fit-radius-pix", type=float, default=14.0, help="Maximum spatial-PSF fit radius")
    p_phot.add_argument("--psf-centroid-search-radius-pix", type=float, default=0.75, help="Half-width of forced-position centroid grid search")
    p_phot.add_argument("--psf-centroid-grid-steps", type=int, default=3, help="Centroid grid steps per axis for spatial_psf")
    p_phot.add_argument("--psf-residual-clip-sigma", type=float, default=5.0, help="Residual clipping threshold after centroid selection")
    p_phot.add_argument("--psf-residual-clip-iterations", type=int, default=2, help="Residual clipping iterations for spatial_psf")
    p_phot.add_argument("--no-psf-adaptive-fallback", dest="psf_fallback_to_adaptive", action="store_false", help="Do not fall back from spatial_psf to adaptive ellipse when profile fitting cannot be used")
    p_phot.set_defaults(psf_fallback_to_adaptive=True)
    p_phot.add_argument("--empirical-psf-grid-size", type=int, default=3, help="Spatial empirical PSF grid size per axis")
    p_phot.add_argument("--empirical-psf-stamp-radius-pix", type=int, default=8, help="Half-size of registered empirical PSF stamps")
    p_phot.add_argument("--empirical-psf-reference-radius-pix", type=float, default=5.0, help="Reference-aperture radius used to normalize empirical PSF templates")
    p_phot.add_argument("--empirical-psf-references-per-node", type=int, default=48, help="Nearest PSF stars combined into each grid-node template")
    p_phot.add_argument("--empirical-psf-min-references-per-node", type=int, default=12, help="Minimum usable reference stars required per empirical PSF node")
    p_phot.add_argument("--no-empirical-psf-adaptive-fallback", dest="empirical_psf_fallback_to_adaptive", action="store_false", help="Do not fall back from empirical_psf to adaptive ellipse when empirical fitting cannot be used")
    p_phot.set_defaults(empirical_psf_fallback_to_adaptive=True)
    p_phot.add_argument("--annulus-inner-pix", type=float, default=8.0, help="Background annulus inner radius in pixels")
    p_phot.add_argument("--annulus-outer-pix", type=float, default=12.0, help="Background annulus outer radius in pixels")
    p_phot.add_argument("--zero-point", type=float, default=0.0, help="Instrumental magnitude additive zero point")
    p_phot.add_argument("--saturation-level-adu", type=float, default=None, help="Override saturation threshold")
    p_phot.add_argument(
        "--random-error-model",
        choices=("physical_aperture_v1", "legacy_aperture_v1"),
        default="physical_aperture_v1",
        help="Random per-epoch aperture-photometry error model",
    )
    p_phot.add_argument("--gain-e-per-adu", type=float, default=None, help="Override detector gain; otherwise use frame/instrument metadata")
    p_phot.add_argument("--read-noise-e", type=float, default=None, help="Override detector read noise; otherwise use frame/instrument metadata")
    p_phot.add_argument("--background-estimator-variance-factor", type=float, default=1.5707963267948966, help="Variance multiplier for the annulus-median background estimator")
    p_phot.add_argument("--no-empirical-background-floor", dest="empirical_background_floor", action="store_false", help="Use only detector-predicted sky/read variance instead of flooring it by measured annulus scatter")
    p_phot.set_defaults(empirical_background_floor=True)
    p_phot.add_argument("--fractional-flux-floor", type=float, default=0.0, help="Optional flat/intrapixel fractional flux-noise floor")
    p_phot.add_argument("--enable-scintillation", dest="scintillation_enabled", action="store_true", help="Enable Young-type scintillation variance when telescope/site metadata are available")
    p_phot.add_argument("--disable-scintillation", dest="scintillation_enabled", action="store_false", help="Disable the scintillation variance term")
    p_phot.set_defaults(scintillation_enabled=True)
    p_phot.add_argument("--scintillation-coefficient", type=float, default=0.09, help="Coefficient in the Young-type fractional scintillation approximation")
    p_phot.add_argument("--telescope-aperture-m", type=float, default=None, help="Telescope clear-aperture diameter in metres for scintillation")
    p_phot.add_argument("--site-altitude-m", type=float, default=None, help="Observatory altitude in metres for scintillation")
    p_phot.add_argument("--diagnostics-out-dir", default=None, help="Optional directory for random-error summary JSON and per-frame CSV")
    p_phot.add_argument("--diagnose-performance", action="store_true", help="Collect detailed forced-photometry phase timing and throughput diagnostics")
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

    p_evidence = sub.add_parser(
        "candidate-evidence-report",
        help="Combine exported candidate products across series and measure repeatability/coherence",
    )
    p_evidence.add_argument(
        "--series-dir",
        action="append",
        required=True,
        help="Series export directory (or its candidates directory); repeat for each series",
    )
    p_evidence.add_argument("--out-prefix", required=True, help="Output prefix for JSON and CSV products")
    p_evidence.add_argument("--max-time-separation-sec", type=float, default=120.0, help="Maximum separation for paired simultaneous epochs")
    p_evidence.add_argument("--min-pairs", type=int, default=10, help="Minimum paired epochs for coherence evidence")
    p_evidence.add_argument("--min-correlation", type=float, default=0.25, help="Minimum Pearson and Spearman correlation for coherence evidence")
    p_evidence.add_argument("--max-fdr-q", type=float, default=0.05, help="Maximum Benjamini-Hochberg q for significant correlation diagnostics")
    p_evidence.add_argument("--min-repeat-series", type=int, default=2, help="Minimum accepted series for repeatability evidence")
    p_evidence.add_argument(
        "--target-source-id",
        action="append",
        default=[],
        help="Explicit target source ID; repeat for multiple targets",
    )
    p_evidence.add_argument("--limit", type=int, default=50, help="Maximum displayed candidate rows")
    p_evidence.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Console output format")
    p_evidence.set_defaults(func=cmd_candidate_evidence_report)

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
    p_field_pipeline.add_argument("--photometry-estimator", choices=("circular", "adaptive_ellipse", "spatial_psf", "empirical_psf"), default="adaptive_ellipse", help="Base science flux estimator; adaptive_ellipse uses a local PSF-aligned elliptical aperture and keeps circular as fallback")
    p_field_pipeline.add_argument("--adaptive-min-psf-snr", type=float, default=20.0, help="Minimum detection S/N for adaptive-aperture PSF references")
    p_field_pipeline.add_argument("--adaptive-max-psf-sources-per-frame", type=int, default=500, help="Maximum PSF reference detections used per frame by adaptive photometry")
    p_field_pipeline.add_argument("--adaptive-psf-cutout-radius-pix", type=int, default=7, help="PSF second-moment cutout radius used by adaptive photometry")
    p_field_pipeline.add_argument("--adaptive-psf-min-isolation-pix", type=float, default=12.0, help="Minimum isolation of PSF reference stars for adaptive photometry")
    p_field_pipeline.add_argument("--adaptive-interpolation-neighbors", type=int, default=24, help="Nearest PSF references interpolated for each adaptive aperture")
    p_field_pipeline.add_argument("--adaptive-min-psf-references", type=int, default=20, help="Minimum PSF references required per frame before adaptive photometry is trusted")
    p_field_pipeline.add_argument("--adaptive-aperture-fwhm-factor", type=float, default=1.0, help="Equal-area adaptive aperture radius in units of local geometric FWHM, with --aperture-radius-pix as a floor")
    p_field_pipeline.add_argument("--adaptive-max-axis-ratio", type=float, default=3.0, help="Safety cap for adaptive aperture semi-major/semi-minor ratio")
    p_field_pipeline.add_argument("--adaptive-max-radius-scale", type=float, default=2.5, help="Safety cap for adaptive equal-area radius relative to --aperture-radius-pix")
    p_field_pipeline.add_argument("--no-adaptive-circular-fallback", dest="adaptive_fallback_to_circular", action="store_false", help="Fail adaptive frames/measurements instead of falling back to circular aperture when a PSF model is unavailable")
    p_field_pipeline.set_defaults(adaptive_fallback_to_circular=True)
    p_field_pipeline.add_argument("--psf-profile", choices=("moffat", "gaussian"), default="moffat", help="Spatial-PSF profile family")
    p_field_pipeline.add_argument("--psf-moffat-beta", type=float, default=2.5, help="Fixed Moffat beta for spatial_psf")
    p_field_pipeline.add_argument("--psf-fit-radius-fwhm", type=float, default=2.5, help="PSF fit radius in local major-axis FWHM units")
    p_field_pipeline.add_argument("--psf-min-fit-radius-pix", type=float, default=5.0, help="Minimum spatial-PSF fit radius")
    p_field_pipeline.add_argument("--psf-max-fit-radius-pix", type=float, default=14.0, help="Maximum spatial-PSF fit radius")
    p_field_pipeline.add_argument("--psf-centroid-search-radius-pix", type=float, default=0.75, help="Half-width of forced-position centroid grid search")
    p_field_pipeline.add_argument("--psf-centroid-grid-steps", type=int, default=3, help="Centroid grid steps per axis for spatial_psf")
    p_field_pipeline.add_argument("--psf-residual-clip-sigma", type=float, default=5.0, help="Residual clipping threshold after centroid selection")
    p_field_pipeline.add_argument("--psf-residual-clip-iterations", type=int, default=2, help="Residual clipping iterations for spatial_psf")
    p_field_pipeline.add_argument("--no-psf-adaptive-fallback", dest="psf_fallback_to_adaptive", action="store_false", help="Do not fall back from spatial_psf to adaptive ellipse when profile fitting cannot be used")
    p_field_pipeline.set_defaults(psf_fallback_to_adaptive=True)
    p_field_pipeline.add_argument("--empirical-psf-grid-size", type=int, default=3, help="Spatial empirical PSF grid size per axis")
    p_field_pipeline.add_argument("--empirical-psf-stamp-radius-pix", type=int, default=8, help="Half-size of registered empirical PSF stamps")
    p_field_pipeline.add_argument("--empirical-psf-reference-radius-pix", type=float, default=5.0, help="Reference-aperture radius used to normalize empirical PSF templates")
    p_field_pipeline.add_argument("--empirical-psf-references-per-node", type=int, default=48, help="Nearest PSF stars combined into each grid-node template")
    p_field_pipeline.add_argument("--empirical-psf-min-references-per-node", type=int, default=12, help="Minimum usable reference stars required per empirical PSF node")
    p_field_pipeline.add_argument("--no-empirical-psf-adaptive-fallback", dest="empirical_psf_fallback_to_adaptive", action="store_false", help="Do not fall back from empirical_psf to adaptive ellipse when empirical fitting cannot be used")
    p_field_pipeline.set_defaults(empirical_psf_fallback_to_adaptive=True)
    p_field_pipeline.add_argument("--annulus-inner-pix", type=float, default=8.0, help="Background annulus inner radius")
    p_field_pipeline.add_argument("--annulus-outer-pix", type=float, default=12.0, help="Background annulus outer radius")
    p_field_pipeline.add_argument("--aperture-correction-reference-radius-pix", type=float, default=5.0, help="Large-aperture radius used to derive the spatial aperture correction")
    p_field_pipeline.add_argument("--aperture-correction-max-calibrator-mag-err", type=float, default=0.05, help="Maximum random mag_err for aperture-correction calibrators; use negative to disable")
    p_field_pipeline.add_argument("--aperture-correction-min-calibrators-per-frame", type=int, default=20, help="Minimum usable stars required for a frame aperture model")
    p_field_pipeline.add_argument("--aperture-correction-min-plane-calibrators", type=int, default=30, help="Minimum usable stars required to consider a spatial plane")
    p_field_pipeline.add_argument("--aperture-correction-clip-sigma", type=float, default=3.5, help="Robust clipping threshold for small-to-large aperture differences")
    p_field_pipeline.add_argument("--aperture-correction-n-iterations", type=int, default=3, help="Aperture-correction clipping iterations")
    p_field_pipeline.add_argument("--aperture-correction-min-plane-bic-improvement", type=float, default=6.0, help="Minimum BIC improvement required to select a spatial plane")
    p_field_pipeline.add_argument("--aperture-correction-min-spatial-peak-to-peak", type=float, default=0.005, help="Minimum predicted detector-wide spatial correction amplitude in mag")
    p_field_pipeline.add_argument("--aperture-correction-plane-policy", choices=["evidence", "safety_only"], default="evidence", help="Plane selection policy: evidence uses BIC/spatial/MAD preference; safety_only is an experimental diagnostic mode that selects any available plane passing safety gates")
    p_field_pipeline.add_argument("--aperture-correction-max-residual-mad", type=float, default=0.05, help="Maximum per-frame residual MAD; use negative to disable")
    p_field_pipeline.add_argument("--aperture-correction-max-abs-correction", type=float, default=0.5, help="Maximum absolute calibrator/constant correction and legacy combined plane-edge threshold in mag")
    p_field_pipeline.add_argument("--aperture-correction-max-abs-spatial-deviation", type=float, default=0.15, help="Maximum absolute plane deviation from the calibrator pivot across detector corners in mag")
    p_field_pipeline.add_argument("--aperture-correction-max-abs-total-correction", type=float, default=0.75, help="Emergency maximum absolute total plane correction at detector corners in mag")
    p_field_pipeline.add_argument("--aperture-correction-min-good-frame-fraction", type=float, default=0.8, help="Minimum fraction of frames with accepted aperture models")
    p_field_pipeline.add_argument("--aperture-correction-max-global-median-residual-mad", type=float, default=0.05, help="Maximum median frame residual MAD; use negative to disable")
    p_field_pipeline.add_argument("--raw-calibration-name", default=None, help="Override generated raw aperture calibration name")
    p_field_pipeline.add_argument("--aperture-zp-calibration-name", default=None, help="Override generated aperture-ZP calibration name")
    p_field_pipeline.add_argument("--ensemble-calibration-name", default=None, help="Override generated ensemble calibration name")
    p_field_pipeline.add_argument("--detection-threshold-sigma", type=float, default=5.0, help="Source detection threshold")
    p_field_pipeline.add_argument("--detection-max-sources-per-frame", type=int, default=2000, help="Maximum detections per frame")
    p_field_pipeline.add_argument("--detection-min-separation-pix", type=float, default=5.0, help="Minimum source-detection separation")
    p_field_pipeline.add_argument("--detection-match-radius-arcsec", type=float, default=2.0, help="Detection-to-catalog matching radius")
    p_field_pipeline.add_argument("--enable-psf-field-diagnostics", action="store_true", help="Measure diagnostic PSF morphology and spatial field models without changing science flux")
    p_field_pipeline.add_argument("--psf-diagnostics-max-sources-per-frame", type=int, default=500, help="Maximum isolated high-S/N detections used for PSF diagnostics per frame")
    p_field_pipeline.add_argument("--psf-diagnostics-min-snr", type=float, default=20.0, help="Minimum detection S/N for PSF diagnostics")
    p_field_pipeline.add_argument("--psf-diagnostics-cutout-radius-pix", type=int, default=7, help="Radius of the PSF second-moment cutout in pixels")
    p_field_pipeline.add_argument("--psf-diagnostics-min-isolation-pix", type=float, default=12.0, help="Minimum distance to any other detection for PSF diagnostic stars")
    p_field_pipeline.add_argument("--psf-diagnostics-clip-sigma", type=float, default=4.0, help="Robust clipping threshold for spatial PSF models")
    p_field_pipeline.add_argument("--psf-diagnostics-n-iterations", type=int, default=3, help="Robust spatial-model clipping iterations")
    p_field_pipeline.add_argument("--no-psf-diagnostic-plots", dest="psf_diagnostics_render_plots", action="store_false", help="Write CSV/JSON PSF diagnostics without PNG plots")
    p_field_pipeline.set_defaults(psf_diagnostics_render_plots=True)
    p_field_pipeline.add_argument("--bulk-insert-batch-size", type=int, default=1000, help="Rows per batched DB INSERT/UPSERT; capped automatically by PostgreSQL bind limit")
    p_field_pipeline.add_argument("--mag-key", default="G", help="Reference magnitude key in objects.ref_mags")
    p_field_pipeline.add_argument("--aperture-zp-min-ref-stars-per-frame", type=int, default=30, help="Aperture-ZP minimum reference stars per frame")
    p_field_pipeline.add_argument("--aperture-zp-max-mag-err", type=float, default=0.08, help="Aperture-ZP max mag_err; use negative to disable")
    p_field_pipeline.add_argument("--aperture-zp-clip-sigma", type=float, default=3.0, help="Aperture-ZP sigma clipping threshold")
    p_field_pipeline.add_argument("--aperture-zp-n-iterations", type=int, default=3, help="Aperture-ZP clipping iterations")
    p_field_pipeline.add_argument("--ensemble-min-measurements", type=int, default=10, help="Ensemble object-stat minimum measurements")
    p_field_pipeline.add_argument("--ensemble-min-ref-measurements", type=int, default=10, help="Ensemble reference candidate minimum measurements")
    p_field_pipeline.add_argument("--ensemble-min-ref-stars-per-frame", type=int, default=30, help="Minimum ensemble references per frame")
    p_field_pipeline.add_argument("--enable-spatial-ensemble", action="store_true", help="Apply zero-mean per-frame spatial residual calibration after each conventional ensemble pass")
    p_field_pipeline.add_argument("--enable-photometric-frame-qc", action="store_true", help="Enable conservative post-ensemble frame QC: relative/spatial anomalies become SUSPECT diagnostic flags without changing measurement errors; BAD is reserved for hard failures; accepted candidates are rechecked on GOOD epochs")
    p_field_pipeline.add_argument("--spatial-ensemble-max-order", type=int, choices=(1, 2), default=2, help="Maximum spatial polynomial order: 1=plane, 2=quadratic")
    p_field_pipeline.add_argument("--spatial-ensemble-min-ref-stars-per-frame", type=int, default=30, help="Minimum reference stars required to fit a spatial ensemble frame model")
    p_field_pipeline.add_argument("--spatial-ensemble-clip-sigma", type=float, default=3.5, help="Robust clipping threshold for spatial ensemble residual fits")
    p_field_pipeline.add_argument("--spatial-ensemble-n-iterations", type=int, default=3, help="Robust spatial ensemble fit iterations")
    p_field_pipeline.add_argument("--spatial-ensemble-min-bic-improvement", type=float, default=6.0, help="Minimum BIC improvement over identity required to apply a spatial model")
    p_field_pipeline.add_argument("--spatial-ensemble-min-spatial-peak-to-peak", type=float, default=0.003, help="Minimum detector-wide spatial amplitude in mag")
    p_field_pipeline.add_argument("--spatial-ensemble-max-residual-mad", type=float, default=0.05, help="Maximum reference residual MAD per frame; use negative to disable")
    p_field_pipeline.add_argument("--spatial-ensemble-max-residual-mad-degradation", type=float, default=0.0, help="Maximum allowed residual-MAD increase for a spatial model versus identity; 0 requires non-degradation, negative disables")
    p_field_pipeline.add_argument("--spatial-ensemble-max-abs-spatial-deviation", type=float, default=0.15, help="Maximum absolute zero-mean spatial correction across the detector")
    p_field_pipeline.add_argument("--spatial-ensemble-min-good-frame-fraction", type=float, default=0.8, help="Minimum valid-frame fraction; below this the stage becomes an identity copy")
    p_field_pipeline.add_argument("--ensemble-max-ref-rms", type=float, default=0.08, help="Maximum RMS for ensemble reference candidates; use negative to disable")
    p_field_pipeline.add_argument("--ensemble-max-ref-mad", type=float, default=0.05, help="Maximum MAD for ensemble reference candidates; use negative to disable")
    p_field_pipeline.add_argument("--ensemble-n-iterations", type=int, default=3, help="Ensemble iterations")
    p_field_pipeline.add_argument("--enable-color-calibration", action="store_true", help="Create a final reference-band color-calibrated science CalibrationVersion")
    p_field_pipeline.add_argument("--color-identity-catalog-name", default=None, help="Identity catalog for source IDs; defaults to --catalog-name")
    p_field_pipeline.add_argument("--color-reference-catalog-name", default=None, help="Imported reference-photometry catalog, e.g. panstarrs_dr2")
    p_field_pipeline.add_argument("--color-reference-band", default=None, help="Reference magnitude band, e.g. i")
    p_field_pipeline.add_argument("--color-index", default=None, help="Reference color index, e.g. r-i")
    p_field_pipeline.add_argument("--color-target-calibration-name", default=None, help="Override generated final color-calibration name")
    p_field_pipeline.add_argument("--color-min-measurements-per-object", type=int, default=5, help="Minimum measurements per color calibrator")
    p_field_pipeline.add_argument("--color-min-stars-per-frame", type=int, default=20, help="Minimum color calibrators per frame")
    p_field_pipeline.add_argument("--color-max-measurement-mag-err", type=float, default=None, help="Maximum instrumental mag_err for color calibrators")
    p_field_pipeline.add_argument("--color-max-reference-mag-err", type=float, default=None, help="Maximum reference-band error for color calibrators")
    p_field_pipeline.add_argument("--color-clip-sigma", type=float, default=4.0, help="Robust clipping threshold for object-level color transform")
    p_field_pipeline.add_argument("--color-n-iterations", type=int, default=5, help="Maximum object-level color-transform iterations")
    p_field_pipeline.add_argument("--color-min-calibrator-objects", type=int, default=20, help="Minimum calibrator objects required to publish the color transform")
    p_field_pipeline.add_argument("--color-min-span", type=float, default=0.3, help="Minimum calibrator color span required to publish the transform")
    p_field_pipeline.add_argument("--color-max-transform-residual-mad", type=float, default=0.10, help="Maximum object-level transform residual MAD; use negative to disable")
    p_field_pipeline.add_argument("--color-max-pivot-fraction", type=float, default=0.25, help="Maximum fraction of measurements using pivot color; use negative to disable")
    p_field_pipeline.add_argument("--color-max-shape-metric-delta", type=float, default=1e-10, help="Maximum allowed change in RMS/MAD/robust amplitude")
    p_field_pipeline.add_argument("--color-min-term-significance", type=float, default=2.0, help="Minimum |C|/sigma(C) required to select the color model")
    p_field_pipeline.add_argument("--color-min-bic-improvement", type=float, default=6.0, help="Minimum BIC improvement required to select the color model")
    p_field_pipeline.add_argument("--color-min-mad-improvement", type=float, default=0.0, help="Minimum residual-MAD improvement required to select the color model")
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
    p_field_pipeline.add_argument("--skip-aperture-correction", action="store_true", help="Skip small-to-large spatial aperture correction")
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
    p_field_pipeline.add_argument("--candidate-ensemble-growth-policy", choices=("initial_only", "all"), default="initial_only", help="Whether later ensemble iterations may add newly appearing candidates; initial_only prevents exclusion cascades")
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
    p_field_pipeline.add_argument("--diagnose-forced-photometry", action="store_true", help="Collect detailed forced-photometry phase timing and throughput diagnostics")
    p_field_pipeline.add_argument("--diagnose-calibration-performance", action="store_true", help="Collect detailed aperture-correction and color-calibration phase timing diagnostics")
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
    p_idem.add_argument("--detection-algorithm", default="numpy_localmax_v1", help="detection_runs.algorithm for source-detection runs")
    p_idem.add_argument("--detection-match-algorithm", default="nearest_sky_v1", help="detection_runs.algorithm for catalog-match runs")
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
    p_process_path.add_argument("--series-id", dest="series_ids", type=int, action="append", help="Execute only this existing field_series among frames under --path; may be repeated; selective mode does not create new series")
    p_process_path.add_argument("--native-observation-id", dest="native_observation_ids", action="append", help="Restrict detected MASTER FITS to this exact ordered native observation ID list; repeat once per ID")
    p_process_path.add_argument("--catalog-name", default=None, help="Catalog namespace already imported; omitted means auto_catalog_* with Gaia DR3 fetch")
    p_process_path.add_argument("--execute", action="store_true", help="Write to DB and run pipelines; omitted means plan-only")
    p_process_path.add_argument(
        "--stage2-profile",
        choices=("bulk", "diagnostic"),
        default="bulk",
        help="Stage-2 optional-work profile (default: bulk)",
    )
    p_process_path.add_argument("--plan-only", action="store_true", help="Alias for the default non-writing planning mode")
    p_process_path.add_argument("--instrument-profile", default="auto", choices=("auto", "MASTER", "RoboPhot"), help="Profile detection mode")
    p_process_path.add_argument("--master-profile", default=None, help="Override MASTER YAML profile path")
    p_process_path.add_argument("--robophot-profile", default=None, help="Override RoboPhot YAML profile path")
    p_process_path.add_argument("--extensions", default=".fits,.fit,.fts", help="Comma-separated FITS extensions")
    p_process_path.add_argument("--no-recursive", dest="recursive", action="store_false", help="Do not search subdirectories")
    p_process_path.set_defaults(recursive=True)
    p_process_path.add_argument("--limit", type=int, default=None, help="Limit discovered FITS files")
    p_process_path.add_argument("--min-frames-per-series", type=int, default=10, help="Minimum frames per generated field_series")
    p_process_path.add_argument(
        "--no-finalize-tail",
        dest="finalize_tail",
        action="store_false",
        default=True,
        help="Leave the trailing series OPEN for incremental additions",
    )
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
    p_process_path.add_argument("--photometry-estimator", choices=("circular", "adaptive_ellipse", "spatial_psf", "empirical_psf"), default="adaptive_ellipse", help="Base science flux estimator; adaptive_ellipse runs the complete Stage-2 chain from a local PSF-aligned elliptical aperture")
    p_process_path.add_argument("--adaptive-min-psf-snr", type=float, default=20.0, help="Minimum detection S/N for adaptive-aperture PSF references")
    p_process_path.add_argument("--adaptive-max-psf-sources-per-frame", type=int, default=500, help="Maximum PSF reference detections used per frame by adaptive photometry")
    p_process_path.add_argument("--adaptive-psf-cutout-radius-pix", type=int, default=7, help="PSF second-moment cutout radius used by adaptive photometry")
    p_process_path.add_argument("--adaptive-psf-min-isolation-pix", type=float, default=12.0, help="Minimum isolation of PSF reference stars for adaptive photometry")
    p_process_path.add_argument("--adaptive-interpolation-neighbors", type=int, default=24, help="Nearest PSF references interpolated for each adaptive aperture")
    p_process_path.add_argument("--adaptive-min-psf-references", type=int, default=20, help="Minimum PSF references required per frame before adaptive photometry is trusted")
    p_process_path.add_argument("--adaptive-aperture-fwhm-factor", type=float, default=1.0, help="Equal-area adaptive aperture radius in units of local geometric FWHM, with --aperture-radius-pix as a floor")
    p_process_path.add_argument("--adaptive-max-axis-ratio", type=float, default=3.0, help="Safety cap for adaptive aperture axis ratio")
    p_process_path.add_argument("--adaptive-max-radius-scale", type=float, default=2.5, help="Safety cap for adaptive equal-area radius relative to --aperture-radius-pix")
    p_process_path.add_argument("--no-adaptive-circular-fallback", dest="adaptive_fallback_to_circular", action="store_false", help="Do not substitute circular flux if the adaptive PSF model is unavailable")
    p_process_path.set_defaults(adaptive_fallback_to_circular=True)
    p_process_path.add_argument("--psf-profile", choices=("moffat", "gaussian"), default="moffat", help="Spatial-PSF profile family")
    p_process_path.add_argument("--psf-moffat-beta", type=float, default=2.5, help="Fixed Moffat beta for spatial_psf")
    p_process_path.add_argument("--psf-fit-radius-fwhm", type=float, default=2.5, help="PSF fit radius in local major-axis FWHM units")
    p_process_path.add_argument("--psf-min-fit-radius-pix", type=float, default=5.0, help="Minimum spatial-PSF fit radius")
    p_process_path.add_argument("--psf-max-fit-radius-pix", type=float, default=14.0, help="Maximum spatial-PSF fit radius")
    p_process_path.add_argument("--psf-centroid-search-radius-pix", type=float, default=0.75, help="Half-width of forced-position centroid grid search")
    p_process_path.add_argument("--psf-centroid-grid-steps", type=int, default=3, help="Centroid grid steps per axis for spatial_psf")
    p_process_path.add_argument("--psf-residual-clip-sigma", type=float, default=5.0, help="Residual clipping threshold after centroid selection")
    p_process_path.add_argument("--psf-residual-clip-iterations", type=int, default=2, help="Residual clipping iterations for spatial_psf")
    p_process_path.add_argument("--no-psf-adaptive-fallback", dest="psf_fallback_to_adaptive", action="store_false", help="Do not fall back from spatial_psf to adaptive ellipse when profile fitting cannot be used")
    p_process_path.set_defaults(psf_fallback_to_adaptive=True)
    p_process_path.add_argument("--empirical-psf-grid-size", type=int, default=3, help="Spatial empirical PSF grid size per axis")
    p_process_path.add_argument("--empirical-psf-stamp-radius-pix", type=int, default=8, help="Half-size of registered empirical PSF stamps")
    p_process_path.add_argument("--empirical-psf-reference-radius-pix", type=float, default=5.0, help="Reference-aperture radius used to normalize empirical PSF templates")
    p_process_path.add_argument("--empirical-psf-references-per-node", type=int, default=48, help="Nearest PSF stars combined into each grid-node template")
    p_process_path.add_argument("--empirical-psf-min-references-per-node", type=int, default=12, help="Minimum usable reference stars required per empirical PSF node")
    p_process_path.add_argument("--no-empirical-psf-adaptive-fallback", dest="empirical_psf_fallback_to_adaptive", action="store_false", help="Do not fall back from empirical_psf to adaptive ellipse when empirical fitting cannot be used")
    p_process_path.set_defaults(empirical_psf_fallback_to_adaptive=True)
    p_process_path.add_argument("--annulus-inner-pix", type=float, default=8.0, help="Background annulus inner radius")
    p_process_path.add_argument("--annulus-outer-pix", type=float, default=12.0, help="Background annulus outer radius")
    p_process_path.add_argument(
        "--random-error-model",
        choices=("physical_aperture_v1", "legacy_aperture_v1"),
        default="physical_aperture_v1",
        help="Random per-epoch aperture-photometry error model; physical_aperture_v1 is the 0.5.37 default",
    )
    p_process_path.add_argument("--photometry-gain-e-per-adu", type=float, default=None, help="Override detector gain; otherwise use frame/instrument metadata")
    p_process_path.add_argument("--photometry-read-noise-e", type=float, default=None, help="Override detector read noise; otherwise use frame/instrument metadata")
    p_process_path.add_argument("--background-estimator-variance-factor", type=float, default=1.5707963267948966, help="Variance multiplier for annulus-median background estimation")
    p_process_path.add_argument("--no-empirical-background-floor", dest="empirical_background_floor", action="store_false", help="Do not floor detector-predicted background variance by measured annulus scatter")
    p_process_path.set_defaults(empirical_background_floor=True)
    p_process_path.add_argument("--photometry-fractional-flux-floor", type=float, default=None, help="Override profile flat/intrapixel fractional flux-noise floor")
    p_process_path.add_argument("--enable-photometry-scintillation", dest="photometry_scintillation_enabled", action="store_true", help="Enable scintillation term instead of the instrument-profile default")
    p_process_path.add_argument("--disable-photometry-scintillation", dest="photometry_scintillation_enabled", action="store_false", help="Disable scintillation term instead of the instrument-profile default")
    p_process_path.set_defaults(photometry_scintillation_enabled=None)
    p_process_path.add_argument("--photometry-scintillation-coefficient", type=float, default=None, help="Override profile Young-type scintillation coefficient")
    p_process_path.add_argument("--photometry-telescope-aperture-m", type=float, default=None, help="Override telescope aperture diameter in metres")
    p_process_path.add_argument("--photometry-site-altitude-m", type=float, default=None, help="Override site altitude in metres")
    p_process_path.add_argument("--aperture-correction-reference-radius-pix", type=float, default=5.0, help="Large-aperture radius used to derive the spatial aperture correction")
    p_process_path.add_argument("--aperture-correction-max-calibrator-mag-err", type=float, default=0.05, help="Maximum random mag_err for aperture-correction calibrators; use negative to disable")
    p_process_path.add_argument("--aperture-correction-min-calibrators-per-frame", type=int, default=20, help="Minimum usable stars required for a frame aperture model")
    p_process_path.add_argument("--aperture-correction-min-plane-calibrators", type=int, default=30, help="Minimum usable stars required to consider a spatial plane")
    p_process_path.add_argument("--aperture-correction-min-plane-bic-improvement", type=float, default=6.0, help="Minimum BIC improvement required to select a spatial plane")
    p_process_path.add_argument("--aperture-correction-min-spatial-peak-to-peak", type=float, default=0.005, help="Minimum detector-wide spatial correction amplitude in mag")
    p_process_path.add_argument("--aperture-correction-plane-policy", choices=["evidence", "safety_only"], default="evidence", help="Plane selection policy: evidence uses BIC/spatial/MAD preference; safety_only is an experimental diagnostic mode that selects any available plane passing safety gates")
    p_process_path.add_argument("--aperture-correction-max-residual-mad", type=float, default=0.05, help="Maximum per-frame residual MAD; use negative to disable")
    p_process_path.add_argument("--aperture-correction-max-abs-correction", type=float, default=0.5, help="Maximum absolute calibrator/constant correction and legacy combined plane-edge threshold in mag")
    p_process_path.add_argument("--aperture-correction-max-abs-spatial-deviation", type=float, default=0.15, help="Maximum absolute plane deviation from the calibrator pivot across detector corners in mag")
    p_process_path.add_argument("--aperture-correction-max-abs-total-correction", type=float, default=0.75, help="Emergency maximum absolute total plane correction at detector corners in mag")
    p_process_path.add_argument("--aperture-correction-min-good-frame-fraction", type=float, default=0.8, help="Minimum fraction of frames with accepted aperture models")
    p_process_path.add_argument("--skip-aperture-correction", action="store_true", help="Skip small-to-large spatial aperture correction")
    p_process_path.add_argument("--bulk-insert-batch-size", type=int, default=1000, help="Rows per batched DB INSERT/UPSERT inside the field pipeline")
    p_process_path.add_argument("--working-catalog-mag-limit", type=float, default=19.0, help="Gaia/reference magnitude cutoff for the forced-photometry working set; use negative to disable")
    p_process_path.add_argument("--max-working-sources-per-frame", type=int, default=2000, help="Maximum brightest catalog sources linked/measured per frame; use 0 to disable")
    p_process_path.add_argument("--catalog-edge-margin-pix", type=float, default=20.0, help="Reject working catalog sources closer than this to frame edges")
    p_process_path.add_argument("--enable-detection-branch", action="store_true", help="Run source detection and detection-catalog matching; disabled by default for large forced-photometry runs")
    p_process_path.add_argument("--enable-psf-field-diagnostics", action="store_true", help="Measure diagnostic PSF morphology/spatial models after source detection; science photometry is unchanged")
    p_process_path.add_argument("--psf-diagnostics-max-sources-per-frame", type=int, default=500, help="Maximum isolated high-S/N detections used for PSF diagnostics per frame")
    p_process_path.add_argument("--psf-diagnostics-min-snr", type=float, default=20.0, help="Minimum detection S/N for PSF diagnostics")
    p_process_path.add_argument("--psf-diagnostics-cutout-radius-pix", type=int, default=7, help="Radius of the PSF second-moment cutout in pixels")
    p_process_path.add_argument("--psf-diagnostics-min-isolation-pix", type=float, default=12.0, help="Minimum distance to any other detection for PSF diagnostic stars")
    p_process_path.add_argument("--psf-diagnostics-clip-sigma", type=float, default=4.0, help="Robust clipping threshold for spatial PSF models")
    p_process_path.add_argument("--psf-diagnostics-n-iterations", type=int, default=3, help="Robust spatial-model clipping iterations")
    p_process_path.add_argument("--no-psf-diagnostic-plots", dest="psf_diagnostics_render_plots", action="store_false", help="Write CSV/JSON PSF diagnostics without PNG plots")
    p_process_path.set_defaults(psf_diagnostics_render_plots=True)
    p_process_path.add_argument("--require-detection-support", dest="require_detection_support", action="store_true", default=None, help="Reject weak/no detection-support objects in science light curves and candidate triage")
    p_process_path.add_argument("--no-require-detection-support", dest="require_detection_support", action="store_false", help="Treat weak/no detection support as diagnostic only, not as a science-light-curve rejection flag")
    p_process_path.add_argument("--target-source-id", default=None, help="Explicit source_id in the selected catalog namespace (no release equivalence implied)")
    p_process_path.add_argument("--target-object-id", type=int, default=None, help="Explicit internal objects.object_id for the field target")
    p_process_path.add_argument("--target-ra-deg", type=float, default=None, help="Explicit target sky RA in degrees [0, 360); requires --target-dec-deg")
    p_process_path.add_argument("--target-dec-deg", type=float, default=None, help="Explicit target sky Dec in degrees [-90, 90]; requires --target-ra-deg")
    p_process_path.add_argument("--target-max-sep-arcsec", type=float, default=120.0, help="Deprecated positive center limit is ignored; negative disables target resolution without an explicit ID/coordinate pair. Explicit sky matching uses 2 arcsec")
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
    p_process_path.add_argument("--candidate-ensemble-growth-policy", choices=("initial_only", "all"), default="initial_only", help="Whether later ensemble iterations may add newly appearing candidates; initial_only prevents exclusion cascades")
    p_process_path.add_argument("--ensemble-min-ref-stars-per-frame", type=int, default=30, help="Minimum conventional ensemble reference stars required per frame; default preserves prior process-archive-path behavior")
    p_process_path.add_argument("--enable-spatial-ensemble", action="store_true", help="Apply zero-mean per-frame spatial residual calibration after each conventional ensemble pass")
    p_process_path.add_argument("--enable-photometric-frame-qc", action="store_true", help="Enable conservative post-ensemble frame QC: relative/spatial anomalies become SUSPECT diagnostic flags without changing measurement errors; BAD is reserved for hard failures; accepted candidates are rechecked on GOOD epochs")
    p_process_path.add_argument("--spatial-ensemble-max-order", type=int, choices=(1, 2), default=2, help="Maximum spatial polynomial order: 1=plane, 2=quadratic")
    p_process_path.add_argument("--spatial-ensemble-min-ref-stars-per-frame", type=int, default=30, help="Minimum reference stars required per frame")
    p_process_path.add_argument("--spatial-ensemble-clip-sigma", type=float, default=3.5, help="Robust clipping threshold")
    p_process_path.add_argument("--spatial-ensemble-n-iterations", type=int, default=3, help="Robust spatial fit iterations")
    p_process_path.add_argument("--spatial-ensemble-min-bic-improvement", type=float, default=6.0, help="Minimum BIC improvement over identity")
    p_process_path.add_argument("--spatial-ensemble-min-spatial-peak-to-peak", type=float, default=0.003, help="Minimum detector-wide spatial amplitude in mag")
    p_process_path.add_argument("--spatial-ensemble-max-residual-mad", type=float, default=0.05, help="Maximum reference residual MAD per frame; use negative to disable")
    p_process_path.add_argument("--spatial-ensemble-max-residual-mad-degradation", type=float, default=0.0, help="Maximum allowed residual-MAD increase versus identity; 0 requires non-degradation, negative disables")
    p_process_path.add_argument("--spatial-ensemble-max-abs-spatial-deviation", type=float, default=0.15, help="Maximum absolute zero-mean spatial correction")
    p_process_path.add_argument("--spatial-ensemble-min-good-frame-fraction", type=float, default=0.8, help="Minimum valid-frame fraction before global identity fallback")
    p_process_path.add_argument("--enable-aperture-stability-qc", action="store_true", help="Run expensive aperture-radius stability sweep; disabled by default in process-archive-path")
    p_process_path.add_argument("--exclude-bad-pre-qc-frames", action="store_true", help="Exclude frames marked BAD by pre-photometry QC from downstream photometry/calibration")
    p_process_path.add_argument("--force-recompute-photometry", action="store_true", help="Recompute raw forced photometry even if the target calibration already has measurements")
    p_process_path.add_argument("--diagnose-forced-photometry", action="store_true", help="Collect detailed forced-photometry phase timing and throughput diagnostics")
    p_process_path.add_argument("--diagnose-calibration-performance", action="store_true", help="Collect detailed aperture-correction and color-calibration phase timing diagnostics")
    p_process_path.add_argument("--enable-color-calibration", action="store_true", help="Apply the instrument-profile color policy as the final science calibration")
    p_process_path.add_argument("--no-auto-reference-photometry", dest="auto_reference_photometry", action="store_false", help="Do not fetch/import missing Pan-STARRS reference photometry automatically")
    p_process_path.set_defaults(auto_reference_photometry=True)
    p_process_path.add_argument("--color-reference-catalog-name", default=None, help="Override profile reference catalog, e.g. panstarrs_dr2")
    p_process_path.add_argument("--color-reference-band", default=None, help="Override profile reference band")
    p_process_path.add_argument("--color-index", default=None, help="Override profile color index, e.g. r-i")
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
    p_process_path.add_argument("--auto-catalog-mag-limit", type=float, default=None, help="Gaia G limit for auto catalog generation; default inherits --working-catalog-mag-limit")
    p_process_path.add_argument("--auto-catalog-max-rows-per-frame", type=int, default=20000, help="Maximum Gaia rows in the auto catalog query")
    p_process_path.add_argument("--auto-catalog-query-mode", choices=("frame_union", "per_frame"), default="frame_union", help="Gaia query strategy for auto catalogs; frame_union queries one cone for the whole input field")
    p_process_path.add_argument("--auto-catalog-max-radius-deg", type=float, default=None, help="Gaia cone safety radius; default 1.6 deg for MASTER and 1.0 deg otherwise; oversized frame_union falls back to spatial clusters")
    p_process_path.add_argument("--auto-catalog-timeout-sec", type=float, default=120.0, help="Best-effort astroquery timeout for auto Gaia query")
    p_process_path.add_argument("--allow-empty-catalog", action="store_true", help="Debug only: continue pipeline even if auto catalog query fails or inserts zero sources")
    p_process_path.add_argument("--manifest-out", default=None, help="Optional JSON manifest path")
    p_process_path.add_argument("--format", choices=("table", "json"), default="table", help="Console output format")
    p_process_path.set_defaults(func=cmd_process_archive_path)

    p_field_archive = sub.add_parser(
        "process-field-archive",
        help="Run a production field-archive job through the server/service layer and validated M8 workflow",
    )
    p_field_archive.add_argument("--job-config", default=None, help="Optional legacy/field-specific YAML/JSON job config. When present, it supplies field-archive job settings instead of the field_archive section from --config; application database/logging settings from --config still apply.")
    p_field_archive.add_argument(
        "--config", dest="config_file", default=argparse.SUPPRESS,
        help="Grandgousier application config; field_archive section is used (may also be specified before the subcommand)",
    )
    p_field_archive.add_argument("--processed-root", default=None, help="Root containing processed FITS batches; required unless supplied by job config/environment")
    p_field_archive.add_argument("--field-id", type=int, default=None, help="Target fields.field_id; required unless supplied by job config/environment")
    p_field_archive.add_argument("--catalog-name", default=None, help="Explicit shared catalog namespace; required unless supplied by job config/environment")
    p_field_archive.add_argument("--instrument-profile", default=None, help="Instrument profile mode; default MASTER after source merging")
    p_field_archive.add_argument("--execute", dest="execute", action="store_true", default=None, help="Execute Stage 2 and Stage 3")
    p_field_archive.add_argument("--plan-only", dest="execute", action="store_false", help="Force read-only orchestration plan even if config/environment requests execution")
    p_field_archive.add_argument("--resume", dest="resume", action="store_true", default=None, help="Skip READY batches and resume incomplete work; default enabled")
    p_field_archive.add_argument("--no-resume", dest="resume", action="store_false", help="Refuse already-indexed or incomplete batches")
    p_field_archive.add_argument(
        "--stage2-recipe",
        choices=("stage3_scale_v1",),
        default=None,
        help="Frozen Stage-2 scientific recipe; default stage3_scale_v1 after source merging",
    )
    p_field_archive.add_argument(
        "--stage3-policy",
        dest="stage3_policies",
        action="append",
        default=None,
        help="Explicit FILTER:MIN_COMMON[:REFERENCE_SERIES_ID[:INSTRUMENT_UNIT_ID]] policy; repeat per filter/system. Required unless supplied by job config/environment.",
    )
    p_field_archive.add_argument("--control-object-id", dest="control_object_ids", action="append", type=int, default=None, help="M5 control object; may be repeated")
    p_field_archive.add_argument("--exclude-date", dest="exclude_dates", action="append", default=None, help="Observing date YYYY-MM-DD to skip explicitly; may be repeated")
    p_field_archive.add_argument("--include-date", dest="include_dates", action="append", default=None, help="Optional allow-list date YYYY-MM-DD; may be repeated")
    p_field_archive.add_argument("--out-root", default=None, help="Consolidated report/output directory; default reports/field_archive")
    p_field_archive.add_argument("--plot-max-points", type=int, default=None, help="M5 control plot hard cap per filter; default 200")
    p_field_archive.add_argument("--no-preflight", action="store_true", help="Skip service readiness checks; intended only for controlled diagnostics/tests")
    p_field_archive.add_argument("--format", choices=("table", "json", "job-json"), default="table", help="Console result format; json preserves the legacy M8 payload when M8 runs, job-json returns the service wrapper")
    p_field_archive.set_defaults(func=cmd_process_field_archive)

    p_field_archive_ready = sub.add_parser(
        "field-archive-readiness",
        help="Check package resources, processed/output roots, PostgreSQL connectivity and migration readiness without running M8",
    )
    p_field_archive_ready.add_argument("--processed-root", required=True, help="Processed FITS root that the service must be able to read")
    p_field_archive_ready.add_argument("--instrument-profile", default="MASTER", help="Instrument profile mode: auto, MASTER or RoboPhot")
    p_field_archive_ready.add_argument("--out-root", default="reports/field_archive", help="Output root that the service must be able to write")
    p_field_archive_ready.add_argument("--format", choices=("table", "json"), default="table", help="Console result format")
    p_field_archive_ready.set_defaults(func=cmd_field_archive_readiness)

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

    p_show_calibration_iterations = sub.add_parser(
        "show-calibration-iterations",
        help="Show compact normalized calibration iterations for one product",
    )
    p_show_calibration_iterations.add_argument("--calibration-name", default=None)
    p_show_calibration_iterations.add_argument("--calibration-version-id", type=int, default=None)
    p_show_calibration_iterations.add_argument("--format", choices=("table", "csv", "json"), default="table")
    p_show_calibration_iterations.set_defaults(func=cmd_show_calibration_iterations)

    p_verify_calibration_terms = sub.add_parser(
        "verify-calibration-terms",
        help="Compare normalized calibration terms with the legacy materialized layer",
    )
    p_verify_calibration_terms.add_argument("--calibration-name", default=None)
    p_verify_calibration_terms.add_argument("--calibration-version-id", type=int, default=None)
    p_verify_calibration_terms.add_argument("--tolerance", type=float, default=1e-10)
    p_verify_calibration_terms.add_argument("--format", choices=("table", "json"), default="table")
    p_verify_calibration_terms.set_defaults(func=cmd_verify_calibration_terms)

    p_verify_calibration_reconstruction = sub.add_parser(
        "verify-calibration-reconstruction",
        help="Reconstruct a calibration from base photometry + normalized terms and compare it with the materialized layer",
    )
    p_verify_calibration_reconstruction.add_argument("--calibration-name", default=None)
    p_verify_calibration_reconstruction.add_argument("--calibration-version-id", type=int, default=None)
    p_verify_calibration_reconstruction.add_argument("--tolerance", type=float, default=1e-10)
    p_verify_calibration_reconstruction.add_argument("--format", choices=("table", "json"), default="table")
    p_verify_calibration_reconstruction.set_defaults(func=cmd_verify_calibration_reconstruction)

    p_show_artifacts = sub.add_parser("show-artifacts", help="Show registered immutable file artifacts")
    p_show_artifacts.add_argument("--pipeline-run-id", type=int, default=None, help="Filter by pipeline run")
    p_show_artifacts.add_argument("--batch-run-id", type=int, default=None, help="Filter by batch run")
    p_show_artifacts.add_argument("--batch-job-attempt-id", type=int, default=None, help="Filter by batch job attempt")
    p_show_artifacts.add_argument("--series-id", type=int, default=None, help="Filter by field series")
    p_show_artifacts.add_argument("--artifact-type", default=None, help="Filter by artifact type")
    p_show_artifacts.add_argument("--limit", type=int, default=100, help="Maximum rows")
    p_show_artifacts.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_show_artifacts.set_defaults(func=cmd_show_artifacts)

    p_verify_artifacts = sub.add_parser("verify-artifacts", help="Verify registered artifact size and SHA-256")
    p_verify_artifacts.add_argument("--pipeline-run-id", type=int, default=None, help="Verify artifacts for one pipeline run")
    p_verify_artifacts.add_argument("--batch-run-id", type=int, default=None, help="Verify artifacts for one batch run")
    p_verify_artifacts.add_argument("--batch-job-attempt-id", type=int, default=None, help="Verify artifacts for one batch job attempt")
    p_verify_artifacts.add_argument("--artifact-id", type=int, default=None, help="Verify one artifact")
    p_verify_artifacts.add_argument("--limit", type=int, default=1000, help="Maximum artifacts to verify")
    p_verify_artifacts.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_verify_artifacts.set_defaults(func=cmd_verify_artifacts)

    p_batch_sync = sub.add_parser("batch-sync-plan", help="Synchronize a scanned batch plan into PostgreSQL")
    p_batch_sync.add_argument("--batch-name", required=True)
    p_batch_sync.add_argument("--root", required=True, help="Archive root path")
    p_batch_sync.add_argument("--inventory-csv", required=True)
    p_batch_sync.add_argument("--queue-csv", required=True)
    p_batch_sync.add_argument("--legacy-state-csv", default=None, help="Optional pre-B3 state.csv to import once")
    p_batch_sync.add_argument("--script-name", default=None)
    p_batch_sync.add_argument("--script-version", default=None)
    p_batch_sync.add_argument("--config-json-file", default=None)
    p_batch_sync.set_defaults(func=cmd_batch_sync_plan)

    p_batch_start = sub.add_parser("batch-start-attempt", help="Create a RUNNING attempt for one batch job")
    p_batch_start.add_argument("--batch-run-id", type=int, required=True)
    p_batch_start.add_argument("--job-id", required=True)
    p_batch_start.add_argument("--attempt-kind", default="EXECUTION")
    p_batch_start.add_argument("--manifest-path", default=None)
    p_batch_start.add_argument("--console-log-path", default=None)
    p_batch_start.add_argument("--metadata-json-file", default=None)
    p_batch_start.set_defaults(func=cmd_batch_start_attempt)

    p_batch_finish = sub.add_parser("batch-finish-attempt", help="Finalize one DB-owned batch job attempt")
    p_batch_finish.add_argument("--batch-job-attempt-id", type=int, required=True)
    p_batch_finish.add_argument("--exit-code", type=int, default=None)
    p_batch_finish.add_argument("--status", required=True)
    p_batch_finish.add_argument("--health", required=True)
    p_batch_finish.add_argument("--science-status", default=None)
    p_batch_finish.add_argument("--catalog-name", default=None)
    p_batch_finish.add_argument("--catalog-sources", type=int, default=0)
    p_batch_finish.add_argument("--pipeline-count", type=int, default=0)
    p_batch_finish.add_argument("--total-points", type=int, default=0)
    p_batch_finish.add_argument("--accepted-points", type=int, default=0)
    p_batch_finish.add_argument("--elapsed-sec", type=float, default=0.0)
    p_batch_finish.add_argument("--manifest-path", default=None)
    p_batch_finish.add_argument("--console-log-path", default=None)
    p_batch_finish.add_argument("--message", default=None)
    p_batch_finish.add_argument("--pipeline-run-id", type=int, default=None)
    p_batch_finish.set_defaults(func=cmd_batch_finish_attempt)

    p_batch_export = sub.add_parser("batch-export-state", help="Export DB-owned batch state to compatibility CSV files")
    p_batch_export.add_argument("--batch-run-id", type=int, required=True)
    p_batch_export.add_argument("--state-csv", required=True)
    p_batch_export.add_argument("--summary-csv", required=True)
    p_batch_export.set_defaults(func=cmd_batch_export_state)

    p_show_batches = sub.add_parser("show-batch-runs", help="Show database-owned batch queues")
    p_show_batches.add_argument("--limit", type=int, default=50)
    p_show_batches.add_argument("--format", choices=("table", "csv", "json"), default="table")
    p_show_batches.set_defaults(func=cmd_show_batch_runs)

    p_show_batch_jobs = sub.add_parser("show-batch-jobs", help="Show current jobs in one database-owned batch queue")
    p_show_batch_jobs.add_argument("--batch-run-id", type=int, required=True)
    p_show_batch_jobs.add_argument("--format", choices=("table", "csv", "json"), default="table")
    p_show_batch_jobs.set_defaults(func=cmd_show_batch_jobs)

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

    p_interseries_plan = sub.add_parser(
        "plan-interseries-calibration",
        help="Inspect Stage-3 readiness and pairwise overlap for one field",
    )
    field_scope = p_interseries_plan.add_mutually_exclusive_group(required=True)
    field_scope.add_argument("--field-id", type=int, default=None, help="fields.field_id")
    field_scope.add_argument("--field-name", default=None, help="Exact fields.field_name, case-insensitive")
    p_interseries_plan.add_argument(
        "--instrument-unit-id", type=int, default=None, help="Optionally restrict candidate series to one instrument unit"
    )
    p_interseries_plan.add_argument(
        "--filter", dest="filter_name", default=None, help="Optionally restrict candidate series to one filter"
    )
    p_interseries_plan.add_argument(
        "--catalog-name", default=None, help="Optionally restrict selected SCIENCE generations to one catalog namespace"
    )
    p_interseries_plan.add_argument(
        "--science-calibration-version-id",
        type=int,
        action="append",
        default=None,
        help="Explicit current SCIENCE alias head ID; repeat once per selected series",
    )
    p_interseries_plan.add_argument(
        "--min-points-per-object", type=int, default=5, help="Minimum science points in each series for an object to count toward overlap"
    )
    p_interseries_plan.add_argument(
        "--min-common-objects", type=int, default=20, help="Minimum common usable objects for an eligible pair"
    )
    p_interseries_plan.add_argument(
        "--required-stage2-recipe",
        choices=("stage3_scale_v1",),
        default=None,
        help="Require persisted Stage-2 provenance compatible with this recipe contract",
    )
    p_interseries_plan.add_argument("--out", default=None, help="Optional JSON plan path")
    p_interseries_plan.add_argument(
        "--format", choices=("table", "json"), default="table", help="Console output format"
    )
    p_interseries_plan.set_defaults(func=cmd_plan_interseries_calibration)

    p_interseries_analyze = sub.add_parser(
        "analyze-interseries-calibration",
        help="Run read-only Stage-3 M1-M3 representative-state, pairwise-offset and graph analysis",
    )
    analyze_scope = p_interseries_analyze.add_mutually_exclusive_group(required=True)
    analyze_scope.add_argument("--field-id", type=int, default=None, help="fields.field_id")
    analyze_scope.add_argument("--field-name", default=None, help="Exact fields.field_name, case-insensitive")
    p_interseries_analyze.add_argument(
        "--instrument-unit-id", type=int, default=None, help="Restrict analysis to one instrument unit"
    )
    p_interseries_analyze.add_argument(
        "--filter", dest="filter_name", default=None, help="Restrict analysis to one filter"
    )
    p_interseries_analyze.add_argument(
        "--catalog-name", default=None, help="Restrict selected SCIENCE generations to one catalog namespace"
    )
    p_interseries_analyze.add_argument(
        "--science-calibration-version-id",
        type=int,
        action="append",
        default=None,
        help="Explicit current SCIENCE alias head ID; repeat once per selected series",
    )
    p_interseries_analyze.add_argument(
        "--reference-series-id", type=int, default=None, help="Explicit graph gauge/reference series"
    )
    p_interseries_analyze.add_argument(
        "--min-points-per-object", type=int, default=5, help="Minimum science points per object inside each series"
    )
    p_interseries_analyze.add_argument(
        "--min-common-objects", type=int, default=20, help="Minimum common reference objects required for a pair"
    )
    p_interseries_analyze.add_argument(
        "--clip-sigma", type=float, default=4.0, help="MAD-sigma clipping threshold for pairwise calibrators"
    )
    p_interseries_analyze.add_argument(
        "--max-clip-iterations", type=int, default=5, help="Maximum robust clipping iterations per pair"
    )
    p_interseries_analyze.add_argument(
        "--required-stage2-recipe",
        choices=("stage3_scale_v1",),
        default=None,
        help="Optionally require persisted Stage-2 provenance compatible with this recipe contract",
    )
    p_interseries_analyze.add_argument(
        "--out-root", default="reports/interseries_analysis", help="Directory for Stage-3 diagnostic CSV/JSON files"
    )
    p_interseries_analyze.add_argument(
        "--format", choices=("table", "json"), default="table", help="Console output format"
    )
    p_interseries_analyze.set_defaults(func=cmd_analyze_interseries_calibration)

    p_interseries_persist = sub.add_parser(
        "persist-interseries-calibration",
        help="Persist one immutable Stage-3 M4 graph-calibration generation",
    )
    persist_scope = p_interseries_persist.add_mutually_exclusive_group(required=True)
    persist_scope.add_argument("--field-id", type=int, default=None, help="fields.field_id")
    persist_scope.add_argument("--field-name", default=None, help="Exact fields.field_name, case-insensitive")
    p_interseries_persist.add_argument("--instrument-unit-id", type=int, default=None)
    p_interseries_persist.add_argument("--filter", dest="filter_name", default=None)
    p_interseries_persist.add_argument("--catalog-name", default=None)
    p_interseries_persist.add_argument(
        "--science-calibration-version-id",
        type=int,
        action="append",
        default=None,
        help="Explicit current SCIENCE alias head ID; repeat once per selected series",
    )
    p_interseries_persist.add_argument("--reference-series-id", type=int, default=None)
    p_interseries_persist.add_argument("--min-points-per-object", type=int, default=5)
    p_interseries_persist.add_argument("--min-common-objects", type=int, default=20)
    p_interseries_persist.add_argument("--clip-sigma", type=float, default=4.0)
    p_interseries_persist.add_argument("--max-clip-iterations", type=int, default=5)
    p_interseries_persist.add_argument(
        "--required-stage2-recipe",
        choices=("stage3_scale_v1",),
        required=True,
        help="Required Stage-2 recipe contract for safe M4 persistence",
    )
    p_interseries_persist.add_argument(
        "--allow-sparse-graph", action="store_true",
        help="Explicitly allow persistence of a connected tree with graph_dof=0 and no closure test",
    )
    p_interseries_persist.add_argument("--format", choices=("table", "json"), default="table")
    p_interseries_persist.set_defaults(func=cmd_persist_interseries_calibration)

    p_interseries_show = sub.add_parser(
        "show-interseries-calibration",
        help="Show persisted immutable Stage-3 M4 generations",
    )
    p_interseries_show.add_argument("--generation-id", type=int, default=None)
    p_interseries_show.add_argument("--field-id", type=int, default=None)
    p_interseries_show.add_argument("--filter", dest="filter_name", default=None)
    p_interseries_show.add_argument("--limit", type=int, default=50)
    p_interseries_show.add_argument("--format", choices=("table", "json"), default="table")
    p_interseries_show.set_defaults(func=cmd_show_interseries_calibration)

    p_interseries_verify = sub.add_parser(
        "verify-interseries-calibration",
        help="Verify fingerprint, exact Stage-2 SCIENCE provenance and stored graph equations for one M4 generation",
    )
    p_interseries_verify.add_argument("--generation-id", type=int, required=True)
    p_interseries_verify.add_argument("--format", choices=("table", "json"), default="table")
    p_interseries_verify.set_defaults(func=cmd_verify_interseries_calibration)

    p_global_object_lc = sub.add_parser(
        "export-global-object-light-curve",
        help="Export read-only M5 global object light curve from exact Stage-2 SCIENCE + persisted M4 generations",
    )
    p_global_object_lc.add_argument("--object-id", type=int, required=True, help="Internal objects.object_id")
    p_global_object_lc.add_argument("--field-id", type=int, required=True, help="Field whose persisted M4 generations define the global scale")
    p_global_object_lc.add_argument(
        "--generation-id",
        dest="generation_ids",
        type=int,
        action="append",
        default=[],
        help="Explicit M4 generation id; repeat for multiple filters/systems. Without this, auto-selection requires one unambiguous generation per scope.",
    )
    p_global_object_lc.add_argument(
        "--out-root",
        required=True,
        help=(
            "Final bundle directory (must not exist); members keep stable basenames "
            "<name>.csv, <name>_summary.json, optional <name>_rejections.csv, and "
            "per-filter <name>_<filter>_plot.csv plus image. Existing bundles and legacy "
            "flat sibling outputs are refused. One same-filesystem native no-replace "
            "directory rename publishes the bundle. A crash may leave a hidden "
            ".<name>.m5-staging-* sibling, not a final output; it is not auto-cleaned. "
            "Windows os.rename, Linux renameat2(RENAME_NOREPLACE), and macOS "
            "renamex_np(RENAME_EXCL) are supported; unsupported platforms/filesystems "
            "fail closed. No fsync/power-loss durability guarantee."
        ),
    )
    p_global_object_lc.add_argument(
        "--include-rejected", action="store_true",
        help="Include Stage-2 rejected rows in the main CSV; plots always use accepted points only",
    )
    p_global_object_lc.add_argument(
        "--include-rejected-debug", action="store_true",
        help=(
            "Write <name>_rejections.csv inside the output bundle with rejected rows and QC "
            "reasons"
        ),
    )
    p_global_object_lc.add_argument(
        "--allow-partial", action="store_true",
        help="Allow export of only exact M4-linked series when the object also has current SCIENCE points in unlinked/stale series",
    )
    p_global_object_lc.add_argument(
        "--no-require-detection-support",
        dest="require_detection_support",
        action="store_false",
        help="Treat NO/WEAK detection support as diagnostic rather than an M5 rejection flag",
    )
    p_global_object_lc.set_defaults(require_detection_support=True)
    p_global_object_lc.add_argument(
        "--extra-reject-flags", default=None,
        help="Comma-separated additional science rejection flags",
    )
    p_global_object_lc.add_argument(
        "--no-plots", action="store_true",
        help="Do not write per-filter plot CSVs or images",
    )
    p_global_object_lc.add_argument(
        "--plot-reduction",
        choices=("auto", "bin", "none"),
        default="auto",
        help="Plot-layer reduction: auto uses raw points below the limit and time bins above it; default auto",
    )
    p_global_object_lc.add_argument(
        "--plot-max-points", type=int, default=5000,
        help="Hard maximum plot points per filter in binned plotting; main CSV is not downsampled",
    )
    p_global_object_lc.add_argument(
        "--plot-bin-minutes", type=float, default=None,
        help="Requested minimum per-series time-bin width in minutes; M5 may coarsen it to respect --plot-max-points",
    )
    p_global_object_lc.add_argument(
        "--plot-format", choices=("png", "pdf", "svg"), default="png",
        help="Per-filter plot format; default png",
    )
    p_global_object_lc.add_argument(
        "--plot-time-axis",
        choices=("auto", "continuous", "compressed"),
        default="auto",
        help="Time-axis display: auto shortens large empty gaps between series; continuous keeps physical spacing; compressed forces gap shortening when possible",
    )
    p_global_object_lc.add_argument("--plot-dpi", type=int, default=160, help="Raster plot DPI")
    p_global_object_lc.add_argument("--format", choices=("table", "json"), default="table")
    p_global_object_lc.set_defaults(func=cmd_export_global_object_light_curve)

    p_catalog_identity = sub.add_parser(
        "audit-field-catalog-identity",
        help="Check whether repeated series of one field share a catalog/object identity namespace",
    )
    catalog_identity_scope = p_catalog_identity.add_mutually_exclusive_group(required=True)
    catalog_identity_scope.add_argument("--field-id", type=int, default=None, help="fields.field_id")
    catalog_identity_scope.add_argument(
        "--field-name", default=None, help="Exact fields.field_name, case-insensitive"
    )
    p_catalog_identity.add_argument(
        "--format", choices=("table", "json"), default="table", help="Console output format"
    )
    p_catalog_identity.set_defaults(func=cmd_audit_field_catalog_identity)

    p_list_science_lc = sub.add_parser("list-light-curve-objects", help="List objects with science light-curve coverage")
    _add_science_lc_scope_arguments(p_list_science_lc)
    p_list_science_lc.add_argument(
        "--interseries-generation-id",
        type=int,
        action="append",
        default=None,
        help="Explicit M4 generation; repeat once per field/instrument/filter/catalog scope",
    )
    p_list_science_lc.add_argument("--min-points", type=int, default=1, help="Minimum points per object")
    p_list_science_lc.add_argument("--exclude-rejected", action="store_true", help="Count only accepted science points")
    p_list_science_lc.add_argument("--limit", type=int, default=50, help="Maximum objects to show")
    p_list_science_lc.add_argument("--format", choices=("table", "csv", "json"), default="table", help="Output format")
    p_list_science_lc.set_defaults(func=cmd_list_light_curve_objects)

    p_science_lc_summary = sub.add_parser("light-curve-summary", help="Summarize science light-curve product coverage and QC")
    _add_science_lc_scope_arguments(p_science_lc_summary)
    p_science_lc_summary.add_argument(
        "--interseries-generation-id",
        type=int,
        action="append",
        default=None,
        help="Explicit M4 generation; repeat once per field/instrument/filter/catalog scope",
    )
    p_science_lc_summary.add_argument("--out", default=None, help="Optional JSON report path")
    p_science_lc_summary.add_argument("--format", choices=("table", "json"), default="table", help="Console output format")
    p_science_lc_summary.set_defaults(func=cmd_light_curve_summary)

    p_export_science_lc = sub.add_parser("export-light-curve", help="Export science light-curve product CSV")
    _add_science_lc_scope_arguments(p_export_science_lc)
    p_export_science_lc.add_argument(
        "--interseries-generation-id",
        type=int,
        action="append",
        default=None,
        help="Explicit M4 generation; repeat once per field/instrument/filter/catalog scope",
    )
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

    p_psf_diag = sub.add_parser("psf-field-diagnostics", help="Analyze PSF morphology and spatial field trends from stored source detections without changing science photometry")
    p_psf_diag.add_argument("--series-id", type=int, default=None, help="Analyze all frames assigned to this field_series")
    p_psf_diag.add_argument("--frame-id", type=int, action="append", help="Specific frame_id; may be repeated")
    p_psf_diag.add_argument("--instrument-unit-id", type=int, default=None, help="Optional instrument-unit filter")
    p_psf_diag.add_argument("--detection-algorithm", default="numpy_localmax_v1", help="Stored source-detection run algorithm")
    p_psf_diag.add_argument("--out-dir", required=True, help="Output directory for psf_sources.csv, psf_frames.csv, JSON and plots")
    p_psf_diag.add_argument("--max-sources-per-frame", type=int, default=500, help="Maximum isolated high-S/N diagnostic stars per frame")
    p_psf_diag.add_argument("--min-snr", type=float, default=20.0, help="Minimum source-detection S/N")
    p_psf_diag.add_argument("--cutout-radius-pix", type=int, default=7, help="PSF second-moment cutout radius")
    p_psf_diag.add_argument("--min-isolation-pix", type=float, default=12.0, help="Minimum distance to any other stored detection")
    p_psf_diag.add_argument("--clip-sigma", type=float, default=4.0, help="Robust clipping threshold for spatial models")
    p_psf_diag.add_argument("--n-iterations", type=int, default=3, help="Spatial-model clipping iterations")
    p_psf_diag.add_argument("--no-plots", dest="render_plots", action="store_false", help="Skip diagnostic PNG rendering")
    p_psf_diag.set_defaults(render_plots=True, func=cmd_psf_field_diagnostics)

    p_adaptive_ap = sub.add_parser(
        "adaptive-aperture-diagnostics",
        help="Compare circular and local-PSF adaptive elliptical aperture photometry without changing science measurements",
    )
    p_adaptive_ap.add_argument("--series-id", type=int, required=True, help="Field series to analyze")
    p_adaptive_ap.add_argument("--catalog-name", default=None, help="Optional catalog_sources catalog_name filter")
    p_adaptive_ap.add_argument("--detection-algorithm", default="numpy_localmax_v1", help="Stored source-detection run algorithm used to build the local PSF field")
    p_adaptive_ap.add_argument("--out-dir", required=True, help="Output directory for adaptive-aperture CSV/JSON/PNG diagnostics")
    p_adaptive_ap.add_argument("--base-aperture-radius-pix", type=float, default=3.0, help="Baseline circular aperture radius in pixels")
    p_adaptive_ap.add_argument("--annulus-inner-pix", type=float, default=8.0, help="Baseline local-background annulus inner radius")
    p_adaptive_ap.add_argument("--annulus-outer-pix", type=float, default=12.0, help="Baseline local-background annulus outer radius")
    p_adaptive_ap.add_argument("--min-psf-snr", type=float, default=20.0, help="Minimum detection S/N for local PSF reference stars")
    p_adaptive_ap.add_argument("--max-psf-sources-per-frame", type=int, default=500, help="Maximum isolated PSF reference stars per frame")
    p_adaptive_ap.add_argument("--psf-cutout-radius-pix", type=int, default=7, help="Second-moment PSF cutout radius")
    p_adaptive_ap.add_argument("--psf-min-isolation-pix", type=float, default=12.0, help="Minimum isolation of PSF reference detections")
    p_adaptive_ap.add_argument("--interpolation-neighbors", type=int, default=24, help="Nearest PSF references used for local covariance interpolation")
    p_adaptive_ap.add_argument("--min-psf-references", type=int, default=20, help="Minimum successful PSF references required per frame")
    p_adaptive_ap.add_argument("--aperture-fwhm-factor", type=float, default=1.0, help="Adaptive equivalent radius is max(base radius, factor * local geometric FWHM)")
    p_adaptive_ap.add_argument("--max-axis-ratio", type=float, default=3.0, help="Safety clamp on adaptive semi-major/semi-minor ratio")
    p_adaptive_ap.add_argument("--max-radius-scale", type=float, default=2.5, help="Safety clamp on adaptive equivalent radius relative to the base radius")
    p_adaptive_ap.add_argument("--no-plots", dest="render_plots", action="store_false", help="Skip diagnostic PNG rendering")
    p_adaptive_ap.set_defaults(render_plots=True, func=cmd_adaptive_aperture_diagnostics)

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
    p_gaia.add_argument("--max-rows-per-frame", type=int, default=20000, help="Gaia TOP N per query")
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
    p_match_det.add_argument("--detection-algorithm", default="numpy_localmax_v1", help="source-detection run algorithm to use")
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


def cmd_reset_derived_data(args: argparse.Namespace) -> int:
    from grandgousier.db.derived_reset import audit_stage2_reset, reset_stage2_derived_data

    if args.scope != "stage2":
        raise SystemExit(f"Unsupported derived-data scope: {args.scope}")
    if not args.all_series:
        print("Refusing global Stage-2 reset without --all-series.")
        return 2
    if args.execute and not args.yes:
        print("Refusing destructive reset without --yes.")
        return 2

    Session = make_session_factory(args.database_url)
    if not args.execute:
        with Session() as session:
            audit = audit_stage2_reset(session)
        payload = audit.to_dict()
        if args.format == "json":
            print(json.dumps({"executed": False, **payload}, ensure_ascii=False, indent=2))
            return 0
        print(
            "Derived-data reset audit: scope=stage2, mode=all_series, "
            f"derived_rows={audit.total_derived_rows}, frames={audit.n_frames_to_reset}, "
            f"series={audit.n_series_to_reset}, fields={audit.n_fields_to_reset}"
        )
        print("Would truncate (transactional; identity sequences continue):")
        _print_rows(
            [row.to_dict() for row in audit.derived],
            ["table_name", "rows_before", "action"],
            "table",
        )
        print("Would preserve:")
        _print_rows(
            [row.to_dict() for row in audit.preserved],
            ["table_name", "rows"],
            "table",
        )
        print("DRY RUN: no database rows were changed.")
        return 0

    with Session() as session:
        try:
            result = reset_stage2_derived_data(session)
            session.commit()
        except Exception:
            session.rollback()
            raise

    if args.format == "json":
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0
    print(
        "Derived-data reset completed: scope=stage2, mode=all_series, "
        f"truncated_rows={result.audit.total_derived_rows}, frames_reset={result.frames_reset}, "
        f"series_reset={result.series_reset}, fields_reset={result.fields_reset}"
    )
    print("Preserved archive/catalog/object identity and schema migration ledger.")
    return 0


def cmd_reclaim_stage1_products(args: argparse.Namespace) -> int:
    from grandgousier.archive.stage1_reclaim import reclaim_series

    Session = make_session_factory(args.database_url)
    with Session() as session:
        result = reclaim_series(
            session,
            series_id=args.series_id,
            processed_root=args.processed_root,
            manifest_root=args.manifest_root,
            execute=args.execute,
            report_path=args.report_out,
        )
    payload = {"execute": bool(args.execute), **result.to_dict()}
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(
            f"Stage-1 reclaim {result.status.lower()}: series={result.series_id}, "
            f"frames={result.summary.get('total_series_frames', 0)}, "
            f"reclaimable={result.summary.get('reclaimable_frames', 0)}, "
            f"already_reclaimed={result.summary.get('already_reclaimed_frames', 0)}, "
            f"blocked={result.summary.get('blocked_frames', 0)}, "
            f"reclaimable_bytes={result.summary.get('reclaimable_bytes', 0)}, "
            f"reasons={','.join(result.reason_codes) or 'none'}"
        )
        if not args.execute:
            print("PLAN ONLY: no files were changed.")
    return 0 if result.status in {"RECLAIMABLE", "RECLAIMED", "ALREADY_RECLAIMED"} else 2


def cmd_legacy_prune_stage1_products(args: argparse.Namespace) -> int:
    from sqlalchemy.engine import make_url

    from grandgousier.archive.legacy_stage1_cleanup import (
        build_plan,
        execute_plan,
        write_plan,
    )

    Session = make_session_factory(args.database_url)
    manifest_root = Path(args.manifest_root).absolute()
    database_url = args.database_url or get_settings().database_url
    database_identity = "unknown"
    if database_url:
        url = make_url(str(database_url))
        database_identity = ":".join(
            str(part) for part in (url.get_backend_name(), url.host or "local", url.port or "default", url.database or "none")
        )
    with Session() as session:
        if args.execute:
            result = execute_plan(session, manifest_root, database_identity=database_identity)
            payload = {"execute": True, **result}
        else:
            plan = build_plan(session, args.processed_root, field_id=args.field_id, object_name=args.object_name)
            write_plan(manifest_root, plan, database_identity=database_identity)
            payload = {"execute": False, **plan}
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        summary = payload.get("summary", payload)
        print(
            "Legacy Stage-1 cleanup " + ("executed" if args.execute else "planned") + ": "
            f"db_linked={summary.get('db_linked_products', summary.get('planned_files', 0))}, "
            f"reclaimable={summary.get('legacy_reclaimable', summary.get('deleted_files', 0))}, "
            f"blocked={summary.get('blocked', 0)}, "
            f"unlinked={summary.get('unlinked', 0)}, "
            f"bytes={summary.get('reclaimable_bytes', summary.get('deleted_bytes', 0))}"
        )
        if not args.execute:
            print("PLAN ONLY: no files were changed.")
    return 0


def cmd_schema_status(args: argparse.Namespace) -> int:
    from grandgousier.db.migrations import status, summarize_status
    from grandgousier.db.session import make_engine

    engine = make_engine(args.database_url)
    items = status(engine)
    summary = summarize_status(items)
    print(
        "schema migrations: "
        f"total={summary['total']}, applied={summary['applied']}, "
        f"baselined={summary['baselined']}, pending={summary['pending']}, "
        f"checksum_drift={summary['checksum_drift']}"
    )
    for item in items:
        print(
            f"  {item.migration_id}: state={item.state}, checksum={item.checksum_status}, "
            f"file={item.filename}"
        )
    return 0 if summary["pending"] == 0 and summary["checksum_drift"] == 0 else 2


def cmd_migrate_db(args: argparse.Namespace) -> int:
    from grandgousier.db.migrations import MigrationError, apply_pending, status, summarize_status
    from grandgousier.db.session import make_engine

    engine = make_engine(args.database_url)
    try:
        applied = apply_pending(
            engine, adopt_legacy=bool(args.adopt_legacy), dry_run=bool(args.dry_run)
        )
    except MigrationError as exc:
        print(f"Migration error: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        if applied:
            print("Pending managed migrations:")
            for migration_id in applied:
                print(f"  {migration_id}")
        else:
            print("No pending managed migrations.")
        return 0

    if applied:
        print("Applied migrations:")
        for migration_id in applied:
            print(f"  {migration_id}")
    else:
        print("No managed migrations were applied.")
    summary = summarize_status(status(engine))
    print(
        "schema migrations: "
        f"applied={summary['applied']}, baselined={summary['baselined']}, "
        f"pending={summary['pending']}, checksum_drift={summary['checksum_drift']}"
    )
    return 0 if summary["pending"] == 0 and summary["checksum_drift"] == 0 else 2


def cmd_backfill_observation_identity(args: argparse.Namespace) -> int:
    from collections import Counter

    from grandgousier.identity.backfill import (
        SUPPORTED_IDENTITY_INSTRUMENTS,
        ObservationIdentityBackfillError,
        ObservationIdentitySchemaError,
        build_observation_identity_plan,
        execute_observation_identity_backfill,
        write_observation_identity_report,
    )

    instruments = tuple(args.instrument or SUPPORTED_IDENTITY_INSTRUMENTS)
    Session = make_session_factory(args.database_url)
    result = None
    try:
        with Session() as session:
            plan = build_observation_identity_plan(session, instruments=instruments)
            if args.execute:
                if not plan.can_execute:
                    session.rollback()
                else:
                    try:
                        result = execute_observation_identity_backfill(session, plan)
                        session.commit()
                    except Exception:
                        session.rollback()
                        raise
            else:
                session.rollback()
    except (ObservationIdentitySchemaError, ObservationIdentityBackfillError) as exc:
        print(f"Observation identity error: {exc}", file=sys.stderr)
        return 2

    write_observation_identity_report(plan, out_root=args.out_root, result=result)

    if args.format == "json":
        payload = {"plan": plan.to_dict(), "result": result.to_dict() if result is not None else {"executed": False}}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        mode = "EXECUTED" if result is not None else "PLAN_ONLY"
        print(
            "Observation identity backfill: "
            f"mode={mode}, frames={plan.frames_scanned}, candidates={plan.candidate_frames}, "
            f"observations={plan.observation_groups}, missing={plan.missing_identity_frames}, "
            f"invalid={plan.invalid_identity_frames}, collisions={plan.collision_groups}, "
            f"can_execute={'YES' if plan.can_execute else 'NO'}"
        )
        by_instrument = Counter(group.instrument for group in plan.groups)
        frame_counts = Counter()
        for group in plan.groups:
            frame_counts[group.instrument] += group.n_frames
        for instrument in instruments:
            print(
                f"  {instrument}: frames={frame_counts[instrument]}, "
                f"observations={by_instrument[instrument]}"
            )
        print(
            "  groups: "
            f"single={plan.single_frame_groups}, copy_like={plan.copy_like_groups}, "
            f"product_variant={plan.product_variant_groups}, collision={plan.collision_groups}"
        )
        print(
            "  links: "
            f"existing_observations={plan.existing_observations}, "
            f"already_linked={plan.already_linked_frames}, "
            f"unlinked_candidates={plan.unlinked_candidate_frames}, "
            f"link_conflicts={plan.link_conflict_frames}"
        )
        if result is not None:
            print(
                "  written: "
                f"observations_created={result.observations_created}, "
                f"observations_reused={result.observations_reused}, "
                f"frames_linked={result.frames_linked}, "
                f"frames_already_linked={result.frames_already_linked}"
            )
        elif args.execute and not plan.can_execute:
            print("REFUSED: plan contains blocking conflicts; database was not changed.")
        else:
            print("DRY RUN: database was not changed. Re-run with --execute after reviewing reports.")
        print(f"Report: {args.out_root}")

    if args.execute and not plan.can_execute:
        return 2
    return 0


def cmd_stage1_plan(args: argparse.Namespace) -> int:
    from grandgousier.archive.stage1_adapters import (
        build_archive_source_runtime,
        build_product_store,
    )
    from grandgousier.archive.stage1_contract import ArchiveRequest
    from grandgousier.archive.stage1_planner import (
        iter_stage1_plan_batches,
        normalize_stage1_batch_size,
        write_stage1_plan_batches,
    )
    from grandgousier.config import read_config_file
    from grandgousier.exit_codes import OK, PRECONDITION, USAGE

    config_file = getattr(args, "config_file", None)
    if not config_file:
        print("stage1-plan requires --config with archives/products sections", file=sys.stderr)
        return USAGE
    try:
        payload = read_config_file(config_file)
        runtime = build_archive_source_runtime(payload, args.source)
        product_store = build_product_store(payload)
        request = ArchiveRequest(
            source_id=args.source,
            filters=tuple(args.stage1_filters or ()),
            datetime_start=args.datetime_start,
            datetime_end=args.datetime_end,
            object_name=args.stage1_object_name,
            ra_deg=args.ra,
            dec_deg=args.dec,
            radius_deg=args.radius_deg,
            limit=args.limit,
            native_observation_ids=tuple(args.native_observation_id),
        )

        source_config = payload.get("archives", {}).get(args.source, {})
        configured_batch_size = (
            source_config.get("stage1_batch_size") if isinstance(source_config, dict) else None
        )
        if configured_batch_size is None:
            configured_batch_size = payload.get("stage1_batch_size")
        try:
            batch_size = normalize_stage1_batch_size(configured_batch_size)
        except ValueError as exc:
            raise ValueError(
                f"{exc}; configure archives.{args.source}.stage1_batch_size or "
                "top-level stage1_batch_size"
            ) from exc
        batches = iter_stage1_plan_batches(
            request=request,
            source=runtime.descriptor,
            metadata_provider=runtime.metadata_provider,
            product_store=product_store,
            batch_size=batch_size,
        )

        json_target = Path(args.out)
        csv_target = Path(args.csv_out or json_target.with_suffix(".csv"))

        def emit_blocked_diagnostic(item) -> None:
            if item.status == "READY":
                return
            observation = item.observation
            label = (
                observation.native_observation_id
                or observation.metadata.get("raw_id")
                or "?"
            )
            sys.stderr.write(f"  BLOCKED {label}: {'; '.join(item.issues)}\n")

        summary, json_path, csv_path = write_stage1_plan_batches(
            batches,
            source=runtime.descriptor,
            request=request,
            product_store=product_store.public_descriptor,
            json_path=json_target,
            csv_path=csv_target,
            on_item=emit_blocked_diagnostic if args.format != "json" else None,
        )
    except Exception as exc:
        print(f"Stage 1 planning failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return USAGE

    if args.format == "json":
        _stream_text_file_to_stdout(json_path)
    else:
        print(
            "Stage 1 plan: "
            f"status={summary.status}, source={args.source}, "
            f"discovered={summary.discovered_records}, science={summary.science_items}, "
            f"ready={summary.ready_items}, blocked={summary.blocked_items}"
        )
        print(f"Plan JSON: {json_path}")
        print(f"Plan CSV: {csv_path}")
        print("Plan-only: no source assets were materialized and no database writes were performed.")

    return OK if summary.status in {"READY", "EMPTY"} else PRECONDITION


def _stream_text_file_to_stdout(path: str | Path, *, chunk_size: int = 64 * 1024) -> None:
    with Path(path).open("r", encoding="utf-8") as handle:
        while chunk := handle.read(chunk_size):
            sys.stdout.write(chunk)


def cmd_stage1_execute(args: argparse.Namespace) -> int:
    from grandgousier.archive.stage1_adapters import (
        build_archive_source_runtime,
        build_product_store,
    )
    from grandgousier.archive.stage1_contract import ArchiveRequest
    from grandgousier.archive.stage1_execution import (
        Stage1StopRequested,
        execute_stage1_stream,
    )
    from grandgousier.archive.stage1_checkpoint import (
        Stage1Checkpoint,
        execution_fingerprint,
    )
    from grandgousier.archive.stage1_planner import normalize_stage1_batch_size
    from grandgousier.config import read_config_file
    from grandgousier.exit_codes import OK, PRECONDITION, PROCESSING, USAGE

    config_file = getattr(args, "config_file", None)
    if not config_file:
        print("stage1-execute requires --config with archives/products sections", file=sys.stderr)
        return USAGE
    try:
        payload = read_config_file(config_file)
        runtime = build_archive_source_runtime(payload, args.source)
        product_store = build_product_store(payload)
        request = ArchiveRequest(
            source_id=args.source,
            filters=tuple(args.stage1_filters or ()),
            datetime_start=args.datetime_start,
            datetime_end=args.datetime_end,
            object_name=args.stage1_object_name,
            ra_deg=args.ra,
            dec_deg=args.dec,
            radius_deg=args.radius_deg,
            limit=args.limit,
            native_observation_ids=tuple(args.native_observation_id),
        )
        source_config = payload.get("archives", {}).get(args.source, {})
        configured_batch_size = args.stage1_batch_size
        if configured_batch_size is None and isinstance(source_config, dict):
            configured_batch_size = source_config.get("stage1_batch_size")
        if configured_batch_size is None:
            configured_batch_size = payload.get("stage1_batch_size")
        batch_size = normalize_stage1_batch_size(configured_batch_size)
        plan_path = Path(args.plan_out)
        csv_path = plan_path.with_suffix(".csv")
        report_path = Path(args.report_out)
        report_csv_path = Path(args.csv_out) if args.csv_out else report_path.with_suffix(".csv")
        checkpoint_path = (
            Path(args.checkpoint)
            if args.checkpoint
            else report_path.with_suffix(".checkpoint.sqlite3")
        )
        run_fingerprint = execution_fingerprint(
            source=runtime.descriptor.to_dict(),
            request=request.to_dict(),
            product_store=product_store.public_descriptor,
            batch_size=batch_size,
            overwrite=bool(args.overwrite),
            max_dark_temperature_delta_c=args.max_dark_temperature_delta_c,
            software_version=__version__,
        )
        checkpoint = Stage1Checkpoint.open(
            checkpoint_path,
            source_id=args.source,
            config_fingerprint=run_fingerprint,
            resume=bool(args.resume),
            restart=bool(args.restart_run),
        )
        try:
            summary = execute_stage1_stream(
                request=request,
                source=runtime.descriptor,
                metadata_provider=runtime.metadata_provider,
                asset_provider=runtime.asset_provider,
                product_store=product_store,
                batch_size=batch_size,
                plan_json_path=plan_path,
                report_json_path=report_path,
                plan_csv_path=csv_path,
                report_csv_path=report_csv_path,
                overwrite=bool(args.overwrite),
                max_dark_temperature_delta_c=args.max_dark_temperature_delta_c,
                checkpoint=checkpoint,
                run_fingerprint=run_fingerprint,
                stop_on_first_failure=bool(args.stop_on_first_failure),
            )
        finally:
            close_provider = getattr(runtime.asset_provider, "close", None)
            if callable(close_provider):
                close_provider()
            checkpoint.close()
    except (KeyboardInterrupt, Stage1StopRequested):
        print("Stage 1 execution interrupted; resume with --resume", file=sys.stderr)
        return PROCESSING
    except Exception as exc:
        print(f"Stage 1 execution failed before item processing: {type(exc).__name__}: {exc}", file=sys.stderr)
        return USAGE

    if args.format == "json":
        print(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2))
    else:
        report_summary = summary.to_dict()["summary"]
        print(
            "Stage 1 execution: "
            f"status={summary.status}, source={args.source}, items={report_summary['items']}, "
            f"successful={report_summary['successful_items']}, blocked={report_summary['blocked_items']}, "
            f"failed={report_summary['failed_items']}, reduced={report_summary['reduced']}, "
            f"imported={report_summary['imported']}, skipped_existing={report_summary['skipped_existing']}, "
            f"adopted_existing={report_summary['adopted_existing']}, batch_size={batch_size}"
        )
        print(f"Executed plan: {plan_path}")
        print(f"Execution JSON: {report_path}")
        print(f"Plan CSV: {csv_path}")
        print(f"Execution CSV: {report_csv_path}")
        print("Stage 1 execution does not write Grandgousier DB state or invoke Stage 2.")

    if summary.status == "READY":
        return OK
    if summary.status == "BLOCKED":
        return PRECONDITION
    return PROCESSING


def cmd_stage1_handoff(args: argparse.Namespace) -> int:
    from grandgousier.archive.stage1_handoff import (
        handoff_stage1_execution,
        write_stage1_handoff_json,
    )
    from grandgousier.config import field_archive_mapping, read_config_file
    from grandgousier.exit_codes import OK, PRECONDITION, PROCESSING, USAGE

    if args.persist_only and args.stage2_manifest_out:
        print("--stage2-manifest-out is unavailable with --persist-only", file=sys.stderr)
        return USAGE

    field_config: dict = {}
    config_file = getattr(args, "config_file", None)
    try:
        if config_file:
            field_config = field_archive_mapping(read_config_file(config_file))
        catalog_name = args.catalog_name or field_config.get("catalog_name")
        instrument_profile = args.instrument_profile or field_config.get("instrument_profile") or "auto"
        out_root = args.out_root or field_config.get("out_root") or "exports"
        Session = make_session_factory(args.database_url)
        with Session() as session:
            report = handoff_stage1_execution(
                session,
                execution_report_path=args.execution_report,
                execute=bool(args.execute),
                persist_only=bool(args.persist_only),
                catalog_name=catalog_name,
                instrument_profile=instrument_profile,
                master_profile=args.master_profile,
                robophot_profile=args.robophot_profile,
                min_frames_per_series=args.min_frames_per_series,
                ensemble_min_ref_stars_per_frame=args.ensemble_min_ref_stars_per_frame,
                max_field_separation_arcmin=args.max_field_separation_arcmin,
                max_series_gap_minutes=args.max_series_gap_minutes,
                out_root=out_root,
                log_dir=args.log_dir,
                manifest_out=args.stage2_manifest_out,
                stage2_profile=args.stage2_profile,
            )
        report_path = write_stage1_handoff_json(report, args.report_out)
    except Exception as exc:
        print(f"Stage 1 handoff failed before Stage-2 invocation: {type(exc).__name__}: {exc}", file=sys.stderr)
        return USAGE

    if args.format == "json":
        print(report.to_json(indent=2))
    else:
        summary = report.to_dict()["summary"]
        print(
            "Stage 1 handoff: "
            f"status={report.status}, execute={bool(args.execute)}, "
            f"persist_only={bool(args.persist_only)}, items={summary['items']}, "
            f"selected_for_stage2={summary['selected_for_stage2']}, "
            f"persisted_identity_only={summary['persisted_identity_only']}, "
            f"already_represented={summary['already_represented']}, "
            f"blocked={summary['blocked']}, stage2_invoked={summary['stage2_invoked']}"
        )
        for item in report.items:
            label = item.product.native_observation_id or item.product.observation_uid or "?"
            print(f"  {item.status}: {label} [{item.p4_action}] -> {item.product.product_path}")
            if item.reason:
                print(f"    {item.reason}")
        if report.stage2_result is not None:
            stage2 = report.stage2_result
            print(
                "Stage 2: "
                f"status={stage2.get('status')}, discovered={stage2.get('discovered_files')}, "
                f"indexed={len(stage2.get('indexed_frame_ids') or [])}, "
                f"created_series={len(stage2.get('created_series_ids') or [])}, "
                f"processed_series={len(stage2.get('processed_series_ids') or [])}"
            )
        for error in report.errors:
            print(f"  ERROR: {error}", file=sys.stderr)
        print(f"Handoff JSON: {report_path}")
        if args.persist_only:
            print("Persistence-only mode: Stage 2 was not invoked.")
        elif not args.execute:
            print("Plan-only: no Stage-2 DB writes were performed.")
        elif summary["selected_for_stage2"] == 0:
            print("No new physical observations were selected; Stage 2 was not invoked and no DB writes were needed.")

    if report.status == "READY":
        return OK
    if report.status == "BLOCKED":
        return PRECONDITION
    return PROCESSING


def cmd_probe_master_physical_archive(args: argparse.Namespace) -> int:
    from grandgousier.archive.master_physical_archive import probe_master_physical_archive
    from grandgousier.exit_codes import OK, PRECONDITION, USAGE

    try:
        report = probe_master_physical_archive(
            args.imdata_root,
            max_files_scanned=args.max_files,
        )
    except (OSError, ValueError) as exc:
        print(f"MASTER physical archive probe failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return USAGE

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return OK if report["status"] == "PHYSICAL_INVENTORY_COMPLETE" else PRECONDITION


def cmd_index_archive(args: argparse.Namespace) -> int:
    from grandgousier.instruments.base import InstrumentProfile
    from grandgousier.io.archive_indexer import index_archive

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



def cmd_master_archive_reduce(args: argparse.Namespace) -> int:
    from grandgousier.archive.manifest import read_archive_manifest_csv
    from grandgousier.archive.master_reduction import (
        attach_raw_cleanup,
        build_master_reduction_tasks,
        compact_existing_master_output,
        reduce_master_task,
        write_master_reduction_report,
    )

    if args.overwrite and args.compact_existing:
        print("MASTER archive reduction planning failed: --overwrite and --compact-existing are mutually exclusive", file=sys.stderr)
        return 2
    try:
        rows = read_archive_manifest_csv(args.manifest)
        output_root = Path(args.out_root) if args.out_root else Path(args.cache_root) / "processed"
        tasks = build_master_reduction_tasks(
            rows,
            cache_root=args.cache_root,
            output_root=output_root,
            limit=args.limit,
        )
    except Exception as exc:
        print(f"MASTER archive reduction planning failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    print(
        "MASTER archive reduction plan: "
        f"manifest_rows={len(rows)}, unique_science={len(tasks)}, cache_root={Path(args.cache_root)}, "
        f"out_root={output_root}, compact_existing={bool(args.compact_existing)}, "
        f"delete_raw_after_success={bool(args.delete_raw_after_success)}"
    )
    plan_rows = [
        {
            "raw_id": task.raw_id,
            "raw": str(task.raw_local_path),
            "dark": str(task.dark_local_path),
            "flat": str(task.flat_local_path),
            "header": str(task.header_local_path),
            "output": str(task.output_path),
        }
        for task in tasks
    ]
    if args.format == "json":
        _print_rows(plan_rows, ["raw_id", "raw", "dark", "flat", "header", "output"], "json")
    else:
        _print_rows(plan_rows[:50], ["raw_id", "raw", "dark", "flat", "header", "output"], "table")
        if len(plan_rows) > 50:
            print(f"... {len(plan_rows) - 50} additional science frames omitted from console plan")

    if not args.execute:
        print("Plan-only: no processed FITS written. Add --execute to run Stage 1B reduction.")
        return 0

    results = []
    raw_freed_bytes = 0
    for index, task in enumerate(tasks, start=1):
        if args.compact_existing and task.output_path.exists() and task.output_path.stat().st_size > 0:
            result = compact_existing_master_output(task)
        else:
            result = reduce_master_task(
                task,
                overwrite=args.overwrite,
                max_dark_temperature_delta_c=args.max_dark_temperature_delta_c,
            )
        if args.delete_raw_after_success:
            result = attach_raw_cleanup(result)
            raw_freed_bytes += int(result.raw_deleted_bytes or 0)
        results.append(result)
        detail = ""
        if result.status in {"REDUCED", "SKIPPED_EXISTING", "COMPACTED_EXISTING"} and result.calibrated_median_adu is not None:
            detail = (
                f" median={result.calibrated_median_adu:.3f} ADU"
                f" invalid_flat={result.n_invalid_flat} saturated={result.n_saturated}"
            )
        elif result.status == "SKIPPED_EXISTING":
            detail = " metrics=legacy_missing (run once with --overwrite to stamp 0.5.42.2 metrics)"
        if result.raw_deleted:
            detail += f" raw_deleted={int(result.raw_deleted_bytes or 0)}B"
        print(f"[{index}/{len(tasks)}] {result.status}: raw_id={task.raw_id} -> {task.output_path}{detail}")
        if result.error:
            print(f"  {result.error}", file=sys.stderr)

    report_path = Path(args.report_out) if args.report_out else output_root / "master_reduction_report.csv"
    write_master_reduction_report(results, report_path)
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    print(
        "MASTER archive reduction result: "
        f"science={len(results)}, reduced={counts.get('REDUCED', 0)}, "
        f"compacted_existing={counts.get('COMPACTED_EXISTING', 0)}, "
        f"skipped_existing={counts.get('SKIPPED_EXISTING', 0)}, failed={counts.get('FAILED', 0)}, "
        f"raw_deleted={sum(1 for r in results if r.raw_deleted)}, raw_freed_gib={raw_freed_bytes / (1024**3):.3f}, "
        f"report={report_path}"
    )
    return 0 if counts.get("FAILED", 0) == 0 else 2


def cmd_master_archive_prune(args: argparse.Namespace) -> int:
    from grandgousier.archive.manifest import read_archive_manifest_csv
    from grandgousier.archive.master_reduction import build_master_reduction_tasks
    from grandgousier.archive.master_storage import (
        execute_processed_prune,
        execute_raw_prune,
        plan_processed_prune,
        plan_raw_prune,
        write_master_prune_report,
    )

    if args.execute and not args.yes:
        print("MASTER archive prune refused: --execute requires --yes", file=sys.stderr)
        return 2
    try:
        rows = read_archive_manifest_csv(args.manifest)
        output_root = Path(args.out_root) if args.out_root else Path(args.cache_root) / "processed"
        tasks = build_master_reduction_tasks(
            rows,
            cache_root=args.cache_root,
            output_root=output_root,
            limit=args.limit,
        )
    except Exception as exc:
        print(f"MASTER archive prune planning failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    results = []
    if args.scope in {"raw", "all"}:
        try:
            raw_results = execute_raw_prune(tasks) if args.execute else plan_raw_prune(tasks)
        except Exception as exc:
            print(f"MASTER raw prune failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        results.extend(raw_results)

    if args.scope in {"processed", "all"}:
        Session = make_session_factory(args.database_url)
        try:
            with Session() as session:
                processed_results = (
                    execute_processed_prune(tasks, session=session)
                    if args.execute
                    else plan_processed_prune(tasks, session=session)
                )
        except Exception as exc:
            print(f"MASTER processed prune failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        results.extend(processed_results)

    report_path = Path(args.report_out) if args.report_out else Path(args.cache_root) / "master_prune_report.csv"
    write_master_prune_report(results, report_path)
    counts: dict[str, int] = {}
    bytes_freed = 0
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
        if result.status == "DELETED":
            bytes_freed += int(result.bytes_freed or 0)

    preview = [
        {
            "raw_id": item.raw_id,
            "scope": item.scope,
            "status": item.status,
            "bytes": item.bytes_freed,
            "frame_id": item.frame_id,
            "path": str(item.path),
            "reason": item.reason or "",
        }
        for item in results
    ]
    _print_rows(preview[:50], ["raw_id", "scope", "status", "bytes", "frame_id", "path", "reason"], args.format)
    if len(preview) > 50 and args.format != "json":
        print(f"... {len(preview) - 50} additional prune rows omitted from console")

    eligible_or_deleted = counts.get("ELIGIBLE", 0) + counts.get("DELETED", 0)
    blocked = counts.get("BLOCKED", 0) + counts.get("FAILED", 0)
    print(
        "MASTER archive prune result: "
        f"scope={args.scope}, tasks={len(tasks)}, eligible_or_deleted={eligible_or_deleted}, "
        f"already_missing={counts.get('ALREADY_MISSING', 0)}, blocked_or_failed={blocked}, "
        f"freed_gib={bytes_freed / (1024**3):.3f}, report={report_path}"
    )
    return 0 if blocked == 0 else 2


def cmd_check_wcs(args: argparse.Namespace) -> int:
    from grandgousier.astrometry.wcs_check import check_frames_wcs

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

        for row in session.execute(stmt).all():
            d = dict(row._mapping)
            d["ra_deg"] = _round_float(d.get("ra_deg"), 8)
            d["dec_deg"] = _round_float(d.get("dec_deg"), 8)
            rows.append(d)
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
            "n_transform_obj": result.ensemble_transform_used_objects,
            "n_transform_meas": result.ensemble_transform_used_measurements,
            "n_transform_clipped": result.ensemble_transform_clipped_objects,
            "color0": _round_float(result.color0, 5),
            "transform_offset": _round_float(result.ensemble_transform_offset, 6),
            "transform_color_term": _round_float(result.ensemble_transform_color_term, 6),
            "transform_rms": _round_float(result.ensemble_transform_residual_rms, 6),
            "transform_mad": _round_float(result.ensemble_transform_residual_mad, 6),
            "frame_diag_obj": result.n_used_objects,
            "frame_diag_frames": result.n_used_frames,
            "frame_diag_color_term": _round_float(result.frame_model_color_term, 6),
            "frame_diag_rms": _round_float(result.frame_model_residual_rms, 6),
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
            "n_transform_obj",
            "n_transform_meas",
            "n_transform_clipped",
            "color0",
            "transform_offset",
            "transform_color_term",
            "transform_rms",
            "transform_mad",
            "frame_diag_obj",
            "frame_diag_frames",
            "frame_diag_color_term",
            "frame_diag_rms",
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
            min_calibrator_objects=args.min_calibrator_objects,
            min_color_span=args.min_color_span,
            max_transform_residual_mad=(None if args.max_transform_residual_mad is not None and args.max_transform_residual_mad < 0 else args.max_transform_residual_mad),
            max_pivot_color_fraction=(None if args.max_pivot_color_fraction is not None and args.max_pivot_color_fraction < 0 else args.max_pivot_color_fraction),
            max_shape_metric_delta=args.max_shape_metric_delta,
            min_color_term_significance=args.min_color_term_significance,
            min_color_model_bic_improvement=args.min_color_model_bic_improvement,
            min_color_model_mad_improvement=args.min_color_model_mad_improvement,
            exclude_saturated=not args.include_saturated,
            limit=args.limit,
            dry_run=args.dry_run,
            diagnose_performance=args.diagnose_performance,
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
            "quality_gate": result.quality_gate_passed,
            "transform_model": result.selected_transform_model,
            "model_reason": result.model_selection_reason,
            "checked": result.checked_measurements,
            "calibrated": result.calibrated,
            "inserted_or_updated": result.inserted_or_updated,
            "deleted_existing": result.deleted_existing,
            "skipped_no_color": result.skipped_no_color,
            "skipped_no_zp": result.skipped_no_frame_zp,
            "color0": _round_float(result.color0, 5),
            "transform_offset": _round_float(result.transform_offset, 6),
            "color_term": _round_float(result.color_term, 6),
            "fitted_color_term": _round_float(result.fitted_color_term, 6),
            "term_significance": _round_float(result.color_term_significance, 3),
            "bic_improvement": _round_float(result.color_model_bic_improvement, 3),
            "transform_rms": _round_float(result.transform_residual_rms, 6),
            "transform_mad": _round_float(result.transform_residual_mad, 6),
            "pivot_fraction": _round_float(result.pivot_color_fraction, 5),
            "sys_err_median": _round_float(result.systematic_error_median, 6),
            "max_rms_delta": _round_float(result.max_abs_rms_delta, 12),
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
            "quality_gate",
            "transform_model",
            "model_reason",
            "checked",
            "calibrated",
            "inserted_or_updated",
            "deleted_existing",
            "skipped_no_color",
            "skipped_no_zp",
            "color0",
            "transform_offset",
            "color_term",
            "fitted_color_term",
            "term_significance",
            "bic_improvement",
            "transform_rms",
            "transform_mad",
            "pivot_fraction",
            "sys_err_median",
            "max_rms_delta",
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
            photometry_estimator=args.photometry_estimator,
            adaptive_min_psf_snr=args.adaptive_min_psf_snr,
            adaptive_max_psf_sources_per_frame=args.adaptive_max_psf_sources_per_frame,
            adaptive_psf_cutout_radius_pix=args.adaptive_psf_cutout_radius_pix,
            adaptive_psf_min_isolation_pix=args.adaptive_psf_min_isolation_pix,
            adaptive_interpolation_neighbors=args.adaptive_interpolation_neighbors,
            adaptive_min_psf_references=args.adaptive_min_psf_references,
            adaptive_aperture_fwhm_factor=args.adaptive_aperture_fwhm_factor,
            adaptive_max_axis_ratio=args.adaptive_max_axis_ratio,
            adaptive_max_radius_scale=args.adaptive_max_radius_scale,
            adaptive_fallback_to_circular=args.adaptive_fallback_to_circular,
            psf_profile=args.psf_profile,
            psf_moffat_beta=args.psf_moffat_beta,
            psf_fit_radius_fwhm=args.psf_fit_radius_fwhm,
            psf_min_fit_radius_pix=args.psf_min_fit_radius_pix,
            psf_max_fit_radius_pix=args.psf_max_fit_radius_pix,
            psf_centroid_search_radius_pix=args.psf_centroid_search_radius_pix,
            psf_centroid_grid_steps=args.psf_centroid_grid_steps,
            psf_residual_clip_sigma=args.psf_residual_clip_sigma,
            psf_residual_clip_iterations=args.psf_residual_clip_iterations,
            psf_fallback_to_adaptive=args.psf_fallback_to_adaptive,
            empirical_psf_grid_size=args.empirical_psf_grid_size,
            empirical_psf_stamp_radius_pix=args.empirical_psf_stamp_radius_pix,
            empirical_psf_reference_radius_pix=args.empirical_psf_reference_radius_pix,
            empirical_psf_references_per_node=args.empirical_psf_references_per_node,
            empirical_psf_min_references_per_node=args.empirical_psf_min_references_per_node,
            empirical_psf_fallback_to_adaptive=args.empirical_psf_fallback_to_adaptive,
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
            random_error_model=args.random_error_model,
            gain_e_per_adu=args.gain_e_per_adu,
            read_noise_e=args.read_noise_e,
            background_estimator_variance_factor=(
                args.background_estimator_variance_factor
            ),
            empirical_background_floor=args.empirical_background_floor,
            fractional_flux_floor=args.fractional_flux_floor,
            scintillation_enabled=args.scintillation_enabled,
            scintillation_coefficient=args.scintillation_coefficient,
            telescope_aperture_m=args.telescope_aperture_m,
            site_altitude_m=args.site_altitude_m,
            diagnostics_out_dir=args.diagnostics_out_dir,
            diagnose_performance=args.diagnose_performance,
        )
    print(
        "Forced photometry result: "
        f"selected_frames={result.selected_frames}, checked_frames={result.checked_frames}, "
        f"selected_links={result.selected_links}, measured={result.measured}, "
        f"inserted_or_updated={result.inserted_or_updated}, insert_batches={result.insert_batches}, "
        f"skipped_missing_file={result.skipped_missing_file}, failed_frames={result.failed_frames}, "
        f"failed_measurements={result.failed_measurements}, "
        f"calibration_version_id={result.calibration_version_id}, "
        f"calibration_name={result.calibration_name}, random_error_model={result.random_error_model}, "
        f"physical_to_legacy_ratio_median={result.physical_to_legacy_mag_err_ratio_median}, "
        f"dry_run={result.dry_run}"
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


def cmd_candidate_evidence_report(args: argparse.Namespace) -> int:
    from grandgousier.quality.candidate_evidence import (
        CANDIDATE_EVIDENCE_COLUMNS,
        build_candidate_evidence_report,
        export_candidate_evidence_report,
    )

    report = build_candidate_evidence_report(
        args.series_dir,
        max_time_separation_sec=args.max_time_separation_sec,
        min_pairs=args.min_pairs,
        min_correlation=args.min_correlation,
        max_fdr_q=args.max_fdr_q,
        min_repeat_series=args.min_repeat_series,
        target_source_ids=args.target_source_id,
    )
    outputs = export_candidate_evidence_report(report, out_prefix=args.out_prefix)
    if args.format == "json":
        print(json.dumps({"outputs": outputs, **report.to_dict()}, ensure_ascii=False, indent=2))
        return 0

    rows = [dict(row) for row in report.rows[: int(args.limit) if args.limit is not None else None]]
    for row in rows:
        for key, nd in (
            ("best_pair_pearson_r", 4),
            ("best_pair_pearson_p", 5),
            ("best_pair_pearson_fdr_q", 5),
            ("best_pair_spearman_rho", 4),
            ("best_pair_spearman_p", 5),
            ("best_pair_spearman_fdr_q", 5),
            ("best_pair_max_time_separation_sec", 3),
            ("same_filter_max_median_delta", 5),
            ("all_filter_median_range", 5),
            ("median_mag_range", 5),
        ):
            row[key] = _round_float(row.get(key), nd)
    if args.format == "table":
        summary = report.to_dict()["summary"]
        print(
            "Candidate evidence report: "
            f"series={summary['series_count']}, sources={summary['candidate_sources']}, "
            f"multi_filter_coherent={summary['multi_filter_coherent_sources']}, "
            f"repeated_accepted={summary['repeated_accepted_sources']}"
        )
        print(
            "Outputs: "
            f"json={outputs['json']}, candidates_csv={outputs['candidates_csv']}, "
            f"pairs_csv={outputs['pairs_csv']}"
        )
    _print_rows(rows, list(CANDIDATE_EVIDENCE_COLUMNS), args.format)
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
            finalize_tail=(
                True if getattr(args, "finalize_tail", None) is None else args.finalize_tail
            ),
            dry_run=args.dry_run,
        )
    if args.format == "json":
        print(
            json.dumps(
                {
                    "summary": result.summary(),
                    "observation_selection": result.observation_selection_rows,
                    "series": result.rows,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(
            "Field/series build result: "
            f"selected_frames={result.selected_frames}, created_fields={result.created_fields}, "
            f"created_series={result.created_series}, updated_series={result.updated_series}, "
            f"open_series_ids={result.open_series_ids}, linked_frames={result.linked_frames}, "
            f"dry_run={result.dry_run}"
        )
        if result.observation_selection:
            print(f"Observation science-input guard: {result.observation_selection}")
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
        photometry_estimator=args.photometry_estimator,
        adaptive_min_psf_snr=args.adaptive_min_psf_snr,
        adaptive_max_psf_sources_per_frame=args.adaptive_max_psf_sources_per_frame,
        adaptive_psf_cutout_radius_pix=args.adaptive_psf_cutout_radius_pix,
        adaptive_psf_min_isolation_pix=args.adaptive_psf_min_isolation_pix,
        adaptive_interpolation_neighbors=args.adaptive_interpolation_neighbors,
        adaptive_min_psf_references=args.adaptive_min_psf_references,
        adaptive_aperture_fwhm_factor=args.adaptive_aperture_fwhm_factor,
        adaptive_max_axis_ratio=args.adaptive_max_axis_ratio,
        adaptive_max_radius_scale=args.adaptive_max_radius_scale,
        adaptive_fallback_to_circular=args.adaptive_fallback_to_circular,
        psf_profile=args.psf_profile,
        psf_moffat_beta=args.psf_moffat_beta,
        psf_fit_radius_fwhm=args.psf_fit_radius_fwhm,
        psf_min_fit_radius_pix=args.psf_min_fit_radius_pix,
        psf_max_fit_radius_pix=args.psf_max_fit_radius_pix,
        psf_centroid_search_radius_pix=args.psf_centroid_search_radius_pix,
        psf_centroid_grid_steps=args.psf_centroid_grid_steps,
        psf_residual_clip_sigma=args.psf_residual_clip_sigma,
        psf_residual_clip_iterations=args.psf_residual_clip_iterations,
        psf_fallback_to_adaptive=args.psf_fallback_to_adaptive,
        empirical_psf_grid_size=args.empirical_psf_grid_size,
        empirical_psf_stamp_radius_pix=args.empirical_psf_stamp_radius_pix,
        empirical_psf_reference_radius_pix=args.empirical_psf_reference_radius_pix,
        empirical_psf_references_per_node=args.empirical_psf_references_per_node,
        empirical_psf_min_references_per_node=args.empirical_psf_min_references_per_node,
        empirical_psf_fallback_to_adaptive=args.empirical_psf_fallback_to_adaptive,
        annulus_inner_pix=args.annulus_inner_pix,
        annulus_outer_pix=args.annulus_outer_pix,
        aperture_correction_reference_radius_pix=args.aperture_correction_reference_radius_pix,
        aperture_correction_max_calibrator_mag_err=_none_if_negative(args.aperture_correction_max_calibrator_mag_err),
        aperture_correction_min_calibrators_per_frame=args.aperture_correction_min_calibrators_per_frame,
        aperture_correction_min_plane_calibrators=args.aperture_correction_min_plane_calibrators,
        aperture_correction_clip_sigma=args.aperture_correction_clip_sigma,
        aperture_correction_n_iterations=args.aperture_correction_n_iterations,
        aperture_correction_min_plane_bic_improvement=args.aperture_correction_min_plane_bic_improvement,
        aperture_correction_min_spatial_peak_to_peak=args.aperture_correction_min_spatial_peak_to_peak,
        aperture_correction_plane_policy=args.aperture_correction_plane_policy,
        aperture_correction_max_residual_mad=_none_if_negative(args.aperture_correction_max_residual_mad),
        aperture_correction_max_abs_correction=args.aperture_correction_max_abs_correction,
        aperture_correction_max_abs_spatial_deviation=(
            args.aperture_correction_max_abs_spatial_deviation
        ),
        aperture_correction_max_abs_total_correction=(
            args.aperture_correction_max_abs_total_correction
        ),
        aperture_correction_min_good_frame_fraction=args.aperture_correction_min_good_frame_fraction,
        aperture_correction_max_global_median_residual_mad=_none_if_negative(args.aperture_correction_max_global_median_residual_mad),
        detection_threshold_sigma=args.detection_threshold_sigma,
        detection_max_sources_per_frame=args.detection_max_sources_per_frame,
        detection_min_separation_pix=args.detection_min_separation_pix,
        detection_match_radius_arcsec=args.detection_match_radius_arcsec,
        enable_psf_field_diagnostics=args.enable_psf_field_diagnostics,
        psf_diagnostics_max_sources_per_frame=args.psf_diagnostics_max_sources_per_frame,
        psf_diagnostics_min_snr=args.psf_diagnostics_min_snr,
        psf_diagnostics_cutout_radius_pix=args.psf_diagnostics_cutout_radius_pix,
        psf_diagnostics_min_isolation_pix=args.psf_diagnostics_min_isolation_pix,
        psf_diagnostics_clip_sigma=args.psf_diagnostics_clip_sigma,
        psf_diagnostics_n_iterations=args.psf_diagnostics_n_iterations,
        psf_diagnostics_render_plots=args.psf_diagnostics_render_plots,
        bulk_insert_batch_size=args.bulk_insert_batch_size,
        mag_key=args.mag_key,
        aperture_zp_min_ref_stars_per_frame=args.aperture_zp_min_ref_stars_per_frame,
        aperture_zp_max_mag_err=_none_if_negative(args.aperture_zp_max_mag_err),
        aperture_zp_clip_sigma=args.aperture_zp_clip_sigma,
        aperture_zp_n_iterations=args.aperture_zp_n_iterations,
        ensemble_min_measurements=args.ensemble_min_measurements,
        ensemble_min_ref_measurements=args.ensemble_min_ref_measurements,
        ensemble_min_ref_stars_per_frame=args.ensemble_min_ref_stars_per_frame,
        enable_spatial_ensemble=args.enable_spatial_ensemble,
        enable_photometric_frame_qc=args.enable_photometric_frame_qc,
        spatial_ensemble_max_order=args.spatial_ensemble_max_order,
        spatial_ensemble_min_ref_stars_per_frame=args.spatial_ensemble_min_ref_stars_per_frame,
        spatial_ensemble_clip_sigma=args.spatial_ensemble_clip_sigma,
        spatial_ensemble_n_iterations=args.spatial_ensemble_n_iterations,
        spatial_ensemble_min_bic_improvement=args.spatial_ensemble_min_bic_improvement,
        spatial_ensemble_min_spatial_peak_to_peak=args.spatial_ensemble_min_spatial_peak_to_peak,
        spatial_ensemble_max_residual_mad=_none_if_negative(args.spatial_ensemble_max_residual_mad),
        spatial_ensemble_max_residual_mad_degradation=_none_if_negative(args.spatial_ensemble_max_residual_mad_degradation),
        spatial_ensemble_max_abs_spatial_deviation=args.spatial_ensemble_max_abs_spatial_deviation,
        spatial_ensemble_min_good_frame_fraction=args.spatial_ensemble_min_good_frame_fraction,
        ensemble_max_ref_rms=_none_if_negative(args.ensemble_max_ref_rms),
        ensemble_max_ref_mad=_none_if_negative(args.ensemble_max_ref_mad),
        ensemble_n_iterations=args.ensemble_n_iterations,
        enable_color_calibration=args.enable_color_calibration,
        color_identity_catalog_name=args.color_identity_catalog_name,
        color_reference_catalog_name=args.color_reference_catalog_name,
        color_reference_band=args.color_reference_band,
        color_index=args.color_index,
        color_target_calibration_name=args.color_target_calibration_name,
        color_min_measurements_per_object=args.color_min_measurements_per_object,
        color_min_stars_per_frame=args.color_min_stars_per_frame,
        color_max_measurement_mag_err=_none_if_negative(args.color_max_measurement_mag_err),
        color_max_reference_mag_err=_none_if_negative(args.color_max_reference_mag_err),
        color_clip_sigma=args.color_clip_sigma,
        color_n_iterations=args.color_n_iterations,
        color_min_calibrator_objects=args.color_min_calibrator_objects,
        color_min_span=args.color_min_span,
        color_max_transform_residual_mad=_none_if_negative(args.color_max_transform_residual_mad),
        color_max_pivot_fraction=_none_if_negative(args.color_max_pivot_fraction),
        color_max_shape_metric_delta=args.color_max_shape_metric_delta,
        color_min_term_significance=args.color_min_term_significance,
        color_min_bic_improvement=args.color_min_bic_improvement,
        color_min_mad_improvement=args.color_min_mad_improvement,
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
        skip_aperture_correction=args.skip_aperture_correction,
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
        candidate_ensemble_growth_policy=args.candidate_ensemble_growth_policy,
        replace_detections=args.replace_detections,
        replace_detection_matches=args.replace_detection_matches,
        materialize_intermediate_calibrations=False,
        science_materialization_diagnostics=False,
        forced_photometry_diagnostics=args.diagnose_forced_photometry,
        calibration_performance_diagnostics=args.diagnose_calibration_performance,
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
            f"ensemble={result.calibration_names.get('ensemble')}, "
            f"science={result.calibration_names.get('science') or result.calibration_names.get('ensemble')}"
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



def cmd_config_example(args: argparse.Namespace) -> int:
    import shutil

    from grandgousier.exit_codes import OK, PRECONDITION
    from grandgousier.resources import application_config_example_path

    destination = Path(args.out)
    if destination.exists() and not args.force:
        print(f"Refusing to overwrite existing config without --force: {destination}", file=sys.stderr)
        return PRECONDITION
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(application_config_example_path(), destination)
    print(f"Grandgousier config template written: {destination}")
    return OK


def cmd_doctor(args: argparse.Namespace) -> int:
    from grandgousier.exit_codes import DATABASE, OK, PRECONDITION
    from grandgousier.services.operations import run_doctor

    result = run_doctor(
        database_url=args.database_url,
        config_file=getattr(args, "config_file", None),
        processed_root=args.processed_root,
        out_root=args.out_root,
        instrument_profile=args.instrument_profile,
        strict_optional=bool(args.strict_optional),
    )
    if args.format == "json":
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str))
    else:
        print(f"Grandgousier doctor: {result.status}")
        _print_rows([row.to_dict() for row in result.checks], ["name", "status", "detail"], "table")
    if result.ready:
        return OK
    failed = {row.name for row in result.checks if row.status == "FAIL"}
    if failed & {"database", "schema_migrations"}:
        return DATABASE
    return PRECONDITION


def cmd_diagnose_run(args: argparse.Namespace) -> int:
    from grandgousier.exit_codes import OK, PRECONDITION
    from grandgousier.services.diagnostics import diagnose_run_bundle

    try:
        result = diagnose_run_bundle(args.path)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return PRECONDITION

    payload = result.to_dict()
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(
            "Grandgousier run diagnosis: "
            f"job_id={result.job_id}, status={result.job_status}, bundle={result.bundle_format}, "
            f"complete={result.complete}"
        )
        print(
            f"Diagnosis: {result.advice.code} [{result.advice.severity}] "
            f"{result.advice.summary}"
        )
        if result.advice.actions:
            print("Recommended actions:")
            for index, action in enumerate(result.advice.actions, start=1):
                print(f"  {index}. {action}")
        if result.artifacts_missing:
            print("Missing bundle artifacts: " + ", ".join(result.artifacts_missing))
    return OK


def cmd_operational_status(args: argparse.Namespace) -> int:
    from sqlalchemy.exc import SQLAlchemyError

    from grandgousier.config import field_archive_mapping, read_config_file
    from grandgousier.exit_codes import DATABASE, OK, PRECONDITION
    from grandgousier.pipeline.recipe_contracts import STAGE2_RECIPE_STAGE3_SCALE_V1
    from grandgousier.services.operations import build_field_status

    config_file = getattr(args, "config_file", None)
    field_config = {}
    if config_file is not None:
        try:
            field_config = field_archive_mapping(read_config_file(config_file))
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            print(str(exc), file=sys.stderr)
            return PRECONDITION

    field_id = args.field_id if args.field_id is not None else field_config.get("field_id")
    if field_id in {None, ""}:
        print("status requires --field-id or field_archive.field_id in --config", file=sys.stderr)
        return PRECONDITION
    catalog_name = args.catalog_name if args.catalog_name is not None else field_config.get("catalog_name")
    recipe = (
        args.required_stage2_recipe
        or field_config.get("stage2_recipe")
        or STAGE2_RECIPE_STAGE3_SCALE_V1
    )

    Session = make_session_factory(args.database_url)
    try:
        with Session() as session:
            result = build_field_status(
                session,
                field_id=int(field_id),
                catalog_name=(str(catalog_name) if catalog_name else None),
                required_stage2_recipe=str(recipe),
                min_points_per_object=args.min_points_per_object,
                min_common_objects=args.min_common_objects,
            )
    except SQLAlchemyError as exc:
        print(f"Database status query failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return DATABASE
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return PRECONDITION

    payload = result.to_dict()
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return OK if result.ready else PRECONDITION

    field = result.field
    stage2 = result.stage2
    print(
        "Grandgousier status: "
        f"field_id={field.get('field_id')}, field_name={field.get('field_name')}, status={result.status}"
    )
    print(
        "Stage 2: "
        f"recipe={stage2.get('required_recipe')}, series={stage2.get('n_series')}, "
        f"ready={stage2.get('n_ready_series')}, pairs={stage2.get('n_eligible_pairs')}, "
        f"largest_component={stage2.get('largest_component_size')}, plan={stage2.get('plan_status')}"
    )
    recipe_counts = stage2.get("recipe_compatibility_counts") or {}
    if recipe_counts:
        print("Stage-2 recipe compatibility: " + ", ".join(f"{k}={v}" for k, v in recipe_counts.items()))
    if result.stage3:
        _print_rows(
            list(result.stage3),
            [
                "filter_name", "instrument_unit_id", "interseries_generation_id", "verification_status",
                "reliability_status", "n_series", "n_edges", "graph_dof",
                "reduced_chi2", "source_scope_matches",
            ],
            "table",
        )
    else:
        print("Stage 3: no persisted generations")
    if args.verbose:
        rows = [
            {
                "series_id": row.get("series_id"),
                "filter_name": row.get("filter_name"),
                "instrument_unit_id": row.get("instrument_unit_id"),
                "status": row.get("status"),
                "science_calibration_id": row.get("calibration_version_id"),
                "recipe_compatibility": row.get("recipe_compatibility"),
                "recipe_notes": ";".join(row.get("recipe_compatibility_notes") or []),
            }
            for row in result.series
        ]
        _print_rows(
            rows,
            ["series_id", "filter_name", "instrument_unit_id", "status", "science_calibration_id", "recipe_compatibility", "recipe_notes"],
            "table",
        )
    return OK if result.ready else PRECONDITION



def cmd_acceptance_check(args: argparse.Namespace) -> int:
    from sqlalchemy.exc import SQLAlchemyError

    from grandgousier.exit_codes import DATABASE, OK, PRECONDITION, PROCESSING
    from grandgousier.services.acceptance import run_acceptance

    try:
        result = run_acceptance(
            database_url=args.database_url,
            dataset=args.dataset,
            config_file=getattr(args, "config_file", None),
            out_root=args.out_root,
        )
    except SQLAlchemyError as exc:
        print(f"Acceptance database query failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return DATABASE
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return PRECONDITION
    except Exception as exc:  # noqa: BLE001 - CLI converts scientific acceptance failures into an exit code.
        print(f"Acceptance processing failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return PROCESSING

    payload = result.to_dict()
    if args.report_out:
        report_path = Path(args.report_out)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"Grandgousier acceptance: {result.status} dataset={result.dataset}")
        _print_rows([row.to_dict() for row in result.checks], ["name", "status", "detail"], "table")
        if args.report_out:
            print(f"Report: {args.report_out}")
    return OK if result.ready else PROCESSING

def cmd_field_archive_readiness(args: argparse.Namespace) -> int:
    from grandgousier.services.readiness import check_runtime_readiness

    result = check_runtime_readiness(
        database_url=args.database_url,
        processed_root=args.processed_root,
        out_root=args.out_root,
        instrument_profile=args.instrument_profile,
    )
    payload = result.to_dict()
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"Field-archive readiness: {result.status}")
        _print_rows(
            [check.to_dict() for check in result.checks],
            ["name", "status", "detail"],
            "table",
        )
    return 0 if result.ready else 2


def cmd_process_field_archive(args: argparse.Namespace) -> int:
    from grandgousier.services.field_archive_job import run_field_archive_job
    from grandgousier.services.runtime import FieldArchiveJobConfig

    overrides = {
        "database_url": args.database_url,
        "processed_root": args.processed_root,
        "field_id": args.field_id,
        "catalog_name": args.catalog_name,
        "stage3_policies": args.stage3_policies,
        "control_object_ids": args.control_object_ids,
        "execute": args.execute,
        "resume": args.resume,
        "instrument_profile": args.instrument_profile,
        "stage2_recipe": args.stage2_recipe,
        "exclude_dates": args.exclude_dates,
        "include_dates": args.include_dates,
        "out_root": args.out_root,
        "plot_max_points": args.plot_max_points,
    }
    try:
        config = FieldArchiveJobConfig.from_sources(
            config_file=args.job_config or getattr(args, "config_file", None),
            overrides=overrides,
        )
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    job = run_field_archive_job(
        config,
        run_preflight=not bool(args.no_preflight),
    )
    workflow = job.workflow_result
    payload = job.to_dict()
    if args.format == "job-json":
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    elif args.format == "json":
        legacy_payload = workflow.to_dict() if workflow is not None else payload
        print(json.dumps(legacy_payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(
            "Field-archive job: "
            f"job_id={job.job_id}, status={job.status}, "
            f"config_fingerprint={job.config_fingerprint[:12]}"
        )
        if job.readiness is not None:
            print(f"Readiness: {job.readiness.status}")
        if workflow is not None:
            print(
                "M8 field archive: "
                f"field_id={workflow.field_id}, status={workflow.status}, execute={workflow.execute}, "
                f"batches={len(workflow.batches)}, stage2_runs={len(workflow.stage2_runs)}, "
                f"stage3_scopes={len(workflow.stage3)}, controls={len(workflow.control_objects)}"
            )
            if workflow.batches:
                batch_rows = [
                    {
                        "date": row.get("observation_date"),
                        "path": row.get("relative_path"),
                        "files": row.get("n_files"),
                        "db_status": row.get("db_status"),
                        "action": row.get("action"),
                        "series_ids": ",".join(str(v) for v in row.get("series_ids") or []),
                        "reason": row.get("reason"),
                    }
                    for row in workflow.batches
                ]
                _print_rows(batch_rows, ["date", "path", "files", "db_status", "action", "series_ids", "reason"], "table")
            for scope in workflow.stage3:
                generation = scope.get("generation") or {}
                verification = scope.get("verification") or {}
                analysis = scope.get("analysis") or {}
                graph = analysis.get("graph_solution") or {}
                policy = scope.get("policy") or {}
                print(
                    "M8 Stage 3: "
                    f"filter={policy.get('filter_name')}, generation_id={generation.get('interseries_generation_id')}, "
                    f"series={graph.get('n_series')}, edges={graph.get('n_edges')}, dof={graph.get('dof')}, "
                    f"reduced_chi2={graph.get('reduced_chi2')}, verify={verification.get('status')}"
                )
            for control in workflow.control_objects:
                print(
                    "M8 M5 control: "
                    f"object_id={control.get('object_id')}, status={control.get('status')}, "
                    f"source={control.get('rows_source_exact_m4')}, accepted={control.get('accepted_rows')}, "
                    f"rejected={control.get('rejected_rows')}"
                )
            print(f"Final report: {workflow.outputs.get('final_report_json')}")
            print(f"HTML report: {workflow.outputs.get('report_html')}")
            for warning in workflow.warnings:
                print(f"WARNING: {warning}")
            for error in workflow.errors:
                print(f"ERROR: {error}", file=sys.stderr)
        if job.error_message:
            print(f"ERROR: {job.error_type}: {job.error_message}", file=sys.stderr)
        print(f"Run bundle: {job.artifacts.get('job_dir')}")
        print(f"Run manifest: {job.artifacts.get('run_manifest_json')}")
        print(f"Run diagnosis: {job.artifacts.get('diagnosis_json')}")
        print(f"Job summary: {job.artifacts.get('job_summary_json')}")
        print(f"Job events: {job.artifacts.get('job_events_jsonl')}")
        if job.artifacts.get("failure_json"):
            print(f"Failure report: {job.artifacts.get('failure_json')}")
    return 0 if job.status in {"PLANNED", "READY", "READY_WITH_WARNINGS"} else 2


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

    from grandgousier.lightcurves.target import validate_target_coordinates

    try:
        validate_target_coordinates(args.target_ra_deg, args.target_dec_deg)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    Session = make_session_factory(args.database_url)
    with Session() as session:
        result = process_archive_path(
            session,
            root=args.path,
            catalog_name=args.catalog_name,
            execute=bool(args.execute),
            stage2_profile=getattr(args, "stage2_profile", "bulk"),
            instrument_profile=args.instrument_profile,
            master_profile=args.master_profile,
            robophot_profile=args.robophot_profile,
            recursive=args.recursive,
            extensions=_parse_extensions_arg(args.extensions),
            limit=args.limit,
            series_ids=args.series_ids,
            native_observation_ids=args.native_observation_ids,
            min_frames_per_series=args.min_frames_per_series,
            finalize_tail=(
                True if getattr(args, "finalize_tail", None) is None else args.finalize_tail
            ),
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
            photometry_estimator=args.photometry_estimator,
            adaptive_min_psf_snr=args.adaptive_min_psf_snr,
            adaptive_max_psf_sources_per_frame=args.adaptive_max_psf_sources_per_frame,
            adaptive_psf_cutout_radius_pix=args.adaptive_psf_cutout_radius_pix,
            adaptive_psf_min_isolation_pix=args.adaptive_psf_min_isolation_pix,
            adaptive_interpolation_neighbors=args.adaptive_interpolation_neighbors,
            adaptive_min_psf_references=args.adaptive_min_psf_references,
            adaptive_aperture_fwhm_factor=args.adaptive_aperture_fwhm_factor,
            adaptive_max_axis_ratio=args.adaptive_max_axis_ratio,
            adaptive_max_radius_scale=args.adaptive_max_radius_scale,
            adaptive_fallback_to_circular=args.adaptive_fallback_to_circular,
            psf_profile=args.psf_profile,
            psf_moffat_beta=args.psf_moffat_beta,
            psf_fit_radius_fwhm=args.psf_fit_radius_fwhm,
            psf_min_fit_radius_pix=args.psf_min_fit_radius_pix,
            psf_max_fit_radius_pix=args.psf_max_fit_radius_pix,
            psf_centroid_search_radius_pix=args.psf_centroid_search_radius_pix,
            psf_centroid_grid_steps=args.psf_centroid_grid_steps,
            psf_residual_clip_sigma=args.psf_residual_clip_sigma,
            psf_residual_clip_iterations=args.psf_residual_clip_iterations,
            psf_fallback_to_adaptive=args.psf_fallback_to_adaptive,
            empirical_psf_grid_size=args.empirical_psf_grid_size,
            empirical_psf_stamp_radius_pix=args.empirical_psf_stamp_radius_pix,
            empirical_psf_reference_radius_pix=args.empirical_psf_reference_radius_pix,
            empirical_psf_references_per_node=args.empirical_psf_references_per_node,
            empirical_psf_min_references_per_node=args.empirical_psf_min_references_per_node,
            empirical_psf_fallback_to_adaptive=args.empirical_psf_fallback_to_adaptive,
            annulus_inner_pix=args.annulus_inner_pix,
            annulus_outer_pix=args.annulus_outer_pix,
            aperture_correction_reference_radius_pix=args.aperture_correction_reference_radius_pix,
            aperture_correction_max_calibrator_mag_err=None if args.aperture_correction_max_calibrator_mag_err is not None and args.aperture_correction_max_calibrator_mag_err < 0 else args.aperture_correction_max_calibrator_mag_err,
            aperture_correction_min_calibrators_per_frame=args.aperture_correction_min_calibrators_per_frame,
            aperture_correction_min_plane_calibrators=args.aperture_correction_min_plane_calibrators,
            aperture_correction_min_plane_bic_improvement=args.aperture_correction_min_plane_bic_improvement,
            aperture_correction_min_spatial_peak_to_peak=args.aperture_correction_min_spatial_peak_to_peak,
            aperture_correction_plane_policy=args.aperture_correction_plane_policy,
            aperture_correction_max_residual_mad=None if args.aperture_correction_max_residual_mad is not None and args.aperture_correction_max_residual_mad < 0 else args.aperture_correction_max_residual_mad,
            aperture_correction_max_abs_correction=args.aperture_correction_max_abs_correction,
            aperture_correction_max_abs_spatial_deviation=(
                args.aperture_correction_max_abs_spatial_deviation
            ),
            aperture_correction_max_abs_total_correction=(
                args.aperture_correction_max_abs_total_correction
            ),
            aperture_correction_min_good_frame_fraction=args.aperture_correction_min_good_frame_fraction,
            skip_aperture_correction=args.skip_aperture_correction,
            bulk_insert_batch_size=args.bulk_insert_batch_size,
            make_light_curve_summary=args.make_light_curve_summary,
            enable_detection_branch=args.enable_detection_branch,
            enable_psf_field_diagnostics=args.enable_psf_field_diagnostics,
            psf_diagnostics_max_sources_per_frame=args.psf_diagnostics_max_sources_per_frame,
            psf_diagnostics_min_snr=args.psf_diagnostics_min_snr,
            psf_diagnostics_cutout_radius_pix=args.psf_diagnostics_cutout_radius_pix,
            psf_diagnostics_min_isolation_pix=args.psf_diagnostics_min_isolation_pix,
            psf_diagnostics_clip_sigma=args.psf_diagnostics_clip_sigma,
            psf_diagnostics_n_iterations=args.psf_diagnostics_n_iterations,
            psf_diagnostics_render_plots=args.psf_diagnostics_render_plots,
            enable_aperture_stability_qc=args.enable_aperture_stability_qc,
            require_detection_support=args.require_detection_support,
            target_source_id=args.target_source_id,
            target_object_id=args.target_object_id,
            target_ra_deg=args.target_ra_deg,
            target_dec_deg=args.target_dec_deg,
            target_max_sep_arcsec=args.target_max_sep_arcsec,
            export_target_light_curve=args.export_target_light_curve,
            export_candidate_light_curves=args.export_candidate_light_curves,
            export_candidate_plots=args.export_candidate_plots,
            candidate_light_curve_limit=args.candidate_light_curve_limit,
            exclude_candidates_from_ensemble=args.exclude_candidates_from_ensemble,
            candidate_ensemble_max_iterations=args.candidate_ensemble_max_iterations,
            candidate_ensemble_until_converged=args.candidate_ensemble_until_converged,
            candidate_ensemble_convergence_max_iterations=args.candidate_ensemble_convergence_max_iterations,
            candidate_ensemble_growth_policy=args.candidate_ensemble_growth_policy,
            ensemble_min_ref_stars_per_frame=args.ensemble_min_ref_stars_per_frame,
            enable_spatial_ensemble=args.enable_spatial_ensemble,
            enable_photometric_frame_qc=args.enable_photometric_frame_qc,
            spatial_ensemble_max_order=args.spatial_ensemble_max_order,
            spatial_ensemble_min_ref_stars_per_frame=args.spatial_ensemble_min_ref_stars_per_frame,
            spatial_ensemble_clip_sigma=args.spatial_ensemble_clip_sigma,
            spatial_ensemble_n_iterations=args.spatial_ensemble_n_iterations,
            spatial_ensemble_min_bic_improvement=args.spatial_ensemble_min_bic_improvement,
            spatial_ensemble_min_spatial_peak_to_peak=args.spatial_ensemble_min_spatial_peak_to_peak,
            spatial_ensemble_max_residual_mad=(
                None
                if args.spatial_ensemble_max_residual_mad is not None
                and args.spatial_ensemble_max_residual_mad < 0
                else args.spatial_ensemble_max_residual_mad
            ),
            spatial_ensemble_max_residual_mad_degradation=(
                None
                if args.spatial_ensemble_max_residual_mad_degradation is not None
                and args.spatial_ensemble_max_residual_mad_degradation < 0
                else args.spatial_ensemble_max_residual_mad_degradation
            ),
            spatial_ensemble_max_abs_spatial_deviation=args.spatial_ensemble_max_abs_spatial_deviation,
            spatial_ensemble_min_good_frame_fraction=args.spatial_ensemble_min_good_frame_fraction,
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
            materialize_intermediate_calibrations=False,
            science_materialization_diagnostics=False,
            forced_photometry_diagnostics=args.diagnose_forced_photometry,
            calibration_performance_diagnostics=args.diagnose_calibration_performance,
            random_error_model=args.random_error_model,
            photometry_gain_e_per_adu=args.photometry_gain_e_per_adu,
            photometry_read_noise_e=args.photometry_read_noise_e,
            background_estimator_variance_factor=(
                args.background_estimator_variance_factor
            ),
            empirical_background_floor=args.empirical_background_floor,
            photometry_fractional_flux_floor=args.photometry_fractional_flux_floor,
            photometry_scintillation_enabled=args.photometry_scintillation_enabled,
            photometry_scintillation_coefficient=(
                args.photometry_scintillation_coefficient
            ),
            photometry_telescope_aperture_m=args.photometry_telescope_aperture_m,
            photometry_site_altitude_m=args.photometry_site_altitude_m,
            enable_color_calibration=args.enable_color_calibration,
            auto_reference_photometry=args.auto_reference_photometry,
            color_reference_catalog_name=args.color_reference_catalog_name,
            color_reference_band=args.color_reference_band,
            color_index=args.color_index,
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
        pipeline_failures = [
            (index, item.get("error_message"))
            for index, item in enumerate(result.pipeline_results, start=1)
            if item.get("error_message")
        ]
        if pipeline_failures:
            print("Pipeline failures:")
            for index, error_message in pipeline_failures:
                print(f"  pipeline:{index}: {error_message}")
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



def cmd_batch_sync_plan(args: argparse.Namespace) -> int:
    from dataclasses import asdict

    from grandgousier.batch.state import sync_batch_plan

    config: dict = {}
    if args.config_json_file:
        config = json.loads(Path(args.config_json_file).read_text(encoding="utf-8-sig"))
    Session = make_session_factory(args.database_url)
    with Session() as session:
        result = sync_batch_plan(
            session,
            batch_name=args.batch_name,
            root_path=args.root,
            inventory_csv=args.inventory_csv,
            queue_csv=args.queue_csv,
            legacy_state_csv=args.legacy_state_csv,
            script_name=args.script_name,
            script_version=args.script_version,
            config=config,
        )
        session.commit()
    print(json.dumps(asdict(result), ensure_ascii=False, default=str))
    return 0


def cmd_batch_start_attempt(args: argparse.Namespace) -> int:
    from dataclasses import asdict

    from grandgousier.batch.state import start_batch_attempt

    metadata: dict = {}
    if args.metadata_json_file:
        metadata = json.loads(Path(args.metadata_json_file).read_text(encoding="utf-8-sig"))
    Session = make_session_factory(args.database_url)
    with Session() as session:
        result = start_batch_attempt(
            session,
            batch_run_id=args.batch_run_id,
            job_id=args.job_id,
            manifest_path=args.manifest_path,
            console_log_path=args.console_log_path,
            attempt_kind=args.attempt_kind,
            metadata={"controller_config": metadata} if metadata else {},
        )
        session.commit()
    print(json.dumps(asdict(result), ensure_ascii=False, default=str))
    return 0


def cmd_batch_finish_attempt(args: argparse.Namespace) -> int:
    from grandgousier.batch.state import finish_batch_attempt

    Session = make_session_factory(args.database_url)
    with Session() as session:
        result = finish_batch_attempt(
            session,
            batch_job_attempt_id=args.batch_job_attempt_id,
            exit_code=args.exit_code,
            status=args.status,
            health=args.health,
            science_status=args.science_status,
            catalog_name=args.catalog_name,
            catalog_sources=args.catalog_sources,
            pipeline_count=args.pipeline_count,
            total_points=args.total_points,
            accepted_points=args.accepted_points,
            elapsed_sec=args.elapsed_sec,
            manifest_path=args.manifest_path,
            console_log_path=args.console_log_path,
            message=args.message,
            pipeline_run_id=args.pipeline_run_id,
        )
        session.commit()
        try:
            from grandgousier.batch.state import snapshot_batch_attempt_artifacts

            result["artifact_ids"] = snapshot_batch_attempt_artifacts(
                session, batch_job_attempt_id=args.batch_job_attempt_id
            )
            session.commit()
        except Exception as exc:  # noqa: BLE001 - operational state is already durable.
            session.rollback()
            result["artifact_warning"] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


def cmd_batch_export_state(args: argparse.Namespace) -> int:
    from grandgousier.batch.state import export_batch_csvs

    Session = make_session_factory(args.database_url)
    with Session() as session:
        result = export_batch_csvs(
            session,
            batch_run_id=args.batch_run_id,
            state_csv=args.state_csv,
            summary_csv=args.summary_csv,
        )
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


def cmd_show_batch_runs(args: argparse.Namespace) -> int:
    from grandgousier.batch.state import batch_run_rows

    Session = make_session_factory(args.database_url)
    with Session() as session:
        rows = batch_run_rows(session, limit=args.limit)
    columns = [
        "batch_run_id", "batch_name", "root_path", "plan_revision", "status", "jobs",
        "completed", "healthy", "warnings", "failed", "running", "pending", "last_activity_at",
    ]
    _print_rows(rows, columns, args.format)
    return 0


def cmd_show_batch_jobs(args: argparse.Namespace) -> int:
    from grandgousier.batch.state import batch_job_rows

    Session = make_session_factory(args.database_url)
    with Session() as session:
        rows = batch_job_rows(session, batch_run_id=args.batch_run_id)
    columns = [
        "batch_job_id", "job_id", "object_name", "relative_path", "fits_count", "attempt",
        "status", "health", "science_status", "catalog_name", "total_points", "accepted_points",
        "pipeline_run_id", "elapsed_sec",
    ]
    _print_rows(rows, columns, args.format)
    return 0

def cmd_show_calibration_iterations(args: argparse.Namespace) -> int:
    from grandgousier.calibration.normalized_terms import list_calibration_iterations

    if args.calibration_name is None and args.calibration_version_id is None:
        raise ValueError("--calibration-name or --calibration-version-id is required")
    Session = make_session_factory(args.database_url)
    with Session() as session:
        rows = list_calibration_iterations(
            session,
            calibration_name=args.calibration_name,
            calibration_version_id=args.calibration_version_id,
        )
    columns = [
        "calibration_version_id", "calibration_name", "product_kind",
        "calibration_iteration_id", "iteration_kind", "iteration_no",
        "algorithm", "status", "convergence_status", "frame_terms",
        "object_terms", "pipeline_run_id", "series_id",
    ]
    _print_rows(rows, columns, args.format)
    return 0


def cmd_verify_calibration_terms(args: argparse.Namespace) -> int:
    from grandgousier.calibration.normalized_terms import calibration_term_parity_report

    if args.calibration_name is None and args.calibration_version_id is None:
        raise ValueError("--calibration-name or --calibration-version-id is required")
    Session = make_session_factory(args.database_url)
    with Session() as session:
        report = calibration_term_parity_report(
            session,
            calibration_name=args.calibration_name,
            calibration_version_id=args.calibration_version_id,
            tolerance=args.tolerance,
        )
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        summary = [{key: value for key, value in report.items() if key != "details"}]
        _print_rows(
            summary,
            [
                "status", "calibration_version_id", "calibration_name", "product_kind",
                "calibration_iteration_id", "iteration_no", "frame_terms", "object_terms",
                "legacy_measurements", "checked", "mismatches",
            ],
            "table",
        )
        if report.get("details"):
            _print_rows(
                list(report["details"]),
                ["scope", "id", "field", "normalized", "legacy"],
                "table",
            )
    return 0 if report.get("status") == "OK" else 1


def cmd_verify_calibration_reconstruction(args: argparse.Namespace) -> int:
    from grandgousier.calibration.reconstruction import calibration_reconstruction_parity_report

    if args.calibration_name is None and args.calibration_version_id is None:
        raise ValueError("--calibration-name or --calibration-version-id is required")
    Session = make_session_factory(args.database_url)
    with Session() as session:
        report = calibration_reconstruction_parity_report(
            session,
            calibration_name=args.calibration_name,
            calibration_version_id=args.calibration_version_id,
            tolerance=args.tolerance,
        )
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        summary = [{key: value for key, value in report.items() if key not in {"details", "stages", "chain", "max_abs_delta"}}]
        _print_rows(
            summary,
            [
                "status", "calibration_version_id", "calibration_name", "product_kind",
                "base_calibration_version_id", "chain_length", "base_measurements",
                "reconstructed_measurements", "materialized_measurements", "compared_measurements",
                "field_checks", "missing_rows", "extra_rows", "scalar_mismatches", "mismatches",
            ],
            "table",
        )
        if report.get("stages"):
            _print_rows(
                list(report["stages"]),
                [
                    "calibration_version_id", "product_kind", "calibration_iteration_id",
                    "input_rows", "output_rows", "status", "reason",
                ],
                "table",
            )
        if report.get("max_abs_delta"):
            delta_rows = [
                {"field": key, "max_abs_delta": value}
                for key, value in sorted(dict(report["max_abs_delta"]).items())
            ]
            _print_rows(delta_rows, ["field", "max_abs_delta"], "table")
        if report.get("details"):
            _print_rows(
                list(report["details"]),
                ["scope", "id", "field", "reconstructed", "materialized"],
                "table",
            )
    return 0 if report.get("status") == "OK" else 1


def cmd_materialize_science_calibration(args: argparse.Namespace) -> int:
    from sqlalchemy import select

    from grandgousier.db.models import CalibrationVersion
    from grandgousier.photometry.science_materialization import materialize_science_calibration

    if args.calibration_name is None and args.calibration_version_id is None:
        raise ValueError("--calibration-name or --calibration-version-id is required")
    if args.calibration_name is not None and args.calibration_version_id is not None:
        raise ValueError("Use only one of --calibration-name or --calibration-version-id")

    Session = make_session_factory(args.database_url)
    with Session.begin() as session:
        if args.calibration_version_id is not None:
            cv = session.get(CalibrationVersion, int(args.calibration_version_id))
        else:
            cv = session.scalar(
                select(CalibrationVersion).where(
                    CalibrationVersion.name == str(args.calibration_name)
                )
            )
        if cv is None:
            raise ValueError("Calibration version not found")
        if str(cv.product_role or "").upper() != "SCIENCE":
            raise ValueError(
                "Refusing science backfill for a non-SCIENCE calibration: "
                f"calibration_version_id={cv.calibration_version_id}, product_role={cv.product_role!r}"
            )
        if str(cv.lifecycle_status or "").upper() not in {
            "COMPLETE_IMMUTABLE",
            "SUPERSEDED_IMMUTABLE",
        }:
            raise ValueError(
                "Refusing science backfill for a calibration that is not an immutable finalized generation: "
                f"calibration_version_id={cv.calibration_version_id}, lifecycle_status={cv.lifecycle_status!r}"
            )

        result = materialize_science_calibration(
            session,
            calibration_version_id=int(cv.calibration_version_id),
            shadow_compare_legacy=bool(args.shadow_compare_legacy),
            diagnostics=bool(args.diagnose),
        )
        payload = result.to_dict()
        payload["product_role"] = cv.product_role
        payload["lifecycle_status"] = cv.lifecycle_status

    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        _print_rows(
            [payload],
            [
                "status",
                "calibration_version_id",
                "calibration_name",
                "product_role",
                "lifecycle_status",
                "base_calibration_version_id",
                "reconstructed_rows",
                "inserted_or_updated",
                "deleted_stale_rows",
                "write_strategy",
                "reconstruction_strategy",
                "total_sec",
            ],
            "table",
        )
    return 0 if result.status == "OK" and int(result.reconstructed_rows) > 0 else 1


def cmd_verify_science_materialization(args: argparse.Namespace) -> int:
    from grandgousier.photometry.science_materialization import science_materialization_parity_report

    if args.calibration_name is None and args.calibration_version_id is None:
        raise ValueError("--calibration-name or --calibration-version-id is required")
    Session = make_session_factory(args.database_url)
    with Session() as session:
        report = science_materialization_parity_report(
            session,
            calibration_name=args.calibration_name,
            calibration_version_id=args.calibration_version_id,
            tolerance=args.tolerance,
        )
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        _print_rows(
            [{key: value for key, value in report.items() if key != "max_abs_delta"}],
            [
                "status", "calibration_version_id", "calibration_name",
                "science_measurements", "legacy_rows", "missing_legacy_rows",
                "extra_legacy_rows", "scalar_mismatches", "mismatches",
            ],
            "table",
        )
        if report.get("max_abs_delta"):
            _print_rows(
                [
                    {"field": key, "max_abs_delta": value}
                    for key, value in sorted(dict(report["max_abs_delta"]).items())
                ],
                ["field", "max_abs_delta"],
                "table",
            )
    return 0 if report.get("status") == "OK" else 1


def cmd_backfill_science_photometry_cache(args: argparse.Namespace) -> int:
    from grandgousier.photometry.science_cache import (
        backfill_science_photometry_cache,
        select_science_calibration_ids,
    )

    Session = make_session_factory(args.database_url)
    with Session() as session:
        calibration_ids = select_science_calibration_ids(
            session,
            calibration_version_ids=args.calibration_version_id,
            field_id=args.field_id,
            all_materialized=bool(args.all_materialized),
            include_superseded=bool(args.include_superseded),
        )
    if not calibration_ids:
        raise ValueError("No SCIENCE calibrations matched the requested scope")

    rows: list[dict[str, object]] = []
    ok = True
    for calibration_version_id in calibration_ids:
        with Session.begin() as session:
            result = backfill_science_photometry_cache(
                session,
                calibration_version_id=int(calibration_version_id),
                execute=bool(args.execute),
            )
            rows.append(result.to_dict())
            ok = ok and result.status in {"READY", "OK"}

    if args.format == "json":
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
    else:
        _print_rows(
            rows,
            [
                "calibration_version_id",
                "calibration_name",
                "lifecycle_status",
                "source_rows",
                "source_distinct_base_rows",
                "existing_cache_rows",
                "written_rows",
                "cache_rows_after",
                "scalar_mismatches",
                "status",
                "reason",
            ],
            "table",
        )
    return 0 if ok else 1


def cmd_backfill_science_flag_overrides(args: argparse.Namespace) -> int:
    from grandgousier.photometry.science_flag_overrides import (
        backfill_science_flag_overrides,
        select_v1_science_calibration_ids,
    )

    Session = make_session_factory(args.database_url)
    with Session() as session:
        calibration_ids = select_v1_science_calibration_ids(
            session,
            calibration_version_ids=args.calibration_version_id,
            field_id=args.field_id,
            all_materialized=bool(args.all_materialized),
            include_superseded=bool(args.include_superseded),
        )
    if not calibration_ids:
        raise ValueError("No SCIENCE calibrations matched the requested scope")

    rows: list[dict[str, object]] = []
    ok = True
    for calibration_version_id in calibration_ids:
        with Session.begin() as session:
            result = backfill_science_flag_overrides(
                session,
                calibration_version_id=int(calibration_version_id),
                execute=bool(args.execute),
                frame_batch_size=int(args.frame_batch_size),
                tolerance=float(args.tolerance),
            )
            rows.append(result.to_dict())
            ok = ok and result.status in {"READY", "OK"}

    if args.format == "json":
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
    else:
        _print_rows(
            rows,
            [
                "calibration_version_id",
                "calibration_name",
                "lifecycle_status",
                "source_rows",
                "sampled_or_checked_rows",
                "flags_exact_without_patch",
                "rows_requiring_patch",
                "common_override_keys",
                "common_removed_keys",
                "row_override_rows",
                "scalar_mismatches",
                "identity_mismatches",
                "patch_failures",
                "written_row_overrides",
                "status",
                "reason",
            ],
            "table",
        )
    return 0 if ok else 1


def cmd_verify_science_flag_overrides(args: argparse.Namespace) -> int:
    from grandgousier.photometry.science_flag_overrides import (
        select_v1_science_calibration_ids,
        verify_science_flag_overrides,
    )

    Session = make_session_factory(args.database_url)
    with Session() as session:
        calibration_ids = select_v1_science_calibration_ids(
            session,
            calibration_version_ids=args.calibration_version_id,
            field_id=args.field_id,
            all_materialized=bool(args.all_materialized),
            include_superseded=bool(args.include_superseded),
        )
    if not calibration_ids:
        raise ValueError("No SCIENCE calibrations matched the requested scope")

    rows: list[dict[str, object]] = []
    ok = True
    for calibration_version_id in calibration_ids:
        with Session() as session:
            result = verify_science_flag_overrides(
                session,
                calibration_version_id=int(calibration_version_id),
                frame_batch_size=int(args.frame_batch_size),
                tolerance=float(args.tolerance),
            )
            rows.append(result.to_dict())
            ok = ok and result.status == "OK"

    if args.format == "json":
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
    else:
        _print_rows(
            rows,
            [
                "calibration_version_id",
                "calibration_name",
                "source_rows",
                "checked_rows",
                "scalar_mismatches",
                "identity_mismatches",
                "flag_mismatches",
                "manifest_row_override_count",
                "actual_row_override_count",
                "status",
                "reason",
            ],
            "table",
        )
    return 0 if ok else 1


def cmd_retire_science_measurements(args: argparse.Namespace) -> int:
    from grandgousier.photometry.science_retirement import (
        retire_science_measurements,
        select_retirable_science_calibration_ids,
    )

    Session = make_session_factory(args.database_url)
    with Session() as session:
        calibration_ids = select_retirable_science_calibration_ids(
            session,
            calibration_version_ids=args.calibration_version_id,
            field_id=args.field_id,
            all_materialized=bool(args.all_materialized),
            include_superseded=bool(args.include_superseded),
        )
    if not calibration_ids:
        raise ValueError("No legacy SCIENCE materializations matched the requested scope")

    rows: list[dict[str, object]] = []
    ok = True
    for calibration_version_id in calibration_ids:
        with Session.begin() as session:
            result = retire_science_measurements(
                session,
                calibration_version_id=int(calibration_version_id),
                execute=bool(args.execute),
                frame_batch_size=int(args.frame_batch_size),
                tolerance=float(args.tolerance),
            )
            rows.append(result.to_dict())
            ok = ok and result.status in {"READY", "RETIRED", "ALREADY_RETIRED"}

    if args.format == "json":
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
    else:
        _print_rows(
            rows,
            [
                "calibration_version_id",
                "calibration_name",
                "lifecycle_status",
                "v1_rows_before",
                "cache_rows",
                "manifest_source_rows",
                "manifest_row_override_count",
                "actual_row_override_count",
                "verified_rows",
                "scalar_mismatches",
                "identity_mismatches",
                "flag_mismatches",
                "deleted_rows",
                "v1_rows_after",
                "status",
                "reason",
            ],
            "table",
        )
    return 0 if ok else 1


def cmd_show_artifacts(args: argparse.Namespace) -> int:
    from sqlalchemy import text

    Session = make_session_factory(args.database_url)
    with Session() as session:
        where = []
        params = {"limit": args.limit}
        if args.pipeline_run_id is not None:
            where.append("a.pipeline_run_id = :pipeline_run_id")
            params["pipeline_run_id"] = args.pipeline_run_id
        if args.batch_run_id is not None:
            where.append("a.batch_run_id = :batch_run_id")
            params["batch_run_id"] = args.batch_run_id
        if args.batch_job_attempt_id is not None:
            where.append("a.batch_job_attempt_id = :batch_job_attempt_id")
            params["batch_job_attempt_id"] = args.batch_job_attempt_id
        if args.series_id is not None:
            where.append("a.series_id = :series_id")
            params["series_id"] = args.series_id
        if args.artifact_type:
            where.append("a.artifact_type = :artifact_type")
            params["artifact_type"] = args.artifact_type
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        rows = [dict(row._mapping) for row in session.execute(text(f"""
            SELECT
                a.artifact_id,
                a.pipeline_run_id,
                a.batch_run_id,
                a.batch_job_attempt_id,
                a.series_id,
                a.calibration_version_id,
                a.artifact_type,
                a.artifact_role,
                a.artifact_format,
                a.size_bytes,
                a.sha256,
                a.uri,
                a.publication_uri,
                a.lifecycle_status,
                a.created_at
            FROM artifacts a
            {where_sql}
            ORDER BY a.artifact_id DESC
            LIMIT :limit
        """), params).all()]
    _print_rows(rows, ARTIFACT_COLUMNS, args.format)
    return 0


def cmd_verify_artifacts(args: argparse.Namespace) -> int:
    from sqlalchemy import select
    from grandgousier.artifacts.registry import verify_registered_artifact
    from grandgousier.db.models import Artifact

    Session = make_session_factory(args.database_url)
    with Session() as session:
        stmt = select(Artifact)
        if args.artifact_id is not None:
            stmt = stmt.where(Artifact.artifact_id == args.artifact_id)
        if args.pipeline_run_id is not None:
            stmt = stmt.where(Artifact.pipeline_run_id == args.pipeline_run_id)
        if args.batch_run_id is not None:
            stmt = stmt.where(Artifact.batch_run_id == args.batch_run_id)
        if args.batch_job_attempt_id is not None:
            stmt = stmt.where(Artifact.batch_job_attempt_id == args.batch_job_attempt_id)
        stmt = stmt.order_by(Artifact.artifact_id.desc()).limit(args.limit)
        artifacts = list(session.scalars(stmt))
        rows = []
        failed = 0
        for artifact in artifacts:
            ok, status = verify_registered_artifact(artifact)
            failed += 0 if ok else 1
            rows.append(
                {
                    "artifact_id": artifact.artifact_id,
                    "pipeline_run_id": artifact.pipeline_run_id,
                    "batch_run_id": artifact.batch_run_id,
                    "batch_job_attempt_id": artifact.batch_job_attempt_id,
                    "artifact_type": artifact.artifact_type,
                    "status": status,
                    "size_bytes": artifact.size_bytes,
                    "sha256": artifact.sha256,
                    "uri": artifact.uri,
                }
            )
    _print_rows(rows, ARTIFACT_VERIFY_COLUMNS, args.format)
    return 0 if failed == 0 else 1


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
    "status",
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
    "reference_candidates",
    "usable_reference_objects",
    "base_ensemble_reference_candidates",
    "base_ensemble_usable_reference_objects",
    "base_ensemble_required_references_per_frame",
    "base_ensemble_selected_frames",
    "base_ensemble_frames_with_offsets",
    "base_ensemble_calibrated",
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

ARTIFACT_COLUMNS = [
    "artifact_id",
    "pipeline_run_id",
    "batch_run_id",
    "batch_job_attempt_id",
    "series_id",
    "calibration_version_id",
    "artifact_type",
    "artifact_role",
    "artifact_format",
    "size_bytes",
    "sha256",
    "uri",
    "publication_uri",
    "lifecycle_status",
    "created_at",
]

ARTIFACT_VERIFY_COLUMNS = [
    "artifact_id",
    "pipeline_run_id",
    "batch_run_id",
    "batch_job_attempt_id",
    "artifact_type",
    "status",
    "size_bytes",
    "sha256",
    "uri",
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
    "reference_candidates",
    "usable_reference_objects",
    "base_ensemble_reference_candidates",
    "base_ensemble_usable_reference_objects",
    "base_ensemble_required_references_per_frame",
    "base_ensemble_selected_frames",
    "base_ensemble_frames_with_offsets",
    "base_ensemble_calibrated",
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
        "reference_candidates",
        "usable_reference_objects",
        "base_ensemble_reference_candidates",
        "base_ensemble_usable_reference_objects",
        "base_ensemble_required_references_per_frame",
        "base_ensemble_selected_frames",
        "base_ensemble_frames_with_offsets",
        "base_ensemble_calibrated",
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


def cmd_audit_field_catalog_identity(args: argparse.Namespace) -> int:
    from grandgousier.interseries.catalog_identity import (
        audit_field_catalog_identity,
        group_identity_columns,
        series_identity_columns,
    )

    Session = make_session_factory(args.database_url)
    with Session() as session:
        try:
            audit = audit_field_catalog_identity(
                session,
                field_id=args.field_id,
                field_name=args.field_name,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc

    if args.format == "json":
        print(json.dumps(audit.to_dict(), ensure_ascii=False, indent=2))
        return 0

    print(
        "Catalog identity audit: "
        f"field_id={audit.field_id}, field_name={audit.field_name}, status={audit.status}"
    )
    print("Series catalog coverage:")
    _print_rows(
        [row.to_dict() for row in audit.series],
        series_identity_columns(),
        "table",
    )
    print("Repeated same-system groups:")
    _print_rows(
        [row.to_dict() for row in audit.groups],
        group_identity_columns(),
        "table",
    )
    return 0


def cmd_plan_interseries_calibration(args: argparse.Namespace) -> int:
    from grandgousier.interseries.planning import (
        build_interseries_plan,
        export_interseries_plan_json,
        interseries_pair_columns,
        interseries_series_columns,
    )

    Session = make_session_factory(args.database_url)
    with Session() as session:
        try:
            plan = build_interseries_plan(
                session,
                field_id=args.field_id,
                field_name=args.field_name,
                instrument_unit_id=args.instrument_unit_id,
                filter_name=args.filter_name,
                catalog_name=args.catalog_name,
                min_points_per_object=args.min_points_per_object,
                min_common_objects=args.min_common_objects,
                required_stage2_recipe=args.required_stage2_recipe,
                science_calibration_version_ids=args.science_calibration_version_id,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc

    if args.out:
        out = export_interseries_plan_json(plan, args.out)
        print(f"Exported inter-series plan JSON: path={out}")

    if args.format == "json":
        print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
        return 0

    print(
        "Inter-series plan: "
        f"field_id={plan.field_id}, field_name={plan.field_name}, status={plan.status}, "
        f"series={plan.n_series}, ready={plan.n_ready_series}, "
        f"eligible_pairs={plan.n_eligible_pairs}, largest_component={plan.largest_component_size}, "
        f"recommended_reference_series_id={plan.recommended_reference_series_id}"
    )
    print("Series candidates:")
    _print_rows(
        [row.to_dict() for row in plan.series],
        interseries_series_columns(),
        "table",
    )
    print("Pairwise overlap:")
    _print_rows(
        [row.to_dict() for row in plan.pairs],
        interseries_pair_columns(),
        "table",
    )
    if plan.connected_components:
        print(
            "Eligible connected components: "
            + "; ".join(",".join(str(value) for value in component) for component in plan.connected_components)
        )
    return 0


def cmd_analyze_interseries_calibration(args: argparse.Namespace) -> int:
    from grandgousier.interseries.analysis import (
        GRAPH_OFFSET_COLUMNS,
        PAIR_OFFSET_COLUMNS,
        SERIES_SUMMARY_COLUMNS,
        analyze_interseries_calibration,
        export_interseries_analysis,
    )

    Session = make_session_factory(args.database_url)
    with Session() as session:
        try:
            analysis = analyze_interseries_calibration(
                session,
                field_id=args.field_id,
                field_name=args.field_name,
                instrument_unit_id=args.instrument_unit_id,
                filter_name=args.filter_name,
                catalog_name=args.catalog_name,
                reference_series_id=args.reference_series_id,
                min_points_per_object=args.min_points_per_object,
                min_common_objects=args.min_common_objects,
                clip_sigma=args.clip_sigma,
                max_clip_iterations=args.max_clip_iterations,
                required_stage2_recipe=args.required_stage2_recipe,
                science_calibration_version_ids=args.science_calibration_version_id,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc

    paths = export_interseries_analysis(analysis, args.out_root)
    if args.format == "json":
        print(json.dumps(analysis.to_dict(), ensure_ascii=False, indent=2))
        print(f"Stage-3 diagnostics: {paths['summary'].parent}")
        return 0

    graph = analysis.graph_solution
    print(
        "Inter-series analysis: "
        f"field_id={analysis.field_id}, field_name={analysis.field_name}, status={analysis.status}, "
        f"series={len(analysis.selected_series_ids)}, reference_series_id={analysis.reference_series_id}, "
        f"pairs_ok={sum(1 for row in analysis.pairwise_offsets if row.status == 'OK')}, "
        f"graph_status={graph.status if graph is not None else None}, "
        f"reduced_chi2={graph.reduced_chi2 if graph is not None else None}"
    )
    print("Series summary:")
    _print_rows([row.to_dict() for row in analysis.series_summaries], list(SERIES_SUMMARY_COLUMNS), "table")
    print("Pairwise offsets (delta_ab = m_b - m_a):")
    _print_rows([row.to_dict() for row in analysis.pairwise_offsets], list(PAIR_OFFSET_COLUMNS), "table")
    if graph is not None and graph.offsets:
        print("Global additive series offsets (m_global = m_science + z_s):")
        _print_rows([row.to_dict() for row in graph.offsets], list(GRAPH_OFFSET_COLUMNS), "table")
    print(f"Stage-3 diagnostics: {paths['summary'].parent}")
    return 0


def cmd_persist_interseries_calibration(args: argparse.Namespace) -> int:
    from grandgousier.interseries.persistence import persist_interseries_calibration

    Session = make_session_factory(args.database_url)
    try:
        with Session.begin() as session:
            result = persist_interseries_calibration(
                session,
                field_id=args.field_id,
                field_name=args.field_name,
                instrument_unit_id=args.instrument_unit_id,
                filter_name=args.filter_name,
                catalog_name=args.catalog_name,
                reference_series_id=args.reference_series_id,
                min_points_per_object=args.min_points_per_object,
                min_common_objects=args.min_common_objects,
                clip_sigma=args.clip_sigma,
                max_clip_iterations=args.max_clip_iterations,
                required_stage2_recipe=args.required_stage2_recipe,
                science_calibration_version_ids=args.science_calibration_version_id,
                allow_sparse_graph=bool(args.allow_sparse_graph),
            )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    row = result.to_dict()
    if args.format == "json":
        print(json.dumps(row, ensure_ascii=False, indent=2, default=str))
        return 0
    print(
        "Inter-series M4 persistence: "
        f"generation_id={result.interseries_generation_id}, generation_uid={result.generation_uid}, "
        f"created={result.created}, reliability={result.reliability_status}, "
        f"series={result.n_series}, edges={result.n_edges}, dof={result.graph_dof}"
    )
    _print_rows([row], list(row.keys()), "table")
    return 0


def cmd_show_interseries_calibration(args: argparse.Namespace) -> int:
    from grandgousier.interseries.persistence import (
        list_interseries_generations,
        load_interseries_generation_edges,
        load_interseries_generation_series,
    )

    Session = make_session_factory(args.database_url)
    with Session() as session:
        rows = list_interseries_generations(
            session,
            generation_id=args.generation_id,
            field_id=args.field_id,
            filter_name=args.filter_name,
            limit=args.limit,
        )
        if args.format == "json":
            payload = []
            for row in rows:
                item = dict(row)
                item["series"] = load_interseries_generation_series(
                    session, generation_id=int(row["interseries_generation_id"])
                )
                item["edges"] = load_interseries_generation_edges(
                    session, generation_id=int(row["interseries_generation_id"])
                )
                payload.append(item)
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
            return 0

        if not rows:
            print("No persisted inter-series calibration generations.")
            return 0
        columns = [
            "interseries_generation_id", "generation_uid", "field_id", "field_name",
            "instrument_unit_id", "filter_name", "catalog_name", "reference_series_id",
            "reliability_status", "n_series", "n_edges", "graph_dof", "reduced_chi2",
            "min_common_objects", "lifecycle_status", "created_at",
        ]
        _print_rows(rows, columns, "table")
        if args.generation_id is not None and len(rows) == 1:
            gid = int(rows[0]["interseries_generation_id"])
            print("Series offsets and exact Stage-2 SCIENCE sources:")
            series = load_interseries_generation_series(session, generation_id=gid)
            if series:
                _print_rows(series, list(series[0].keys()), "table")
            print("Pairwise graph edges:")
            edges = load_interseries_generation_edges(session, generation_id=gid)
            if edges:
                _print_rows(edges, list(edges[0].keys()), "table")
            else:
                print("No rows.")
    return 0


def cmd_verify_interseries_calibration(args: argparse.Namespace) -> int:
    from grandgousier.interseries.persistence import verify_interseries_generation

    Session = make_session_factory(args.database_url)
    with Session() as session:
        try:
            result = verify_interseries_generation(session, generation_id=args.generation_id)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    row = result.to_dict()
    if args.format == "json":
        print(json.dumps(row, ensure_ascii=False, indent=2, default=str))
    else:
        summary = {key: value for key, value in row.items() if key != "details"}
        _print_rows([summary], list(summary.keys()), "table")
    return 0 if result.status == "OK" else 2


def cmd_export_global_object_light_curve(args: argparse.Namespace) -> int:
    from grandgousier.lightcurves.global_object import export_global_object_light_curve

    Session = make_session_factory(args.database_url)
    with Session() as session:
        try:
            result = export_global_object_light_curve(
                session,
                object_id=args.object_id,
                field_id=args.field_id,
                generation_ids=args.generation_ids or None,
                out_root=args.out_root,
                include_rejected=bool(args.include_rejected),
                include_rejected_debug=bool(args.include_rejected_debug),
                allow_partial=bool(args.allow_partial),
                require_detection_support=bool(args.require_detection_support),
                extra_reject_flags=args.extra_reject_flags,
                plots=not bool(args.no_plots),
                plot_reduction=args.plot_reduction,
                plot_max_points=args.plot_max_points,
                plot_bin_minutes=args.plot_bin_minutes,
                plot_format=args.plot_format,
                plot_dpi=args.plot_dpi,
                plot_time_axis=args.plot_time_axis,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc

    payload = result.to_dict()
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0 if result.accepted_rows > 0 else 2

    print(
        "M5 global object light curve: "
        f"object_id={result.object_id}, field_id={result.field_id}, status={result.status}, "
        f"source_rows={result.rows_source_exact_m4}, written={result.rows_written}, "
        f"accepted={result.accepted_rows}, rejected={result.rejected_rows}, "
        f"source={result.science_read_source}, "
        f"generations={','.join(str(v) for v in result.generation_ids)}"
    )
    print(f"CSV: {result.csv_path}")
    print(f"Summary: {result.summary_path}")
    if result.rejection_debug_path:
        print(f"Rejections: {result.rejection_debug_path}")
    if result.rejection_reasons:
        print("M5 rejection reasons:")
        rejection_rows = [
            {"reason": reason, "count": count}
            for reason, count in sorted(result.rejection_reasons.items(), key=lambda item: (-item[1], item[0]))
        ]
        _print_rows(rejection_rows, ["reason", "count"], "table")
    if result.plot_paths:
        plot_rows = []
        for filter_name, path in sorted(result.plot_paths.items()):
            plot_rows.append({
                "filter_name": filter_name,
                "reduction": result.plot_reduction.get(filter_name),
                "input_points": result.plot_input_points.get(filter_name),
                "plot_points": result.plot_output_points.get(filter_name),
                "time_axis": result.plot_time_axis.get(filter_name),
                "breaks": result.plot_time_breaks.get(filter_name),
                "style": result.plot_style.get(filter_name),
                "plot": path,
                "plot_data": result.plot_data_paths.get(filter_name),
            })
        _print_rows(
            plot_rows,
            ["filter_name", "reduction", "input_points", "plot_points", "time_axis", "breaks", "style", "plot", "plot_data"],
            "table",
        )
    if result.warnings:
        for warning in result.warnings:
            print(f"WARNING: {warning}")
    return 0 if result.accepted_rows > 0 else 2


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
        scope, rows = list_light_curve_objects(
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
            interseries_generation_ids=args.interseries_generation_id,
            reject_flags=reject_flags,
            min_points=args.min_points,
            exclude_rejected=args.exclude_rejected,
            limit=args.limit,
        )
    for warning in scope.warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
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
            interseries_generation_ids=args.interseries_generation_id,
            reject_flags=reject_flags,
        )
    for warning in summary.scope.warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
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
            interseries_generation_ids=args.interseries_generation_id,
            reject_flags=reject_flags,
            exclude_rejected=args.exclude_rejected,
            limit=args.limit,
        )
    for warning in scope.warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
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
            f"selected_frame_ids_count={scope.selected_frame_ids_count}, "
            f"interseries_generation_ids={scope.interseries_generation_ids}"
        )
    return 0


def cmd_export_lightcurve_plot(args: argparse.Namespace) -> int:
    from grandgousier.calibration.preliminary import load_lightcurve_rows
    from grandgousier.visualization.lightcurve_plot import (
        export_lightcurve_plot,
        format_lightcurve_identity_subtitle,
    )

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
    anchor_row = rows[0] if rows else None
    subtitle = format_lightcurve_identity_subtitle(
        object_uid=getattr(anchor_row, "object_uid", None),
        ra_deg=getattr(anchor_row, "ra_deg", None),
        dec_deg=getattr(anchor_row, "dec_deg", None),
        catalog_name=getattr(anchor_row, "catalog_name", None),
        source_id=getattr(anchor_row, "source_id", None),
    )
    result = export_lightcurve_plot(
        rows,
        args.out,
        x_key=args.x,
        y_key=args.y,
        title=args.title,
        subtitle=subtitle,
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
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
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


def cmd_psf_field_diagnostics(args: argparse.Namespace) -> int:
    from grandgousier.archive.fields_series import load_series_selection
    from grandgousier.quality.psf_field import analyze_psf_field

    frame_ids = [int(value) for value in (args.frame_id or [])]
    instrument_unit_id = args.instrument_unit_id
    Session = make_session_factory(args.database_url)
    with Session() as session:
        if args.series_id is not None:
            selection = load_series_selection(session, int(args.series_id))
            series_frame_ids = [int(value) for value in selection.get("frame_ids") or []]
            if frame_ids:
                allowed = set(series_frame_ids)
                frame_ids = [value for value in frame_ids if value in allowed]
            else:
                frame_ids = series_frame_ids
            if instrument_unit_id is None and selection.get("instrument_unit_id") is not None:
                instrument_unit_id = int(selection["instrument_unit_id"])
        if not frame_ids and instrument_unit_id is None:
            print("psf-field-diagnostics requires --series-id, --frame-id, or --instrument-unit-id", file=sys.stderr)
            return 2
        result = analyze_psf_field(
            session,
            frame_ids=frame_ids or None,
            instrument_unit_id=instrument_unit_id,
            detection_algorithm=args.detection_algorithm,
            out_dir=args.out_dir,
            max_sources_per_frame=args.max_sources_per_frame,
            min_snr=args.min_snr,
            cutout_radius_pix=args.cutout_radius_pix,
            min_isolation_pix=args.min_isolation_pix,
            clip_sigma=args.clip_sigma,
            n_iterations=args.n_iterations,
            render_plots=args.render_plots,
        )
    print(
        "PSF field diagnostics: "
        f"selected_frames={result.selected_frames}, analyzed_frames={result.analyzed_frames}, "
        f"measured_sources={result.measured_sources}, failed_frames={result.failed_frames}, "
        f"without_detections={result.frames_without_detections}, "
        f"without_usable_sources={result.frames_without_usable_psf_sources}"
    )
    if result.frames_csv:
        print(f"  frames_csv={result.frames_csv}")
    if result.sources_csv:
        print(f"  sources_csv={result.sources_csv}")
    if result.summary_json:
        print(f"  summary_json={result.summary_json}")
    if result.field_map_png:
        print(f"  field_map_png={result.field_map_png}")
    if result.time_series_png:
        print(f"  time_series_png={result.time_series_png}")
    for warning in result.warnings:
        print(f"  WARNING: {warning}")
    return 0 if result.failed_frames == 0 and result.analyzed_frames > 0 else 1



def cmd_adaptive_aperture_diagnostics(args: argparse.Namespace) -> int:
    from grandgousier.archive.fields_series import load_series_selection
    from grandgousier.photometry.adaptive_aperture import analyze_adaptive_aperture

    Session = make_session_factory(args.database_url)
    with Session() as session:
        selection = load_series_selection(session, int(args.series_id))
        frame_ids = [int(value) for value in selection.get("frame_ids") or []]
        result = analyze_adaptive_aperture(
            session,
            frame_ids=frame_ids,
            catalog_name=args.catalog_name,
            detection_algorithm=args.detection_algorithm,
            out_dir=args.out_dir,
            base_aperture_radius_pix=args.base_aperture_radius_pix,
            annulus_inner_pix=args.annulus_inner_pix,
            annulus_outer_pix=args.annulus_outer_pix,
            min_psf_snr=args.min_psf_snr,
            max_psf_sources_per_frame=args.max_psf_sources_per_frame,
            psf_cutout_radius_pix=args.psf_cutout_radius_pix,
            psf_min_isolation_pix=args.psf_min_isolation_pix,
            interpolation_neighbors=args.interpolation_neighbors,
            min_psf_references=args.min_psf_references,
            aperture_fwhm_factor=args.aperture_fwhm_factor,
            max_axis_ratio=args.max_axis_ratio,
            max_radius_scale=args.max_radius_scale,
            render_plots=args.render_plots,
        )
    print(
        "Adaptive aperture diagnostics: "
        f"selected_frames={result.selected_frames}, analyzed_frames={result.analyzed_frames}, "
        f"measured_pairs={result.measured_pairs}, psf_model_failures={result.frames_without_psf_model}, "
        f"failed_frames={result.failed_frames}"
    )
    for label, value in (
        ("measurements_csv", result.measurements_csv),
        ("frames_csv", result.frames_csv),
        ("objects_csv", result.objects_csv),
        ("summary_json", result.summary_json),
        ("field_map_png", result.field_map_png),
        ("time_series_png", result.time_series_png),
    ):
        if value:
            print(f"  {label}={value}")
    if result.warnings:
        print("Warnings:")
        for warning in result.warnings:
            print(f"  {warning}")
    return 0 if result.failed_frames == 0 else 2

def cmd_source_detection_summary(args: argparse.Namespace) -> int:
    from sqlalchemy import func, select
    from grandgousier.db.detection_runs import SOURCE_DETECTION_RUN
    from grandgousier.db.models import DetectionRun, DetectionRunFrame, Frame, InstrumentUnit, SourceDetection

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
                DetectionRun.algorithm.label("algorithm"),
                func.count(SourceDetection.source_detection_id).label("n_detections"),
                func.min(SourceDetection.snr).label("snr_min"),
                func.max(SourceDetection.snr).label("snr_max"),
            )
            .select_from(SourceDetection)
            .join(DetectionRunFrame, DetectionRunFrame.detection_run_frame_id == SourceDetection.detection_run_frame_id)
            .join(DetectionRun, DetectionRun.detection_run_id == DetectionRunFrame.detection_run_id)
            .join(Frame, Frame.frame_id == DetectionRunFrame.frame_id)
            .outerjoin(InstrumentUnit, InstrumentUnit.instrument_unit_id == Frame.instrument_unit_id)
            .where(DetectionRun.run_kind == SOURCE_DETECTION_RUN)
            .group_by(
                InstrumentUnit.instrument_unit_id,
                InstrumentUnit.instrument,
                InstrumentUnit.site,
                InstrumentUnit.tube,
                InstrumentUnit.camera,
                InstrumentUnit.filter_name,
                InstrumentUnit.observing_mode,
                SourceDetection.detection_status,
                DetectionRun.algorithm,
            )
            .order_by(InstrumentUnit.instrument_unit_id, SourceDetection.detection_status)
        )
        if args.instrument_unit_id is not None:
            stmt = stmt.where(InstrumentUnit.instrument_unit_id == int(args.instrument_unit_id))

        rows = []
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
        "instrument_unit_id", "instrument", "site", "tube", "camera", "filter_name",
        "observing_mode", "detection_status", "algorithm", "n_detections", "snr_min", "snr_max",
    ]
    _print_rows(rows, columns, args.format)
    return 0

def cmd_show_source_detections(args: argparse.Namespace) -> int:
    from sqlalchemy import select
    from grandgousier.db.detection_runs import SOURCE_DETECTION_RUN
    from grandgousier.db.models import DetectionRun, DetectionRunFrame, Frame, InstrumentUnit, SourceDetection

    Session = make_session_factory(args.database_url)
    with Session() as session:
        stmt = (
            select(
                SourceDetection.source_detection_id,
                DetectionRunFrame.frame_id.label("frame_id"),
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
                DetectionRun.algorithm.label("algorithm"),
            )
            .select_from(SourceDetection)
            .join(DetectionRunFrame, DetectionRunFrame.detection_run_frame_id == SourceDetection.detection_run_frame_id)
            .join(DetectionRun, DetectionRun.detection_run_id == DetectionRunFrame.detection_run_id)
            .join(Frame, Frame.frame_id == DetectionRunFrame.frame_id)
            .outerjoin(InstrumentUnit, InstrumentUnit.instrument_unit_id == Frame.instrument_unit_id)
            .where(DetectionRun.run_kind == SOURCE_DETECTION_RUN)
            .order_by(SourceDetection.source_detection_id)
            .offset(args.offset)
            .limit(args.limit)
        )
        if args.frame_id is not None:
            stmt = stmt.where(DetectionRunFrame.frame_id == args.frame_id)
        if args.instrument:
            stmt = stmt.where(Frame.instrument == args.instrument)
        if args.instrument_unit_id is not None:
            stmt = stmt.where(Frame.instrument_unit_id == args.instrument_unit_id)
        rows = []
        for row in session.execute(stmt).all():
            d = dict(row._mapping)
            d["date_obs"] = _format_dt(d.get("date_obs"))
            for key, nd in (("x", 3), ("y", 3), ("ra_deg", 8), ("dec_deg", 8), ("peak_adu", 3), ("flux", 3), ("snr", 3), ("local_background", 3)):
                d[key] = _round_float(d.get(key), nd)
            rows.append(d)
    columns = [
        "source_detection_id", "frame_id", "instrument", "tube", "camera", "filter_name",
        "frame_object", "date_obs", "x", "y", "ra_deg", "dec_deg", "peak_adu", "flux",
        "snr", "local_background", "detection_status", "algorithm",
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
        f"queried_frames={result.queried_frames}, queried_cones={result.queried_cones}, effective_query_mode={result.effective_query_mode}, row_limit_reached_cones={result.row_limit_reached_cones}, read_rows={result.read_rows}, "
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
    from grandgousier.db.detection_runs import CATALOG_MATCH_RUN
    from grandgousier.db.models import DetectionCatalogMatch, DetectionRun, DetectionRunFrame, Frame, InstrumentUnit

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
                DetectionRun.catalog_name.label("catalog_name"),
                DetectionRun.algorithm.label("algorithm"),
                func.count(DetectionCatalogMatch.detection_catalog_match_id).label("n_matches"),
                func.avg(DetectionCatalogMatch.sep_arcsec).label("sep_mean_arcsec"),
                func.min(DetectionCatalogMatch.sep_arcsec).label("sep_min_arcsec"),
                func.max(DetectionCatalogMatch.sep_arcsec).label("sep_max_arcsec"),
                func.avg(DetectionCatalogMatch.zero_point).label("zp_mean"),
            )
            .select_from(DetectionCatalogMatch)
            .join(DetectionRunFrame, DetectionRunFrame.detection_run_frame_id == DetectionCatalogMatch.detection_run_frame_id)
            .join(DetectionRun, DetectionRun.detection_run_id == DetectionRunFrame.detection_run_id)
            .join(Frame, Frame.frame_id == DetectionRunFrame.frame_id)
            .outerjoin(InstrumentUnit, InstrumentUnit.instrument_unit_id == Frame.instrument_unit_id)
            .where(DetectionRun.run_kind == CATALOG_MATCH_RUN)
            .group_by(
                InstrumentUnit.instrument_unit_id, InstrumentUnit.instrument, InstrumentUnit.site,
                InstrumentUnit.tube, InstrumentUnit.camera, InstrumentUnit.filter_name,
                InstrumentUnit.observing_mode, DetectionRun.catalog_name, DetectionRun.algorithm,
            )
            .order_by(InstrumentUnit.instrument_unit_id, DetectionRun.catalog_name)
        )
        if args.catalog_name:
            stmt = stmt.where(DetectionRun.catalog_name == args.catalog_name)
        if args.instrument_unit_id is not None:
            stmt = stmt.where(Frame.instrument_unit_id == args.instrument_unit_id)
        rows = []
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
        "instrument_unit_id", "instrument", "site", "tube", "camera", "filter_name",
        "observing_mode", "catalog_name", "algorithm", "n_matches", "sep_mean_arcsec",
        "sep_min_arcsec", "sep_max_arcsec", "zp_mean",
    ]
    _print_rows(rows, columns, args.format)
    return 0

def cmd_show_source_catalog_matches(args: argparse.Namespace) -> int:
    from sqlalchemy import select
    from grandgousier.db.detection_runs import CATALOG_MATCH_RUN
    from grandgousier.db.models import (
        CatalogSource, DetectionCatalogMatch, DetectionRun, DetectionRunFrame,
        Frame, InstrumentUnit, SourceDetection,
    )

    Session = make_session_factory(args.database_url)
    with Session() as session:
        stmt = (
            select(
                DetectionCatalogMatch.detection_catalog_match_id,
                DetectionRunFrame.frame_id.label("frame_id"),
                InstrumentUnit.instrument,
                InstrumentUnit.tube,
                InstrumentUnit.camera,
                InstrumentUnit.filter_name,
                Frame.object_name.label("frame_object"),
                Frame.date_obs,
                DetectionRun.catalog_name.label("catalog_name"),
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
            .join(DetectionRunFrame, DetectionRunFrame.detection_run_frame_id == DetectionCatalogMatch.detection_run_frame_id)
            .join(DetectionRun, DetectionRun.detection_run_id == DetectionRunFrame.detection_run_id)
            .join(Frame, Frame.frame_id == DetectionRunFrame.frame_id)
            .join(SourceDetection, SourceDetection.source_detection_id == DetectionCatalogMatch.source_detection_id)
            .join(CatalogSource, CatalogSource.catalog_source_id == DetectionCatalogMatch.catalog_source_id)
            .outerjoin(InstrumentUnit, InstrumentUnit.instrument_unit_id == Frame.instrument_unit_id)
            .where(DetectionRun.run_kind == CATALOG_MATCH_RUN)
            .order_by(DetectionCatalogMatch.detection_catalog_match_id)
            .offset(args.offset)
            .limit(args.limit)
        )
        if args.catalog_name:
            stmt = stmt.where(DetectionRun.catalog_name == args.catalog_name)
        if args.frame_id is not None:
            stmt = stmt.where(DetectionRunFrame.frame_id == args.frame_id)
        if args.instrument:
            stmt = stmt.where(Frame.instrument == args.instrument)
        if args.instrument_unit_id is not None:
            stmt = stmt.where(Frame.instrument_unit_id == args.instrument_unit_id)
        rows = []
        for row in session.execute(stmt).all():
            d = dict(row._mapping)
            d["date_obs"] = _format_dt(d.get("date_obs"))
            for key, nd in (("det_x", 3), ("det_y", 3), ("dx_pix", 4), ("dy_pix", 4), ("sep_arcsec", 4), ("snr", 3), ("detection_mag_inst", 5), ("catalog_mag", 5), ("zero_point", 5)):
                d[key] = _round_float(d.get(key), nd)
            rows.append(d)
    columns = [
        "detection_catalog_match_id", "frame_id", "instrument", "tube", "camera", "filter_name",
        "frame_object", "date_obs", "catalog_name", "source_id", "det_x", "det_y", "dx_pix",
        "dy_pix", "sep_arcsec", "snr", "detection_mag_inst", "catalog_mag", "zero_point", "match_status",
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
    parser.add_argument("--offset-algorithm", default="interseries_graph_median_v1", help="Immutable interseries generation analysis_algorithm")
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
    from grandgousier.db.models import CatalogSource, DetectionCatalogMatch, DetectionRun, DetectionRunFrame, Frame, FrameCatalogLink, FrameQc, InstrumentUnit, Observation, ProcessingEvent, Object, ObjectQc, Measurement, CalibrationVersion, SourceDetection

    Session = make_session_factory(args.database_url)
    with Session() as session:
        n_frames = session.scalar(select(func.count(Frame.frame_id)))
        n_observations = session.scalar(select(func.count(Observation.observation_id)))
        n_events = session.scalar(select(func.count(ProcessingEvent.event_id)))
        n_units = session.scalar(select(func.count(InstrumentUnit.instrument_unit_id)))
        n_catalog_sources = session.scalar(select(func.count(CatalogSource.catalog_source_id)))
        n_frame_catalog_links = session.scalar(select(func.count(FrameCatalogLink.frame_catalog_link_id)))
        n_objects = session.scalar(select(func.count(Object.object_id)))
        n_measurements = session.scalar(select(func.count(Measurement.measurement_id)))
        n_calibration_versions = session.scalar(select(func.count(CalibrationVersion.calibration_version_id)))
        n_detection_runs = session.scalar(select(func.count(DetectionRun.detection_run_id)))
        n_detection_run_frames = session.scalar(select(func.count(DetectionRunFrame.detection_run_frame_id)))
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
    print(f"observations={n_observations}")
    print(f"instrument_units={n_units}")
    print(f"catalog_sources={n_catalog_sources}")
    print(f"frame_catalog_links={n_frame_catalog_links}")
    print(f"objects={n_objects}")
    print(f"measurements={n_measurements}")
    print(f"calibration_versions={n_calibration_versions}")
    print(f"detection_runs={n_detection_runs}")
    print(f"detection_run_frames={n_detection_run_frames}")
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


def cmd_export_science_parquet(args: argparse.Namespace) -> int:
    from sqlalchemy import create_engine
    from grandgousier.lightcurves.parquet_export import export_series

    engine = create_engine(args.database_url, pool_pre_ping=True, future=True)
    try:
        with engine.connect() as connection:
            with connection.begin():
                # PostgreSQL enforces this transaction setting before any data query.
                from sqlalchemy.orm import Session
                with Session(bind=connection, autoflush=False, expire_on_commit=False) as session:
                    manifest = export_series(
                        session, args.series_id, args.out, logical_db_id=args.db_logical_id,
                        calibration_version_id=args.calibration_version_id,
                        interseries_generation_id=args.interseries_generation_id, batch_size=args.batch_size,
                    )
        print(json.dumps(manifest, indent=2, sort_keys=True))
        return 0
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    # Exact opt-ins dispatch before argparse, Settings(), .env, config,
    # logging and the ordinary SQLAlchemy schema-status path. Never render argv.
    integrity_result = _maybe_guarded_p207_integrity_schema_status(argv)
    if integrity_result is not None:
        return integrity_result
    probe_result = _maybe_guarded_p207_probe_schema_status(argv)
    if probe_result is not None:
        return probe_result
    guarded_result = _maybe_guarded_p207_schema_status(argv)
    if guarded_result is not None:
        return guarded_result
    original_stderr = sys.stderr
    sys.stderr = _SanitizingStderr(original_stderr)
    try:
        return _run_main(argv)
    except SystemExit as exc:
        if isinstance(exc.code, str):
            raise SystemExit(sanitize_diagnostic_text(exc.code)) from exc
        raise
    except Exception as exc:
        rendered_traceback = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
        sys.stderr.write(sanitize_diagnostic_text(rendered_traceback))
        return 1
    finally:
        sys.stderr = original_stderr


def _maybe_guarded_p207_integrity_schema_status(argv: list[str] | None) -> int | None:
    tokens = sys.argv[1:] if argv is None else argv
    # Existing ledger/probe guards still own private-config-only and shared prefixes.
    # An integrity-specific prefix or flag must never reach Settings/argparse.
    if not any(
        isinstance(token, str) and (
            token.startswith(_P207_GUARDED_INTEGRITY_FLAG)
            or (len(option := token.partition("=")[0]) > len("--guard-p207-test-readonly-")
                and _P207_GUARDED_INTEGRITY_FLAG.startswith(option))
        ) for token in tokens
    ):
        return None
    private_config = os.environ.get(_P207_PRIVATE_CONFIG_ENV)
    exact = ["--config", private_config,
             "schema-status", _P207_GUARDED_INTEGRITY_FLAG]
    if private_config and tokens == exact:
        try:
            from grandgousier.db.migrations import guarded_p207_test_readonly_integrity

            counts = guarded_p207_test_readonly_integrity()
            if (type(counts) is not tuple or len(counts) != 5
                    or any(type(n) is not int for n in counts)
                    or counts[0] != 35 or not 0 < counts[1] <= 10000
                    or counts[2:] != (18, 4, 36)):
                raise ValueError("Unverified bounded integrity result")
        except BaseException:  # noqa: BLE001 - never render source/driver/SQL exceptions
            print("STOP P207_TEST_INTEGRITY_UNVERIFIED", file=sys.stderr)
            return 2
        print("PREFLIGHT_ONLY P207_TEST_INTEGRITY OLD35 NOT_WRITE_READY "
              f"ledger=35 cv={counts[1]} series=18 generations=4 members=36 edges=144 "
              "role_lifecycle=DIAGNOSTIC_ONLY")
        return 0
    print("STOP P207_TEST_INTEGRITY_INVALID_ARGS", file=sys.stderr)
    return 2


def _maybe_guarded_p207_probe_schema_status(argv: list[str] | None) -> int | None:
    tokens = sys.argv[1:] if argv is None else argv
    if not any(
        isinstance(token, str)
        and (
            token.startswith(_P207_GUARDED_PROBE_FLAG)
            or (
                len(option := token.partition("=")[0]) > len("--guard-p207-test-readonly-")
                and _P207_GUARDED_PROBE_FLAG.startswith(option)
            )
        )
        for token in tokens
    ):
        return None
    private_config = os.environ.get(_P207_PRIVATE_CONFIG_ENV)
    if tokens != [
        "--config", private_config,
        "schema-status", _P207_GUARDED_PROBE_FLAG,
    ] or not private_config:
        print("STOP P207_GUARDED_PROBE_INVALID_ARGS", file=sys.stderr)
        return 2
    try:
        from grandgousier.db.migrations import guarded_p207_test_readonly_probe

        failed = guarded_p207_test_readonly_probe()
        if (type(failed) is not tuple
                or any(type(code) is not str or code not in _P207_PROBE_FAILURE_CODES
                       for code in failed)
                or len(failed) != len(set(failed))
                or ("ROW_SHAPE" in failed and failed != ("ROW_SHAPE",))):
            raise ValueError("Unverified probe classification")
    except BaseException:  # noqa: BLE001 - no driver/config/cleanup exception may escape
        print("STOP P207_GUARDED_PROBE_UNVERIFIED", file=sys.stderr)
        return 2
    if failed:
        print("STOP P207_TEST_BACKEND_PROBE_ONLY " + " ".join(failed), file=sys.stderr)
        return 2
    print("PROBE_ONLY P207_TEST_BACKEND_STRUCTURAL_CHECK")
    return 0


def cmd_finalize_field_series(args: argparse.Namespace) -> int:
    from grandgousier.archive.fields_series import finalize_open_series

    Session = make_session_factory(args.database_url)
    with Session() as session:
        result = finalize_open_series(session, args.series_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "READY" else 2


def _maybe_guarded_p207_schema_status(argv: list[str] | None) -> int | None:
    tokens = sys.argv[1:] if argv is None else argv
    private_config = os.environ.get(_P207_PRIVATE_CONFIG_ENV)
    # Detect the explicit config path even when the flag is missing or --config
    # is abbreviated/attached. argparse also accepts prefixes such as --g here;
    # these must not reach the ordinary parser or Settings/.env on schema-status.
    has_schema_status = "schema-status" in tokens
    if (
        private_config is None
        and _p207_legacy_private_config_named(tokens)
    ):
        print("STOP P207_CONFIG_REQUIRED", file=sys.stderr)
        return 2
    if not any(
        isinstance(token, str)
        and (
            (private_config is not None and private_config in token)
            or token.startswith(_P207_GUARDED_LEDGER_FLAG)
            or (
                (has_schema_status or token.startswith("--guard"))
                and len(option := token.partition("=")[0]) > 2
                and _P207_GUARDED_LEDGER_FLAG.startswith(option)
            )
        )
        for token in tokens
    ):
        return None
    if tokens != [
        "--config", private_config,
        "schema-status", _P207_GUARDED_LEDGER_FLAG,
    ] or not private_config:
        print("STOP P207_GUARDED_LEDGER_INVALID_ARGS", file=sys.stderr)
        return 2
    try:
        from grandgousier.db.migrations import guarded_p207_test_readonly_ledger

        state = guarded_p207_test_readonly_ledger()
    except BaseException:  # noqa: BLE001 - never let driver/config errors reveal credentials
        # Driver/config/SQL exceptions may contain a password or URL. No traceback,
        # exception text, input path, environment value, or raw argument is emitted.
        print("STOP P207_GUARDED_LEDGER_UNVERIFIED", file=sys.stderr)
        return 2
    if state == "NEW37":
        print("PASS P207_TEST_LEDGER_ONLY ledger=37 baselined=32 applied=5 pending=0 drift=0")
        return 0
    if state == "OLD35":
        print("STOP P207_TEST_LEDGER_PENDING ledger=35 baselined=32 applied=3 pending=2", file=sys.stderr)
        return 2
    if state == "PARTIAL36":
        print("STOP P207_TEST_LEDGER_PENDING ledger=36 baselined=32 applied=4 pending=1", file=sys.stderr)
        return 2
    print("STOP P207_GUARDED_LEDGER_UNVERIFIED", file=sys.stderr)
    return 2


def _p207_legacy_private_config_named(tokens: list[str]) -> bool:
    for index, token in enumerate(tokens):
        if not isinstance(token, str):
            continue
        option, separator, value = token.partition("=")
        if option != "--config" and not (
            len(option) > 2 and "--config".startswith(option)
        ):
            continue
        config_path = value if separator else (
            tokens[index + 1] if index + 1 < len(tokens) else None
        )
        if not isinstance(config_path, str):
            continue
        basename = config_path.replace("\\", "/").rsplit("/", 1)[-1]
        if basename.casefold() == _P207_LEGACY_PRIVATE_CONFIG_BASENAME.casefold():
            return True
    return False


def _run_main(argv: list[str] | None = None) -> int:
    from grandgousier.config import application_settings_mapping, read_config_file
    from grandgousier.exit_codes import USAGE

    parser = build_parser()
    args = parser.parse_args(argv)
    config_file = getattr(args, "config_file", None)
    try:
        settings = get_settings(config_file=config_file)
        explicit_config = (
            application_settings_mapping(read_config_file(config_file))
            if config_file is not None
            else {}
        )
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return USAGE
    if args.database_url is None and explicit_config.get("database_url"):
        args.database_url = explicit_config["database_url"]
    configure_logging(args.log_level or settings.log_level)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
