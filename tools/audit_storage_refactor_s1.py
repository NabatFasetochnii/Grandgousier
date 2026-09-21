from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from grandgousier.db.session import make_session_factory


BASE_CV_SQL = text(
    """
    SELECT
        cv.calibration_version_id,
        cv.name,
        cv.instrument_unit_id,
        cv.created_at
    FROM calibration_versions cv
    WHERE cv.product_kind = 'BASE_PHOTOMETRY'
      AND cv.product_role = 'BASE'
      AND cv.lifecycle_status = 'COMPLETE_IMMUTABLE'
    ORDER BY cv.calibration_version_id
    """
)

FRAME_COUNTS_SQL = text(
    """
    SELECT
        m.frame_id,
        COUNT(*)::bigint AS n_rows,
        f.instrument_unit_id,
        f.instrument,
        f.site,
        f.tube,
        f.camera,
        f.filter_name,
        f.date_obs
    FROM measurements m
    JOIN frames f ON f.frame_id = m.frame_id
    WHERE m.calibration_version_id = :cv_id
    GROUP BY
        m.frame_id,
        f.instrument_unit_id,
        f.instrument,
        f.site,
        f.tube,
        f.camera,
        f.filter_name,
        f.date_obs
    ORDER BY m.frame_id
    """
)

BASE_TOTAL_ROWS_SQL = text(
    """
    SELECT COUNT(*)::bigint
    FROM measurements m
    JOIN calibration_versions cv
      ON cv.calibration_version_id = m.calibration_version_id
    WHERE cv.product_kind = 'BASE_PHOTOMETRY'
      AND cv.product_role = 'BASE'
      AND cv.lifecycle_status = 'COMPLETE_IMMUTABLE'
    """
)

FRAME_FLAG_AUDIT_SQL = text(
    r"""
    WITH frame_rows AS (
        SELECT
            m.measurement_id,
            COALESCE(m.flags, '{}'::jsonb) AS flags
        FROM measurements m
        WHERE m.calibration_version_id = :cv_id
          AND m.frame_id = :frame_id
    ),
    n AS (
        SELECT COUNT(*)::bigint AS n_rows
        FROM frame_rows
    ),
    kv AS (
        SELECT
            r.measurement_id,
            e.key,
            e.value,
            pg_column_size(e.value)::bigint AS value_pg_bytes,
            pg_column_size(jsonb_build_object(e.key, e.value))::bigint AS pair_pg_bytes
        FROM frame_rows r
        CROSS JOIN LATERAL jsonb_each(r.flags) AS e(key, value)
    ),
    key_stats AS (
        SELECT
            key,
            COUNT(*)::bigint AS occurrences,
            COUNT(DISTINCT value)::bigint AS distinct_values,
            SUM(value_pg_bytes)::bigint AS repeated_value_pg_bytes,
            SUM(pair_pg_bytes)::bigint AS repeated_pair_pg_bytes,
            MIN(pair_pg_bytes)::bigint AS one_pair_pg_bytes
        FROM kv
        GROUP BY key
    ),
    constant_keys AS (
        SELECT
            kv.key,
            (array_agg(kv.value ORDER BY kv.measurement_id))[1] AS value
        FROM kv
        CROSS JOIN n
        GROUP BY kv.key, n.n_rows
        HAVING COUNT(*) = n.n_rows
           AND COUNT(DISTINCT kv.value) = 1
    ),
    context AS (
        SELECT
            COALESCE(jsonb_object_agg(key, value), '{}'::jsonb) AS context_flags,
            COALESCE(array_agg(key ORDER BY key), ARRAY[]::text[]) AS context_keys
        FROM constant_keys
    ),
    row_sizes AS (
        SELECT
            r.measurement_id,
            pg_column_size(r.flags)::bigint AS current_stored_pg_bytes,
            pg_column_size(r.flags || '{}'::jsonb)::bigint AS current_logical_pg_bytes,
            octet_length(r.flags::text)::bigint AS current_json_text_bytes,
            pg_column_size((r.flags - c.context_keys) || '{}'::jsonb)::bigint
                AS remainder_logical_pg_bytes,
            octet_length((r.flags - c.context_keys)::text)::bigint
                AS remainder_json_text_bytes,
            ((c.context_flags || (r.flags - c.context_keys)) = r.flags) AS exact_merge
        FROM frame_rows r
        CROSS JOIN context c
    ),
    group_metrics AS (
        SELECT
            n.n_rows,
            COUNT(*) FILTER (WHERE rs.exact_merge)::bigint AS exact_merge_rows,
            SUM(rs.current_stored_pg_bytes)::bigint AS current_stored_pg_bytes,
            SUM(rs.current_logical_pg_bytes)::bigint AS current_logical_pg_bytes,
            SUM(rs.current_json_text_bytes)::bigint AS current_json_text_bytes,
            SUM(rs.remainder_logical_pg_bytes)::bigint AS remainder_logical_pg_bytes,
            SUM(rs.remainder_json_text_bytes)::bigint AS remainder_json_text_bytes,
            pg_column_size(c.context_flags || '{}'::jsonb)::bigint AS context_logical_pg_bytes,
            octet_length(c.context_flags::text)::bigint AS context_json_text_bytes,
            COALESCE(array_length(c.context_keys, 1), 0)::bigint AS context_key_count
        FROM n
        CROSS JOIN context c
        LEFT JOIN row_sizes rs ON TRUE
        GROUP BY
            n.n_rows,
            c.context_flags,
            c.context_keys
    )
    SELECT
        gm.n_rows,
        gm.exact_merge_rows,
        gm.current_stored_pg_bytes,
        gm.current_logical_pg_bytes,
        gm.current_json_text_bytes,
        gm.remainder_logical_pg_bytes,
        gm.remainder_json_text_bytes,
        gm.context_logical_pg_bytes,
        gm.context_json_text_bytes,
        gm.context_key_count,
        ks.key,
        ks.occurrences,
        ks.distinct_values,
        ks.repeated_value_pg_bytes,
        ks.repeated_pair_pg_bytes,
        ks.one_pair_pg_bytes,
        CASE
            WHEN ks.key IS NULL THEN FALSE
            ELSE ks.occurrences = gm.n_rows AND ks.distinct_values = 1
        END AS safe_frame_constant
    FROM group_metrics gm
    LEFT JOIN key_stats ks ON TRUE
    ORDER BY
        CASE WHEN ks.key IS NULL THEN 1 ELSE 0 END,
        ks.repeated_pair_pg_bytes DESC NULLS LAST,
        ks.key
    """
)

