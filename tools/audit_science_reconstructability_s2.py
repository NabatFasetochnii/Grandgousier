from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import select, text
from sqlalchemy.orm import aliased

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from grandgousier.calibration.reconstruction import (
    _RECONSTRUCTED_FIELDS,
    _chain_from_base,
    _latest_iteration,
    reconstruct_calibration_measurement_states,
)
from grandgousier.db.models import CalibrationVersion, Measurement, ScienceMeasurement
from grandgousier.db.session import make_session_factory
from grandgousier.photometry.science_flags import merge_effective_science_flags


CV_INVENTORY_SQL = text(
    """
    SELECT
        cv.calibration_version_id,
        cv.name,
        cv.logical_name,
        cv.generation_no,
        cv.product_kind,
        cv.product_role,
        cv.materialization_kind,
        cv.lifecycle_status,
        cv.source_calibration_version_id,
        cv.series_id,
        cv.field_id,
        COUNT(sm.science_measurement_id)::bigint AS n_rows,
        COUNT(DISTINCT sm.frame_id)::bigint AS n_frames,
        COUNT(DISTINCT sm.object_id)::bigint AS n_objects,
        COUNT(*) FILTER (WHERE sm.base_measurement_id IS NULL)::bigint AS missing_base_id,
        COUNT(*) FILTER (WHERE bm.measurement_id IS NULL)::bigint AS missing_base_row,
        COUNT(*) FILTER (
            WHERE bm.measurement_id IS NOT NULL
              AND (bm.object_id IS DISTINCT FROM sm.object_id OR bm.frame_id IS DISTINCT FROM sm.frame_id)
        )::bigint AS base_identity_mismatch,
        COUNT(*) FILTER (
            WHERE sm.mag_err IS DISTINCT FROM sm.mag_err_random
        )::bigint AS mag_err_diff_random,
        AVG(pg_column_size(COALESCE(sm.flags, '{}'::jsonb)))::float8 AS avg_science_flags_pg_bytes,
        AVG(octet_length(COALESCE(sm.flags, '{}'::jsonb)::text))::float8 AS avg_science_flags_text_bytes
    FROM science_measurements sm
    JOIN calibration_versions cv
      ON cv.calibration_version_id = sm.calibration_version_id
    LEFT JOIN measurements bm
      ON bm.measurement_id = sm.base_measurement_id
    GROUP BY
        cv.calibration_version_id,
        cv.name,
        cv.logical_name,
        cv.generation_no,
        cv.product_kind,
        cv.product_role,
        cv.materialization_kind,
        cv.lifecycle_status,
        cv.source_calibration_version_id,
        cv.series_id,
        cv.field_id
    ORDER BY cv.calibration_version_id
    """
)

MATERIALIZATION_SOURCES_SQL = text(
    """
    SELECT
        calibration_version_id,
        materialization_source,
        COUNT(*)::bigint AS n_rows
    FROM science_measurements
    GROUP BY calibration_version_id, materialization_source
    ORDER BY calibration_version_id, n_rows DESC, materialization_source
    """
)

FRAME_COUNTS_SQL = text(
    """
    SELECT frame_id, COUNT(*)::bigint AS n_rows
    FROM science_measurements
    WHERE calibration_version_id = :cv_id
    GROUP BY frame_id
    ORDER BY frame_id
    """
)

GLOBAL_COUNTS_SQL = text(
    """
    SELECT
        COUNT(*)::bigint AS science_rows,
        COUNT(DISTINCT sm.base_measurement_id)::bigint AS distinct_base_measurement_ids,
        COUNT(*) FILTER (WHERE sm.base_measurement_id IS NULL)::bigint AS missing_base_id,
        COUNT(*) FILTER (WHERE bm.measurement_id IS NULL)::bigint AS missing_base_row,
        COUNT(*) FILTER (
            WHERE bm.measurement_id IS NOT NULL
              AND (bm.object_id IS DISTINCT FROM sm.object_id OR bm.frame_id IS DISTINCT FROM sm.frame_id)
        )::bigint AS base_identity_mismatch,
        COUNT(*) FILTER (WHERE sm.mag_err IS DISTINCT FROM sm.mag_err_random)::bigint AS mag_err_diff_random
    FROM science_measurements sm
    LEFT JOIN measurements bm ON bm.measurement_id = sm.base_measurement_id
    """
)