RELATION_SIZE_SQL = text(
    """
    SELECT
        c.relname AS relation_name,
        pg_relation_size(c.oid)::bigint AS heap_bytes,
        pg_table_size(c.oid)::bigint AS table_bytes,
        pg_indexes_size(c.oid)::bigint AS indexes_bytes,
        pg_total_relation_size(c.oid)::bigint AS total_bytes,
        CASE
            WHEN c.reltoastrelid = 0 THEN 0::bigint
            ELSE pg_total_relation_size(c.reltoastrelid)::bigint
        END AS toast_total_bytes
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = current_schema()
      AND c.relname IN ('measurements', 'science_measurements')
    ORDER BY c.relname
    """
)

SCIENCE_INDEX_SQL = text(
    """
    SELECT
        idx.relname AS index_name,
        pg_relation_size(idx.oid)::bigint AS index_bytes,
        pg_get_indexdef(idx.oid) AS definition
    FROM pg_index i
    JOIN pg_class tbl ON tbl.oid = i.indrelid
    JOIN pg_namespace ns ON ns.oid = tbl.relnamespace
    JOIN pg_class idx ON idx.oid = i.indexrelid
    WHERE ns.nspname = current_schema()
      AND tbl.relname = 'science_measurements'
    ORDER BY idx.relname
    """
)

SCIENCE_CV_SQL = text(
    """
    SELECT
        sm.calibration_version_id,
        cv.name,
        COUNT(*)::bigint AS n_rows,
        MIN(sm.science_measurement_id)::bigint AS min_science_measurement_id,
        MAX(sm.science_measurement_id)::bigint AS max_science_measurement_id
    FROM science_measurements sm
    JOIN calibration_versions cv
      ON cv.calibration_version_id = sm.calibration_version_id
    GROUP BY sm.calibration_version_id, cv.name
    ORDER BY sm.calibration_version_id
    """
)

SCIENCE_PROJECTION_SQL = text(
    r"""
    WITH sample AS (
        SELECT sm.*
        FROM science_measurements sm
        WHERE sm.calibration_version_id = :cv_id
        ORDER BY sm.science_measurement_id
        LIMIT :sample_rows
    )
    SELECT
        COUNT(*)::bigint AS sampled_rows,
        COUNT(*) FILTER (WHERE bm.measurement_id IS NULL)::bigint AS missing_base_rows,
        AVG(pg_column_size(s))::double precision AS current_row_pg_bytes_avg,
        AVG(pg_column_size(ROW(
            s.science_measurement_id,
            s.calibration_version_id,
            s.base_measurement_id,
            s.object_id,
            s.frame_id,
            s.flux,
            s.flux_err,
            s.mag_inst,
            s.mag_cal,
            s.mag_err,
            s.mag_err_random,
            s.mag_err_systematic,
            s.local_zp_correction,
            s.flags,
            s.materialization_source,
            s.created_at,
            s.updated_at
        )))::double precision AS v2_core_row_pg_bytes_avg,
        AVG(pg_column_size(ROW(
            s.science_measurement_id,
            s.calibration_version_id,
            s.source_measurement_id,
            s.base_measurement_id,
            s.object_id,
            s.frame_id,
            s.flux,
            s.flux_err,
            s.mag_inst,
            s.mag_cal,
            s.mag_err,
            s.mag_err_random,
            s.mag_err_systematic,
            s.local_zp_correction,
            s.flags,
            s.materialization_source,
            s.created_at,
            s.updated_at
        )))::double precision AS v2_compat_row_pg_bytes_avg,
        AVG(pg_column_size(s.flags))::double precision AS flags_pg_bytes_avg,
        COUNT(*) FILTER (WHERE s.x IS NOT DISTINCT FROM bm.x)::bigint AS x_equal_rows,
        COUNT(*) FILTER (WHERE s.y IS NOT DISTINCT FROM bm.y)::bigint AS y_equal_rows,
        COUNT(*) FILTER (
            WHERE s.aperture_radius_pix IS NOT DISTINCT FROM bm.aperture_radius_pix
        )::bigint AS aperture_equal_rows,
        COUNT(*) FILTER (
            WHERE s.local_background IS NOT DISTINCT FROM bm.local_background
        )::bigint AS background_equal_rows,
        COUNT(*) FILTER (
            WHERE s.centroid_dx_pix IS NOT DISTINCT FROM bm.centroid_dx_pix
        )::bigint AS centroid_dx_equal_rows,
        COUNT(*) FILTER (
            WHERE s.centroid_dy_pix IS NOT DISTINCT FROM bm.centroid_dy_pix
        )::bigint AS centroid_dy_equal_rows
    FROM sample s
    LEFT JOIN measurements bm
      ON bm.measurement_id = s.base_measurement_id
    """
)