RELATION_SIZE_SQL = text(
    """
    SELECT
        c.relname AS relation_name,
        pg_relation_size(c.oid)::bigint AS heap_bytes,
        pg_indexes_size(c.oid)::bigint AS index_bytes,
        CASE
            WHEN c.reltoastrelid = 0 THEN 0::bigint
            ELSE pg_total_relation_size(c.reltoastrelid)::bigint
        END AS toast_total_bytes,
        pg_total_relation_size(c.oid)::bigint AS total_bytes
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = current_schema()
      AND c.relname IN ('science_measurements', 'measurements')
    ORDER BY c.relname
    """
)


@dataclass(slots=True)
class ScalarStats:
    checked: int = 0
    mismatches: int = 0
    max_abs_delta: float = 0.0


def _finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _equal_scalar(predicted: Any, actual: Any, tolerance: float) -> tuple[bool, float | None]:
    if predicted is None or actual is None:
        return predicted is None and actual is None, None
    try:
        p = float(predicted)
        a = float(actual)
    except (TypeError, ValueError):
        return predicted == actual, None
    if math.isnan(p) or math.isnan(a):
        return math.isnan(p) and math.isnan(a), None
    if math.isinf(p) or math.isinf(a):
        return p == a, None
    delta = abs(p - a)
    return delta <= tolerance, delta