@dataclass(frozen=True)
class SelectedFrame:
    calibration_version_id: int
    calibration_name: str
    frame_id: int
    n_rows: int
    instrument_unit_id: int | None
    instrument: str | None
    site: str | None
    tube: str | None
    camera: str | None
    filter_name: str | None
    date_obs: Any


@dataclass
class KeyAccumulator:
    groups_present: int = 0
    groups_safe_constant: int = 0
    groups_varying: int = 0
    groups_sparse: int = 0
    occurrences: int = 0
    rows_in_present_groups: int = 0
    repeated_value_pg_bytes: int = 0
    repeated_pair_pg_bytes: int = 0
    projected_pair_pg_bytes: int = 0
    removable_pair_pg_bytes: int = 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only S1 storage audit for Grandgousier 0.6.3.2: complete-frame BASE "
            "flag factoring plus a narrow ScienceMeasurement V2 row-width projection."
        )
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="SQLAlchemy PostgreSQL URL; GRANDGOUSIER_DATABASE_URL/settings fallback is used when omitted.",
    )
    parser.add_argument(
        "--out-root",
        default="reports/storage_refactor_s1_0632",
        help="Directory for CSV/JSON/Markdown reports.",
    )
    parser.add_argument(
        "--frames",
        type=int,
        default=80,
        help="Target number of complete BASE frames to audit (default: 80).",
    )
    parser.add_argument(
        "--max-calibrations",
        type=int,
        default=10,
        help="Maximum BASE calibration versions used for automatic stratification (default: 10).",
    )
    parser.add_argument(
        "--calibration-version-id",
        dest="calibration_version_ids",
        action="append",
        type=int,
        default=[],
        help="Explicit BASE calibration version id. Repeat to override automatic calibration selection.",
    )
    parser.add_argument(
        "--statement-timeout-ms",
        type=int,
        default=300_000,
        help="PostgreSQL statement timeout applied locally to the read-only transaction (default: 300000).",
    )
    parser.add_argument(
        "--science-calibrations",
        type=int,
        default=8,
        help="Number of science calibration versions for V2 row-width projection (default: 8).",
    )
    parser.add_argument(
        "--science-sample-rows",
        type=int,
        default=20_000,
        help="Maximum science rows sampled per selected calibration (default: 20000).",
    )
    parser.add_argument(
        "--skip-science-projection",
        action="store_true",
        help="Skip ScienceMeasurement V2 row-width/index projection.",
    )
    return parser