def _evenly_spaced(items: list[int], count: int) -> list[int]:
    if count <= 0 or not items:
        return []
    if len(items) <= count:
        return list(items)
    if count == 1:
        return [items[len(items) // 2]]
    indices = {
        int(round(i * (len(items) - 1) / (count - 1)))
        for i in range(count)
    }
    return [items[i] for i in sorted(indices)]


def _load_stored_science_states(
    session,
    *,
    calibration_version_id: int,
    frame_ids: Iterable[int],
) -> dict[tuple[int, int], dict[str, Any]]:
    frame_ids = sorted({int(v) for v in frame_ids})
    base = aliased(Measurement, name="s2_base")
    columns = [
        ScienceMeasurement.science_measurement_id.label("science_measurement_id"),
        ScienceMeasurement.base_measurement_id.label("base_measurement_id"),
        ScienceMeasurement.object_id.label("object_id"),
        ScienceMeasurement.frame_id.label("frame_id"),
        *[
            getattr(ScienceMeasurement, field_name).label(field_name)
            for field_name in _RECONSTRUCTED_FIELDS
        ],
        base.flags.label("base_flags"),
        ScienceMeasurement.flags.label("science_flags"),
    ]
    stmt = (
        select(*columns)
        .outerjoin(base, base.measurement_id == ScienceMeasurement.base_measurement_id)
        .where(ScienceMeasurement.calibration_version_id == int(calibration_version_id))
    )
    if frame_ids:
        stmt = stmt.where(ScienceMeasurement.frame_id.in_(frame_ids))
    rows = session.execute(
        stmt.order_by(ScienceMeasurement.object_id, ScienceMeasurement.frame_id)
    ).mappings()
    out: dict[tuple[int, int], dict[str, Any]] = {}
    for row in rows:
        payload = {
            "science_measurement_id": int(row["science_measurement_id"]),
            "base_measurement_id": (
                int(row["base_measurement_id"])
                if row["base_measurement_id"] is not None
                else None
            ),
            "object_id": int(row["object_id"]),
            "frame_id": int(row["frame_id"]),
            **{field_name: row[field_name] for field_name in _RECONSTRUCTED_FIELDS},
            "flags": merge_effective_science_flags(row["base_flags"], row["science_flags"]),
            "science_flags": dict(row["science_flags"] or {}),
            "base_flags": dict(row["base_flags"] or {}),
        }
        out[(payload["object_id"], payload["frame_id"])] = payload
    return out


def _chain_inventory(session, cv: CalibrationVersion) -> tuple[list[dict[str, Any]], str, str | None]:
    try:
        chain = _chain_from_base(session, cv)
    except Exception as exc:  # audit must continue across broken historical products
        return [], "CHAIN_ERROR", str(exc)
    if not chain or str(chain[0].product_kind or "").upper() != "BASE_PHOTOMETRY":
        return [], "NO_BASE_ROOT", "source chain does not terminate at BASE_PHOTOMETRY"
    stages: list[dict[str, Any]] = []
    for item in chain:
        iteration = None
        if str(item.product_kind or "").upper() != "BASE_PHOTOMETRY":
            iteration = _latest_iteration(session, int(item.calibration_version_id))
        stages.append(
            {
                "calibration_version_id": int(item.calibration_version_id),
                "name": str(item.name),
                "product_kind": str(item.product_kind or ""),
                "product_role": str(item.product_role or ""),
                "iteration_id": (
                    int(iteration.calibration_iteration_id) if iteration is not None else None
                ),
                "iteration_kind": (
                    str(iteration.iteration_kind or "") if iteration is not None else None
                ),
            }
        )
        if str(item.product_kind or "").upper() != "BASE_PHOTOMETRY" and iteration is None:
            return stages, "MISSING_NORMALIZED_ITERATION", f"cv={item.calibration_version_id}"
    return stages, "READY", None


def _parity_one_cv(
    session,
    *,
    cv: CalibrationVersion,
    sample_frames: int,
    tolerance: float,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    frame_rows = session.execute(
        FRAME_COUNTS_SQL, {"cv_id": int(cv.calibration_version_id)}
    ).mappings().all()
    all_frame_ids = [int(row["frame_id"]) for row in frame_rows]
    selected_frames = _evenly_spaced(all_frame_ids, sample_frames)
    result: dict[str, Any] = {
        "calibration_version_id": int(cv.calibration_version_id),
        "sample_frame_ids": selected_frames,
        "sample_frames": len(selected_frames),
        "sample_rows": 0,
        "reconstructed_rows": 0,
        "stored_rows": 0,
        "row_identity_mismatches": 0,
        "scalar_mismatches": 0,
        "flags_exact_rows": 0,
        "flags_mismatch_rows": 0,
        "status": "NOT_RUN",
        "reason": None,
    }
    field_rows: list[dict[str, Any]] = []
    if not selected_frames:
        result["status"] = "NO_FRAMES"
        return result, field_rows

    try:
        reconstructed = reconstruct_calibration_measurement_states(
            session,
            calibration_version_id=int(cv.calibration_version_id),
            frame_ids=selected_frames,
        )
    except Exception as exc:
        result["status"] = "RECONSTRUCTION_ERROR"
        result["reason"] = str(exc)
        return result, field_rows

    stored = _load_stored_science_states(
        session,
        calibration_version_id=int(cv.calibration_version_id),
        frame_ids=selected_frames,
    )
    predicted = reconstructed.states
    result["reconstructed_rows"] = len(predicted)
    result["stored_rows"] = len(stored)
    result["sample_rows"] = len(set(predicted) | set(stored))
    result["row_identity_mismatches"] = len(set(predicted) ^ set(stored))

    stats = {field: ScalarStats() for field in _RECONSTRUCTED_FIELDS}
    flags_exact = 0
    flags_mismatch = 0
    for key in sorted(set(predicted) & set(stored)):
        p = predicted[key]
        a = stored[key]
        if p.get("base_measurement_id") != a.get("base_measurement_id"):
            result["row_identity_mismatches"] += 1
        for field_name in _RECONSTRUCTED_FIELDS:
            st = stats[field_name]
            st.checked += 1
            equal, delta = _equal_scalar(p.get(field_name), a.get(field_name), tolerance)
            if not equal:
                st.mismatches += 1
            if delta is not None:
                st.max_abs_delta = max(st.max_abs_delta, delta)
        if dict(p.get("flags") or {}) == dict(a.get("flags") or {}):
            flags_exact += 1
        else:
            flags_mismatch += 1

    scalar_mismatches = sum(st.mismatches for st in stats.values())
    result["scalar_mismatches"] = scalar_mismatches
    result["flags_exact_rows"] = flags_exact
    result["flags_mismatch_rows"] = flags_mismatch
    if result["row_identity_mismatches"]:
        result["status"] = "ROW_IDENTITY_MISMATCH"
    elif scalar_mismatches:
        result["status"] = "SCALAR_MISMATCH"
    else:
        result["status"] = "SCALAR_PARITY"

    for field_name, st in stats.items():
        field_rows.append(
            {
                "calibration_version_id": int(cv.calibration_version_id),
                "field": field_name,
                "checked": st.checked,
                "mismatches": st.mismatches,
                "max_abs_delta": st.max_abs_delta,
            }
        )
    return result, field_rows


def _temp_relation_sizes(session, table_name: str) -> dict[str, int]:
    row = session.execute(
        text(
            """
            SELECT
                pg_relation_size(CAST(:table_name AS regclass))::bigint AS heap_bytes,
                pg_indexes_size(CAST(:table_name AS regclass))::bigint AS index_bytes,
                pg_total_relation_size(CAST(:table_name AS regclass))::bigint AS total_bytes
            """
        ),
        {"table_name": f"pg_temp.{table_name}"},
    ).mappings().one()
    return {key: int(row[key] or 0) for key in ("heap_bytes", "index_bytes", "total_bytes")}


def _build_physical_projection(session, *, sample_rows_per_cv: int) -> list[dict[str, Any]]:
    # Representative immutable products: cap each calibration equally so one
    # historical generation cannot dominate the physical-layout comparison.
    session.execute(
        text(
            """
            CREATE TEMP TABLE gg_s2_source ON COMMIT DROP AS
            WITH ranked AS (
                SELECT
                    sm.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY sm.calibration_version_id
                        ORDER BY sm.science_measurement_id
                    ) AS rn
                FROM science_measurements sm
                JOIN calibration_versions cv
                  ON cv.calibration_version_id = sm.calibration_version_id
                WHERE cv.product_role = 'SCIENCE'
                  AND cv.lifecycle_status IN ('COMPLETE_IMMUTABLE', 'SUPERSEDED_IMMUTABLE')
            )
            SELECT * FROM ranked
            WHERE rn <= :sample_rows_per_cv
            """
        ),
        {"sample_rows_per_cv": int(sample_rows_per_cv)},
    )
    source_rows = int(session.scalar(text("SELECT COUNT(*) FROM gg_s2_source")) or 0)
    if source_rows <= 0:
        return []

    variants = {
        "v1_copy": """
            science_measurement_id, calibration_version_id, source_measurement_id,
            base_measurement_id, object_id, frame_id,
            x, y, flux, flux_err, mag_inst, mag_cal, mag_err,
            mag_err_random, mag_err_systematic, aperture_radius_pix,
            local_background, local_zp_correction, centroid_dx_pix, centroid_dy_pix,
            flags, materialization_source, created_at, updated_at
        """,
        "minimal_mag3": """
            science_measurement_id, calibration_version_id, base_measurement_id,
            object_id, frame_id, mag_cal, mag_err_random, mag_err_systematic
        """,
        "minimal_mag4": """
            science_measurement_id, calibration_version_id, base_measurement_id,
            object_id, frame_id, mag_cal, mag_err, mag_err_random, mag_err_systematic
        """,
        "identity_only": """
            science_measurement_id, calibration_version_id, base_measurement_id,
            object_id, frame_id
        """,
    }
    rows: list[dict[str, Any]] = []
    for name, columns in variants.items():
        table_name = f"gg_s2_{name}"
        session.execute(text(f"CREATE TEMP TABLE {table_name} ON COMMIT DROP AS SELECT {columns} FROM gg_s2_source"))
        session.execute(text(f"ALTER TABLE {table_name} ADD PRIMARY KEY (science_measurement_id)"))
        session.execute(
            text(
                f"CREATE UNIQUE INDEX {table_name}_u1 ON {table_name} "
                "(calibration_version_id, object_id, frame_id)"
            )
        )
        session.execute(
            text(
                f"CREATE INDEX {table_name}_f1 ON {table_name} "
                "(calibration_version_id, frame_id, object_id)"
            )
        )
        session.execute(text(f"ANALYZE {table_name}"))
        size = _temp_relation_sizes(session, table_name)
        rows.append({"variant": name, "rows": source_rows, **size})
        session.execute(text(f"DROP TABLE {table_name}"))
    session.execute(text("DROP TABLE gg_s2_source"))
    if rows:
        baseline = next(row["total_bytes"] for row in rows if row["variant"] == "v1_copy")
        for row in rows:
            row["saving_fraction_vs_v1"] = (
                0.0 if baseline <= 0 else 1.0 - float(row["total_bytes"]) / float(baseline)
            )
    return rows


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _mib(value: int | float) -> float:
    return float(value) / 1024.0 / 1024.0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "S2 audit: inventory which science_measurements state is deterministically "
            "reconstructable from BASE + normalized calibration provenance."
        )
    )
    parser.add_argument("--out-root", default="reports/storage_refactor_s2_reconstructability")
    parser.add_argument("--sample-frames-per-calibration", type=int, default=3)
    parser.add_argument("--tolerance", type=float, default=1.0e-10)
    parser.add_argument("--max-calibrations", type=int, default=0, help="0 = all SCIENCE calibrations")
    parser.add_argument("--physical-sample-rows-per-calibration", type=int, default=5000)
    parser.add_argument("--skip-physical-projection", action="store_true")
    args = parser.parse_args()

    out_root = Path(args.out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    Session = make_session_factory()

    with Session() as session:
        inventory = [dict(row) for row in session.execute(CV_INVENTORY_SQL).mappings().all()]
        if args.max_calibrations > 0:
            inventory = inventory[: int(args.max_calibrations)]
        selected_ids = {int(row["calibration_version_id"]) for row in inventory}

        source_rows = session.execute(MATERIALIZATION_SOURCES_SQL).mappings().all()
        sources_by_cv: dict[int, list[dict[str, Any]]] = {}
        for row in source_rows:
            cv_id = int(row["calibration_version_id"])
            if cv_id not in selected_ids:
                continue
            sources_by_cv.setdefault(cv_id, []).append(
                {
                    "materialization_source": str(row["materialization_source"]),
                    "n_rows": int(row["n_rows"]),
                }
            )

        global_counts = dict(session.execute(GLOBAL_COUNTS_SQL).mappings().one())
        relation_sizes = [dict(row) for row in session.execute(RELATION_SIZE_SQL).mappings().all()]

        calibration_rows: list[dict[str, Any]] = []
        field_rows: list[dict[str, Any]] = []
        chain_rows: list[dict[str, Any]] = []
        status_counter: Counter[str] = Counter()

        total = len(inventory)
        for index, inv in enumerate(inventory, start=1):
            cv_id = int(inv["calibration_version_id"])
            cv = session.get(CalibrationVersion, cv_id)
            if cv is None:
                continue
            chain, chain_status, chain_reason = _chain_inventory(session, cv)
            for position, stage in enumerate(chain):
                chain_rows.append(
                    {
                        "target_calibration_version_id": cv_id,
                        "position": position,
                        **stage,
                    }
                )
            parity, per_field = _parity_one_cv(
                session,
                cv=cv,
                sample_frames=max(1, int(args.sample_frames_per_calibration)),
                tolerance=float(args.tolerance),
            )
            field_rows.extend(per_field)
            status_counter[parity["status"]] += 1
            materialization_sources = sources_by_cv.get(cv_id, [])
            row = {
                **inv,
                "chain_status": chain_status,
                "chain_reason": chain_reason,
                "chain_length": len(chain),
                "base_calibration_version_id": (
                    int(chain[0]["calibration_version_id"]) if chain else None
                ),
                "materialization_sources": json.dumps(materialization_sources, ensure_ascii=False),
                **parity,
            }
            calibration_rows.append(row)
            print(
                f"[{index:03d}/{total:03d}] cv={cv_id} rows={int(inv['n_rows']):,} "
                f"chain={chain_status} parity={parity['status']} sample={parity['stored_rows']:,} "
                f"flags_exact={parity['flags_exact_rows']:,}/{parity['stored_rows']:,}"
            )

        physical_rows: list[dict[str, Any]] = []
        if not args.skip_physical_projection:
            physical_rows = _build_physical_projection(
                session,
                sample_rows_per_cv=max(1, int(args.physical_sample_rows_per_calibration)),
            )
            # All writes are TEMP only. Roll back explicitly so no accidental
            # transaction state can escape this audit helper.
            session.rollback()

    _write_csv(out_root / "calibrations.csv", calibration_rows)
    _write_csv(out_root / "field_parity.csv", field_rows)
    _write_csv(out_root / "chains.csv", chain_rows)
    _write_csv(out_root / "physical_projection.csv", physical_rows)
    _write_csv(out_root / "relation_sizes.csv", relation_sizes)

    scalar_ready_rows = sum(
        int(row.get("n_rows") or 0)
        for row in calibration_rows
        if row.get("chain_status") == "READY" and row.get("status") == "SCALAR_PARITY"
    )
    total_selected_rows = sum(int(row.get("n_rows") or 0) for row in calibration_rows)
    flags_exact_sample = sum(int(row.get("flags_exact_rows") or 0) for row in calibration_rows)
    flags_sample_rows = sum(int(row.get("stored_rows") or 0) for row in calibration_rows)

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "selected_calibrations": len(calibration_rows),
        "selected_science_rows": total_selected_rows,
        "scalar_ready_rows_if_sample_gate_holds": scalar_ready_rows,
        "scalar_ready_fraction_if_sample_gate_holds": (
            0.0 if total_selected_rows <= 0 else scalar_ready_rows / total_selected_rows
        ),
        "sample_status_counts": dict(status_counter),
        "flags_exact_sample_rows": flags_exact_sample,
        "flags_sample_rows": flags_sample_rows,
        "flags_exact_sample_fraction": (
            0.0 if flags_sample_rows <= 0 else flags_exact_sample / flags_sample_rows
        ),
        "global_counts": global_counts,
        "relation_sizes": relation_sizes,
        "physical_projection": physical_rows,
        "reconstructed_fields": list(_RECONSTRUCTED_FIELDS),
        "candidate_minimal_hot_row": [
            "science_measurement_id",
            "calibration_version_id",
            "base_measurement_id",
            "object_id",
            "frame_id",
            "mag_cal",
            "mag_err_random",
            "mag_err_systematic",
        ],
        "notes": [
            "SCALAR_PARITY is sampled by complete frames; it is not an all-row proof for that calibration.",
            "Exact historical flag parity is reported separately and is not required to prove scalar reconstructability.",
            "Physical projection intentionally omits row JSON from minimal variants to measure the storage lower bound after normalized/sparse flag cutover.",
        ],
    }
    (out_root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )

    lines = [
        "# S2 science reconstructability audit",
        "",
        f"Calibrations: {len(calibration_rows)}",
        f"Selected SCIENCE rows: {total_selected_rows:,}",
        f"Rows behind sampled scalar-parity products: {scalar_ready_rows:,}",
        f"Sample status counts: {dict(status_counter)}",
        f"Exact reconstructed effective flags in sample: {flags_exact_sample:,}/{flags_sample_rows:,}",
        "",
        "## Physical lower-bound projection",
        "",
    ]
    for row in physical_rows:
        lines.append(
            f"- {row['variant']}: {_mib(row['total_bytes']):.1f} MiB total; "
            f"saving={100.0 * float(row.get('saving_fraction_vs_v1') or 0.0):.1f}%"
        )
    lines.extend(
        [
            "",
            "The minimal variants deliberately store no SCIENCE JSON flags and no reconstructable extraction/calibration scalars. They are a physical lower bound, not yet a migration approval.",
        ]
    )
    (out_root / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"Report root: {out_root}")
    if physical_rows:
        for row in physical_rows:
            print(
                f"  {row['variant']}: {_mib(row['total_bytes']):.1f} MiB "
                f"saving={100.0 * float(row.get('saving_fraction_vs_v1') or 0.0):.1f}%"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