def _evenly_spaced(items: list[Any], count: int) -> list[Any]:
    if count <= 0 or not items:
        return []
    if count >= len(items):
        return list(items)
    if count == 1:
        return [items[len(items) // 2]]
    indices = []
    for i in range(count):
        pos = round(i * (len(items) - 1) / (count - 1))
        if pos not in indices:
            indices.append(pos)
    return [items[index] for index in indices]


def _choose_typical_frames(rows: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    """Choose complete frames across the central row-count distribution.

    Sorting by n_rows first deliberately rejects tiny/huge tails when enough frames
    exist.  We then spread selections through the central 70% so the audit does not
    mistake a handful of unusually sparse frames for a representative BASE workload.
    """

    if count <= 0 or not rows:
        return []
    if len(rows) <= count:
        return sorted(rows, key=lambda row: int(row["frame_id"]))

    ordered = sorted(rows, key=lambda row: (int(row["n_rows"]), int(row["frame_id"])))
    if len(ordered) >= 10:
        lo = int(math.floor((len(ordered) - 1) * 0.15))
        hi = int(math.ceil((len(ordered) - 1) * 0.85)) + 1
        central = ordered[lo:hi]
    else:
        central = ordered
    chosen = _evenly_spaced(central, min(count, len(central)))
    return sorted(chosen, key=lambda row: int(row["frame_id"]))


def _allocate(total: int, buckets: int) -> list[int]:
    if buckets <= 0:
        return []
    total = max(total, buckets)
    base, extra = divmod(total, buckets)
    return [base + (1 if i < extra else 0) for i in range(buckets)]


def _write_csv(path: Path, rows: Iterable[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _safe_ratio(numerator: float | int, denominator: float | int) -> float | None:
    if not denominator:
        return None
    return float(numerator) / float(denominator)


def _classification(acc: KeyAccumulator) -> str:
    if acc.groups_present == 0:
        return "NO_DATA"
    if acc.groups_varying == 0 and acc.groups_sparse == 0:
        return "FRAME_CONSTANT_IN_ALL_PRESENT_GROUPS"
    safe_ratio = acc.groups_safe_constant / acc.groups_present
    if acc.groups_varying == 0 and safe_ratio >= 0.95:
        return "MOSTLY_FRAME_CONSTANT_WITH_SPARSE_GROUPS"
    if safe_ratio >= 0.95:
        return "MOSTLY_FRAME_CONSTANT_WITH_EXCEPTIONS"
    if acc.groups_varying > 0:
        return "OBJECT_VARYING_IN_SAMPLE"
    return "SPARSE_OR_INCONSISTENT"


def _owner_hint(key: str) -> str:
    calibration_parameter_keys = {
        "annulus_inner_pix",
        "annulus_outer_pix",
        "zero_point",
        "photometry_stage",
        "photometry_estimator_requested",
        "random_error_model",
        "random_error_model_semantics",
    }
    frame_or_instrument_keys = {
        "random_error_gain_source",
        "random_error_read_noise_source",
        "random_error_gain_e_per_adu",
        "random_error_read_noise_e",
        "random_error_airmass",
        "random_error_exptime_s",
        "random_error_scintillation_coefficient",
        "random_error_telescope_aperture_m",
        "random_error_site_altitude_m",
    }
    object_identity_keys = {
        "catalog_name",
        "catalog_source_id",
        "source_id",
    }
    if key in calibration_parameter_keys:
        return "CALIBRATION_VERSION_PARAMETERS_CANDIDATE"
    if key in frame_or_instrument_keys:
        return "FRAME_OR_INSTRUMENT_CONTEXT_CANDIDATE"
    if key in object_identity_keys:
        return "OBJECT_CATALOG_IDENTITY;_DO_NOT_FACTOR_BY_CONSTANCY_ALONE"
    if key.startswith("random_error_"):
        return "MIXED_ERROR_MODEL_DIAGNOSTIC;SEMANTIC_AUDIT_REQUIRED"
    if key.startswith("psf_") or key.startswith("adaptive_") or key.startswith("empirical_psf_"):
        return "PHOTOMETRY_DIAGNOSTIC;SEMANTIC_AUDIT_REQUIRED"
    return "UNCLASSIFIED;SEMANTIC_AUDIT_REQUIRED"


def _select_base_calibrations(session, explicit: list[int], max_calibrations: int) -> list[dict[str, Any]]:
    rows = [dict(row) for row in session.execute(BASE_CV_SQL).mappings().all()]
    if explicit:
        by_id = {int(row["calibration_version_id"]): row for row in rows}
        missing = [value for value in explicit if value not in by_id]
        if missing:
            raise ValueError(
                "Explicit calibration version(s) are not COMPLETE_IMMUTABLE BASE_PHOTOMETRY/BASE: "
                + ", ".join(str(value) for value in missing)
            )
        return [by_id[value] for value in explicit]
    return _evenly_spaced(rows, max(1, int(max_calibrations)))


def _select_complete_frames(session, calibrations: list[dict[str, Any]], target_frames: int) -> list[SelectedFrame]:
    allocations = _allocate(max(1, int(target_frames)), len(calibrations))
    selected: list[SelectedFrame] = []
    for cv, wanted in zip(calibrations, allocations, strict=True):
        cv_id = int(cv["calibration_version_id"])
        frame_rows = [
            dict(row)
            for row in session.execute(FRAME_COUNTS_SQL, {"cv_id": cv_id}).mappings().all()
        ]
        for row in _choose_typical_frames(frame_rows, wanted):
            selected.append(
                SelectedFrame(
                    calibration_version_id=cv_id,
                    calibration_name=str(cv["name"]),
                    frame_id=int(row["frame_id"]),
                    n_rows=int(row["n_rows"]),
                    instrument_unit_id=(
                        int(row["instrument_unit_id"])
                        if row["instrument_unit_id"] is not None
                        else None
                    ),
                    instrument=row.get("instrument"),
                    site=row.get("site"),
                    tube=row.get("tube"),
                    camera=row.get("camera"),
                    filter_name=row.get("filter_name"),
                    date_obs=row.get("date_obs"),
                )
            )
    return selected


def _audit_base_flags(session, frames: list[SelectedFrame]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    group_rows: list[dict[str, Any]] = []
    per_key_group_rows: list[dict[str, Any]] = []
    key_acc: dict[str, KeyAccumulator] = defaultdict(KeyAccumulator)

    totals = defaultdict(int)
    for ordinal, frame in enumerate(frames, start=1):
        result = [
            dict(row)
            for row in session.execute(
                FRAME_FLAG_AUDIT_SQL,
                {
                    "cv_id": int(frame.calibration_version_id),
                    "frame_id": int(frame.frame_id),
                },
            ).mappings().all()
        ]
        if not result:
            raise RuntimeError(
                f"No audit row returned for cv={frame.calibration_version_id} frame={frame.frame_id}"
            )
        first = result[0]
        n_rows = int(first["n_rows"] or 0)
        exact_rows = int(first["exact_merge_rows"] or 0)
        if n_rows != frame.n_rows:
            raise RuntimeError(
                f"Frame row count changed during audit: cv={frame.calibration_version_id} "
                f"frame={frame.frame_id} selected={frame.n_rows} audited={n_rows}"
            )
        if exact_rows != n_rows:
            raise RuntimeError(
                f"Exact merge parity failed: cv={frame.calibration_version_id} "
                f"frame={frame.frame_id} exact={exact_rows}/{n_rows}"
            )

        current_logical = int(first["current_logical_pg_bytes"] or 0)
        remainder_logical = int(first["remainder_logical_pg_bytes"] or 0)
        context_logical = int(first["context_logical_pg_bytes"] or 0)
        projected_logical = remainder_logical + context_logical
        current_text = int(first["current_json_text_bytes"] or 0)
        remainder_text = int(first["remainder_json_text_bytes"] or 0)
        context_text = int(first["context_json_text_bytes"] or 0)
        projected_text = remainder_text + context_text

        group_row = {
            "ordinal": ordinal,
            "calibration_version_id": frame.calibration_version_id,
            "calibration_name": frame.calibration_name,
            "frame_id": frame.frame_id,
            "n_rows": n_rows,
            "instrument_unit_id": frame.instrument_unit_id,
            "instrument": frame.instrument,
            "site": frame.site,
            "tube": frame.tube,
            "camera": frame.camera,
            "filter_name": frame.filter_name,
            "date_obs": frame.date_obs,
            "context_key_count": int(first["context_key_count"] or 0),
            "exact_merge_rows": exact_rows,
            "current_stored_pg_bytes": int(first["current_stored_pg_bytes"] or 0),
            "current_logical_pg_bytes": current_logical,
            "context_logical_pg_bytes": context_logical,
            "remainder_logical_pg_bytes": remainder_logical,
            "projected_logical_pg_bytes": projected_logical,
            "logical_savings_pg_bytes": current_logical - projected_logical,
            "logical_savings_pg_fraction": _safe_ratio(current_logical - projected_logical, current_logical),
            "current_json_text_bytes": current_text,
            "context_json_text_bytes": context_text,
            "remainder_json_text_bytes": remainder_text,
            "projected_json_text_bytes": projected_text,
            "json_text_savings_bytes": current_text - projected_text,
            "json_text_savings_fraction": _safe_ratio(current_text - projected_text, current_text),
        }
        group_rows.append(group_row)

        for key_row in result:
            key = key_row.get("key")
            if key is None:
                continue
            key = str(key)
            occurrences = int(key_row["occurrences"] or 0)
            distinct_values = int(key_row["distinct_values"] or 0)
            safe = bool(key_row["safe_frame_constant"])
            repeated_value = int(key_row["repeated_value_pg_bytes"] or 0)
            repeated_pair = int(key_row["repeated_pair_pg_bytes"] or 0)
            one_pair = int(key_row["one_pair_pg_bytes"] or 0)
            projected_pair = one_pair if safe else repeated_pair
            removable_pair = repeated_pair - projected_pair

            per_key_group_rows.append(
                {
                    "calibration_version_id": frame.calibration_version_id,
                    "calibration_name": frame.calibration_name,
                    "frame_id": frame.frame_id,
                    "n_rows": n_rows,
                    "key": key,
                    "occurrences": occurrences,
                    "occurrence_fraction": _safe_ratio(occurrences, n_rows),
                    "distinct_values": distinct_values,
                    "safe_frame_constant": safe,
                    "repeated_value_pg_bytes": repeated_value,
                    "repeated_pair_pg_bytes": repeated_pair,
                    "projected_pair_pg_bytes": projected_pair,
                    "removable_pair_pg_bytes": removable_pair,
                    "owner_hint": _owner_hint(key),
                }
            )

            acc = key_acc[key]
            acc.groups_present += 1
            acc.occurrences += occurrences
            acc.rows_in_present_groups += n_rows
            acc.repeated_value_pg_bytes += repeated_value
            acc.repeated_pair_pg_bytes += repeated_pair
            acc.projected_pair_pg_bytes += projected_pair
            acc.removable_pair_pg_bytes += removable_pair
            if safe:
                acc.groups_safe_constant += 1
            elif occurrences < n_rows:
                acc.groups_sparse += 1
            if distinct_values > 1:
                acc.groups_varying += 1

        for name in (
            "n_rows",
            "current_stored_pg_bytes",
            "current_logical_pg_bytes",
            "context_logical_pg_bytes",
            "remainder_logical_pg_bytes",
            "projected_logical_pg_bytes",
            "logical_savings_pg_bytes",
            "current_json_text_bytes",
            "context_json_text_bytes",
            "remainder_json_text_bytes",
            "projected_json_text_bytes",
            "json_text_savings_bytes",
        ):
            totals[name] += int(group_row[name] or 0)

        print(
            f"[{ordinal:03d}/{len(frames):03d}] cv={frame.calibration_version_id} "
            f"frame={frame.frame_id} rows={n_rows} context_keys={group_row['context_key_count']} "
            f"logical_saving={group_row['logical_savings_pg_fraction'] or 0:.1%}"
        )

    key_rows: list[dict[str, Any]] = []
    total_groups = len(frames)
    for key, acc in sorted(
        key_acc.items(),
        key=lambda item: (-item[1].removable_pair_pg_bytes, item[0]),
    ):
        key_rows.append(
            {
                "key": key,
                "sampled_groups_total": total_groups,
                "groups_present": acc.groups_present,
                "groups_present_fraction": _safe_ratio(acc.groups_present, total_groups),
                "groups_safe_constant": acc.groups_safe_constant,
                "safe_constant_fraction_of_present": _safe_ratio(acc.groups_safe_constant, acc.groups_present),
                "groups_varying": acc.groups_varying,
                "groups_sparse": acc.groups_sparse,
                "occurrences": acc.occurrences,
                "occurrence_fraction_within_present_groups": _safe_ratio(acc.occurrences, acc.rows_in_present_groups),
                "repeated_value_pg_bytes": acc.repeated_value_pg_bytes,
                "repeated_pair_pg_bytes": acc.repeated_pair_pg_bytes,
                "projected_pair_pg_bytes": acc.projected_pair_pg_bytes,
                "removable_pair_pg_bytes": acc.removable_pair_pg_bytes,
                "classification": _classification(acc),
                "owner_hint": _owner_hint(key),
            }
        )

    summary = {
        "sampled_frames": len(frames),
        "sampled_rows": totals["n_rows"],
        "current_stored_pg_bytes": totals["current_stored_pg_bytes"],
        "current_logical_pg_bytes": totals["current_logical_pg_bytes"],
        "projected_logical_pg_bytes": totals["projected_logical_pg_bytes"],
        "logical_savings_pg_bytes": totals["logical_savings_pg_bytes"],
        "logical_savings_pg_fraction": _safe_ratio(
            totals["logical_savings_pg_bytes"], totals["current_logical_pg_bytes"]
        ),
        "current_json_text_bytes": totals["current_json_text_bytes"],
        "projected_json_text_bytes": totals["projected_json_text_bytes"],
        "json_text_savings_bytes": totals["json_text_savings_bytes"],
        "json_text_savings_fraction": _safe_ratio(
            totals["json_text_savings_bytes"], totals["current_json_text_bytes"]
        ),
        "exact_merge_all_rows": True,
    }
    return group_rows, key_rows, {"summary": summary, "per_key_group_rows": per_key_group_rows}


def _science_projection(session, *, calibration_count: int, sample_rows: int) -> dict[str, Any]:
    cv_rows = [dict(row) for row in session.execute(SCIENCE_CV_SQL).mappings().all()]
    selected = _evenly_spaced(cv_rows, max(1, int(calibration_count))) if cv_rows else []
    projections: list[dict[str, Any]] = []
    weighted = defaultdict(float)
    total_sampled = 0
    for cv in selected:
        cv_id = int(cv["calibration_version_id"])
        projection = dict(
            session.execute(
                SCIENCE_PROJECTION_SQL,
                {"cv_id": cv_id, "sample_rows": max(1, int(sample_rows))},
            ).mappings().one()
        )
        sampled = int(projection["sampled_rows"] or 0)
        row = {
            "calibration_version_id": cv_id,
            "calibration_name": cv["name"],
            "calibration_rows": int(cv["n_rows"] or 0),
            **projection,
        }
        for field in (
            "x_equal_rows",
            "y_equal_rows",
            "aperture_equal_rows",
            "background_equal_rows",
            "centroid_dx_equal_rows",
            "centroid_dy_equal_rows",
        ):
            row[field.replace("_rows", "_fraction")] = _safe_ratio(int(row[field] or 0), sampled)
        if row.get("current_row_pg_bytes_avg"):
            row["v2_core_reduction_fraction"] = 1.0 - (
                float(row["v2_core_row_pg_bytes_avg"]) / float(row["current_row_pg_bytes_avg"])
            )
            row["v2_compat_reduction_fraction"] = 1.0 - (
                float(row["v2_compat_row_pg_bytes_avg"]) / float(row["current_row_pg_bytes_avg"])
            )
        else:
            row["v2_core_reduction_fraction"] = None
            row["v2_compat_reduction_fraction"] = None
        projections.append(row)

        total_sampled += sampled
        for field in (
            "current_row_pg_bytes_avg",
            "v2_core_row_pg_bytes_avg",
            "v2_compat_row_pg_bytes_avg",
            "flags_pg_bytes_avg",
        ):
            if row.get(field) is not None:
                weighted[field] += float(row[field]) * sampled

    indexes = [dict(row) for row in session.execute(SCIENCE_INDEX_SQL).mappings().all()]
    relation_sizes = [dict(row) for row in session.execute(RELATION_SIZE_SQL).mappings().all()]
    weighted_avg = {
        field: (value / total_sampled if total_sampled else None)
        for field, value in weighted.items()
    }
    current_index_bytes = sum(int(row["index_bytes"] or 0) for row in indexes)
    science_total_rows = sum(int(row["n_rows"] or 0) for row in cv_rows)
    return {
        "science_calibration_versions": len(cv_rows),
        "science_total_rows": science_total_rows,
        "selected_calibrations": len(selected),
        "sampled_rows": total_sampled,
        "weighted_avg": weighted_avg,
        "projection_rows": projections,
        "current_science_index_bytes": current_index_bytes,
        "projected_v2_index_bytes_same_keyset": current_index_bytes,
        "index_projection_note": (
            "V2 keeps the same PK/UNIQUE/calibration-frame key widths in this conservative projection; "
            "therefore current science index bytes are the first-order estimate. Rebuild/prototype measurement "
            "is required for a physical estimate."
        ),
        "relation_sizes": relation_sizes,
        "indexes": indexes,
    }


def _format_bytes(value: int | float | None) -> str:
    if value is None:
        return "n/a"
    size = float(value)
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    unit = units[0]
    for candidate in units:
        unit = candidate
        if abs(size) < 1024.0 or candidate == units[-1]:
            break
        size /= 1024.0
    return f"{size:.3f} {unit}"


def _write_markdown(path: Path, report: dict[str, Any]) -> None:
    base = report["base_flags"]
    sci = report.get("science_v2")
    lines = [
        "# Grandgousier S1 read-only storage audit",
        "",
        f"Generated: {report['generated_at_utc']}",
        "",
        "## BASE flags — complete-frame sample",
        "",
        f"- sampled frames: {base['sampled_frames']}",
        f"- sampled rows: {base['sampled_rows']:,}",
        f"- exact merge parity: {'YES' if base['exact_merge_all_rows'] else 'NO'}",
        f"- current logical JSONB datum bytes: {_format_bytes(base['current_logical_pg_bytes'])}",
        f"- projected frame-context + object remainder: {_format_bytes(base['projected_logical_pg_bytes'])}",
        f"- sampled logical saving: {_format_bytes(base['logical_savings_pg_bytes'])} "
        f"({(base['logical_savings_pg_fraction'] or 0):.2%})",
        f"- exact BASE rows in production selection: {report['base_total_rows']:,}",
        f"- sample-extrapolated BASE flag saving: {_format_bytes(report['base_extrapolated_savings_bytes'])}",
        "",
        "The extrapolation is a sample estimate, not a physical relation-size forecast. Actual heap/TOAST savings "
        "must be measured on an additive prototype because PostgreSQL compression/TOAST thresholds are nonlinear.",
        "",
        "## Largest flag families by removable repeated pair bytes",
        "",
        "| key | classification | safe groups / present | removable pair bytes | owner hint |",
        "|---|---|---:|---:|---|",
    ]
    for row in report["top_keys"][:20]:
        lines.append(
            f"| `{row['key']}` | {row['classification']} | "
            f"{row['groups_safe_constant']}/{row['groups_present']} | "
            f"{_format_bytes(row['removable_pair_pg_bytes'])} | {row['owner_hint']} |"
        )
    if sci:
        weighted = sci["weighted_avg"]
        current = weighted.get("current_row_pg_bytes_avg")
        v2_core = weighted.get("v2_core_row_pg_bytes_avg")
        v2_compat = weighted.get("v2_compat_row_pg_bytes_avg")
        lines.extend(
            [
                "",
                "## ScienceMeasurement V2 logical row-width projection",
                "",
                f"- science rows: {sci['science_total_rows']:,}",
                f"- sampled rows: {sci['sampled_rows']:,}",
                f"- current sampled row datum: {current:.1f} B" if current is not None else "- current sampled row datum: n/a",
                f"- V2 core sampled row datum: {v2_core:.1f} B" if v2_core is not None else "- V2 core sampled row datum: n/a",
                f"- V2 compatibility sampled row datum: {v2_compat:.1f} B" if v2_compat is not None else "- V2 compatibility sampled row datum: n/a",
                f"- current index bytes: {_format_bytes(sci['current_science_index_bytes'])}",
                "",
                "V2 core omits `source_measurement_id` and the six BASE-inherited extraction/geometric columns. "
                "V2 compatibility keeps `source_measurement_id` but still inherits the six duplicate columns. "
                "This is a logical row projection only; it is not an authorization to drop V1 storage.",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.frames < 1:
        raise SystemExit("--frames must be >= 1")
    if args.max_calibrations < 1:
        raise SystemExit("--max-calibrations must be >= 1")
    if args.science_calibrations < 1:
        raise SystemExit("--science-calibrations must be >= 1")
    if args.science_sample_rows < 1:
        raise SystemExit("--science-sample-rows must be >= 1")

    out_root = Path(args.out_root)
    out_root.mkdir(parents=True, exist_ok=True)

    Session = make_session_factory(args.database_url)
    with Session() as session:
        dialect = session.get_bind().dialect.name
        if dialect != "postgresql":
            raise RuntimeError(f"This audit requires PostgreSQL; connected dialect={dialect!r}")
        # This must be the first transactional command.  The script never issues
        # INSERT/UPDATE/DELETE/DDL; PostgreSQL additionally enforces read-only mode.
        session.execute(text("SET TRANSACTION READ ONLY"))
        session.execute(
            text("SELECT set_config('statement_timeout', :timeout, true)"),
            {"timeout": f"{int(args.statement_timeout_ms)}ms"},
        )

        base_total_rows = int(session.scalar(BASE_TOTAL_ROWS_SQL) or 0)
        calibrations = _select_base_calibrations(
            session,
            list(dict.fromkeys(int(value) for value in args.calibration_version_ids)),
            int(args.max_calibrations),
        )
        if not calibrations:
            raise RuntimeError("No COMPLETE_IMMUTABLE BASE_PHOTOMETRY/BASE calibration versions found")
        frames = _select_complete_frames(session, calibrations, int(args.frames))
        if not frames:
            raise RuntimeError("No complete BASE frames selected")

        print(
            f"BASE audit: calibrations={len(calibrations)} complete_frames={len(frames)} "
            f"target={args.frames} exact_base_rows={base_total_rows}"
        )
        group_rows, key_rows, raw = _audit_base_flags(session, frames)
        base_summary = dict(raw["summary"])
        sampled_rows = int(base_summary["sampled_rows"] or 0)
        savings_per_row = (
            float(base_summary["logical_savings_pg_bytes"]) / sampled_rows
            if sampled_rows
            else 0.0
        )
        extrapolated = int(round(savings_per_row * base_total_rows))

        science = None
        if not args.skip_science_projection:
            print("Science V2 projection: reading representative immutable materializations...")
            science = _science_projection(
                session,
                calibration_count=int(args.science_calibrations),
                sample_rows=int(args.science_sample_rows),
            )

        # No commit: close rolls the read-only transaction back.

    _write_csv(
        out_root / "base_frame_groups.csv",
        group_rows,
        [
            "ordinal",
            "calibration_version_id",
            "calibration_name",
            "frame_id",
            "n_rows",
            "instrument_unit_id",
            "instrument",
            "site",
            "tube",
            "camera",
            "filter_name",
            "date_obs",
            "context_key_count",
            "exact_merge_rows",
            "current_stored_pg_bytes",
            "current_logical_pg_bytes",
            "context_logical_pg_bytes",
            "remainder_logical_pg_bytes",
            "projected_logical_pg_bytes",
            "logical_savings_pg_bytes",
            "logical_savings_pg_fraction",
            "current_json_text_bytes",
            "context_json_text_bytes",
            "remainder_json_text_bytes",
            "projected_json_text_bytes",
            "json_text_savings_bytes",
            "json_text_savings_fraction",
        ],
    )
    _write_csv(
        out_root / "base_flag_keys.csv",
        key_rows,
        [
            "key",
            "sampled_groups_total",
            "groups_present",
            "groups_present_fraction",
            "groups_safe_constant",
            "safe_constant_fraction_of_present",
            "groups_varying",
            "groups_sparse",
            "occurrences",
            "occurrence_fraction_within_present_groups",
            "repeated_value_pg_bytes",
            "repeated_pair_pg_bytes",
            "projected_pair_pg_bytes",
            "removable_pair_pg_bytes",
            "classification",
            "owner_hint",
        ],
    )
    _write_csv(
        out_root / "base_flag_key_groups.csv",
        raw["per_key_group_rows"],
        [
            "calibration_version_id",
            "calibration_name",
            "frame_id",
            "n_rows",
            "key",
            "occurrences",
            "occurrence_fraction",
            "distinct_values",
            "safe_frame_constant",
            "repeated_value_pg_bytes",
            "repeated_pair_pg_bytes",
            "projected_pair_pg_bytes",
            "removable_pair_pg_bytes",
            "owner_hint",
        ],
    )
    if science:
        _write_csv(
            out_root / "science_v2_projection.csv",
            science["projection_rows"],
            [
                "calibration_version_id",
                "calibration_name",
                "calibration_rows",
                "sampled_rows",
                "missing_base_rows",
                "current_row_pg_bytes_avg",
                "v2_core_row_pg_bytes_avg",
                "v2_compat_row_pg_bytes_avg",
                "flags_pg_bytes_avg",
                "v2_core_reduction_fraction",
                "v2_compat_reduction_fraction",
                "x_equal_rows",
                "x_equal_fraction",
                "y_equal_rows",
                "y_equal_fraction",
                "aperture_equal_rows",
                "aperture_equal_fraction",
                "background_equal_rows",
                "background_equal_fraction",
                "centroid_dx_equal_rows",
                "centroid_dx_equal_fraction",
                "centroid_dy_equal_rows",
                "centroid_dy_equal_fraction",
            ],
        )
        _write_csv(
            out_root / "relation_sizes.csv",
            science["relation_sizes"],
            [
                "relation_name",
                "heap_bytes",
                "table_bytes",
                "indexes_bytes",
                "total_bytes",
                "toast_total_bytes",
            ],
        )
        _write_csv(
            out_root / "science_indexes.csv",
            science["indexes"],
            ["index_name", "index_bytes", "definition"],
        )

    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "READ_ONLY",
        "database_writes": False,
        "selection": {
            "target_frames": int(args.frames),
            "selected_frames": len(frames),
            "selected_calibration_versions": [
                {
                    "calibration_version_id": int(row["calibration_version_id"]),
                    "name": str(row["name"]),
                }
                for row in calibrations
            ],
            "selection_method": (
                "calibration versions evenly spaced by BASE calibration_version_id; complete frames spread "
                "through central 70% of each selected calibration's frame row-count distribution"
            ),
        },
        "base_total_rows": base_total_rows,
        "base_flags": base_summary,
        "base_sample_savings_per_row_bytes": savings_per_row,
        "base_extrapolated_savings_bytes": extrapolated,
        "base_extrapolation_warning": (
            "Sample extrapolation only. JSONB compression/TOAST/page effects are nonlinear; measure physical "
            "savings on an additive prototype before any migration."
        ),
        "top_keys": key_rows[:50],
        "science_v2": science,
    }
    (out_root / "summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    _write_markdown(out_root / "summary.md", report)

    print(f"Report root: {out_root}")
    print(
        "BASE sampled logical saving: "
        f"{_format_bytes(base_summary['logical_savings_pg_bytes'])} "
        f"({(base_summary['logical_savings_pg_fraction'] or 0):.2%}); "
        f"sample extrapolation={_format_bytes(extrapolated)}"
    )
    if science:
        weighted = science["weighted_avg"]
        print(
            "Science sampled row datum: "
            f"current={weighted.get('current_row_pg_bytes_avg') or 0:.1f} B, "
            f"v2_core={weighted.get('v2_core_row_pg_bytes_avg') or 0:.1f} B, "
            f"v2_compat={weighted.get('v2_compat_row_pg_bytes_avg') or 0:.1f} B"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
