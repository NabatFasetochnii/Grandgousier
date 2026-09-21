from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from grandgousier.calibration.reconstruction import (
    _RECONSTRUCTED_FIELDS,
    reconstruct_calibration_measurement_states,
)
from grandgousier.db.session import make_session_factory
from grandgousier.photometry.science_flags import merge_effective_science_flags


@dataclass(slots=True)
class CalibrationAudit:
    calibration_version_id: int
    calibration_name: str
    lifecycle_status: str
    materialization_sources: str
    v1_rows: int
    cache_rows: int
    sampled_frames: int
    sampled_rows: int
    scalar_mismatches: int
    identity_mismatches: int
    flags_exact_rows: int
    flags_patch_rows: int
    flag_patch_failures: int
    override_key_occurrences: int
    removed_key_occurrences: int
    old_science_flags_json_bytes: int
    old_effective_flags_json_bytes: int
    reconstructed_flags_json_bytes: int
    patch_json_bytes: int
    status: str
    reason: str | None = None


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "S3 read/temporary-write audit for retiring legacy wide science_measurements. "
            "Samples complete SCIENCE frames, reconstructs authoritative normalized state, "
            "and measures a sparse lossless historical flag patch. Only TEMP tables are "
            "written; the transaction is rolled back."
        )
    )
    p.add_argument("--database-url", default=None)
    p.add_argument("--field-id", type=int, default=33,
                   help="Limit SCIENCE products to one field; use --all-fields to audit all fields")
    p.add_argument("--all-fields", action="store_true")
    p.add_argument("--sample-frames-per-calibration", type=int, default=3)
    p.add_argument("--max-calibrations", type=int, default=0,
                   help="0 means all selected SCIENCE calibrations")
    p.add_argument("--out-root", default="reports/storage_refactor_s3_flag_overrides_0642")
    p.add_argument("--statement-timeout-ms", type=int, default=900000)
    p.add_argument("--scalar-tolerance", type=float, default=1e-10)
    return p


def _json_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _equivalent(left: Any, right: Any, tolerance: float) -> bool:
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, bool) or isinstance(right, bool):
        return left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        try:
            lf = float(left)
            rf = float(right)
        except (TypeError, ValueError):
            return left == right
        if math.isnan(lf) or math.isnan(rf):
            return math.isnan(lf) and math.isnan(rf)
        if math.isinf(lf) or math.isinf(rf):
            return lf == rf
        return abs(lf - rf) <= float(tolerance)
    return left == right


def _evenly_spaced(values: list[int], n: int) -> list[int]:
    if not values or n <= 0:
        return []
    if len(values) <= n:
        return list(values)
    if n == 1:
        return [values[len(values) // 2]]
    out: list[int] = []
    for i in range(n):
        idx = round(i * (len(values) - 1) / (n - 1))
        value = values[idx]
        if value not in out:
            out.append(value)
    return out


def _lossless_flag_patch(
    reconstructed: dict[str, Any],
    historical: dict[str, Any],
) -> tuple[dict[str, Any], list[str], bool]:
    """Return sparse top-level patch such that apply(reconstructed, patch) == historical."""
    removed = sorted(str(key) for key in (set(reconstructed) - set(historical)))
    overrides = {
        str(key): value
        for key, value in historical.items()
        if key not in reconstructed or reconstructed.get(key) != value
    }
    restored = dict(reconstructed)
    for key in removed:
        restored.pop(key, None)
    restored.update(overrides)
    return overrides, removed, restored == historical


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                keys.append(key)
                seen.add(key)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def _relation_size(session, name: str) -> dict[str, int]:
    row = session.execute(text("""
        SELECT
            pg_relation_size(to_regclass(:name))::bigint AS heap_bytes,
            pg_table_size(to_regclass(:name))::bigint AS table_bytes,
            pg_indexes_size(to_regclass(:name))::bigint AS index_bytes,
            pg_total_relation_size(to_regclass(:name))::bigint AS total_bytes
    """), {"name": name}).mappings().one()
    return {k: int(v or 0) for k, v in row.items()}


def _selected_calibrations(session, *, field_id: int | None, limit: int) -> list[dict[str, Any]]:
    where = ["UPPER(COALESCE(cv.product_role, '')) = 'SCIENCE'", "sm.n_rows > 0"]
    params: dict[str, Any] = {}
    if field_id is not None:
        where.append("cv.field_id = :field_id")
        params["field_id"] = int(field_id)
    sql = f"""
        WITH sm AS (
            SELECT calibration_version_id,
                   COUNT(*)::bigint AS n_rows,
                   COUNT(DISTINCT frame_id)::bigint AS n_frames,
                   jsonb_agg(DISTINCT materialization_source) AS materialization_sources
            FROM science_measurements
            GROUP BY calibration_version_id
        ), cache AS (
            SELECT calibration_version_id, COUNT(*)::bigint AS n_rows
            FROM science_photometry_cache
            GROUP BY calibration_version_id
        )
        SELECT cv.calibration_version_id, cv.name, cv.lifecycle_status,
               cv.field_id, cv.series_id,
               sm.n_rows AS v1_rows, sm.n_frames,
               COALESCE(cache.n_rows, 0)::bigint AS cache_rows,
               sm.materialization_sources::text AS materialization_sources
        FROM calibration_versions cv
        JOIN sm USING (calibration_version_id)
        LEFT JOIN cache USING (calibration_version_id)
        WHERE {' AND '.join(where)}
        ORDER BY cv.calibration_version_id
    """
    rows = [dict(r) for r in session.execute(text(sql), params).mappings().all()]
    return rows[:limit] if limit > 0 else rows


def main() -> int:
    args = _parser().parse_args()
    if args.sample_frames_per_calibration < 1:
        raise SystemExit("--sample-frames-per-calibration must be >= 1")
    field_id = None if args.all_fields else int(args.field_id)
    out = Path(args.out_root)
    out.mkdir(parents=True, exist_ok=True)

    Session = make_session_factory(args.database_url)
    calibration_rows: list[dict[str, Any]] = []
    override_key_counts: Counter[str] = Counter()
    removed_key_counts: Counter[str] = Counter()
    override_key_bytes: Counter[str] = Counter()

    with Session() as session:
        if session.get_bind().dialect.name != "postgresql":
            raise RuntimeError("S3 requires PostgreSQL")
        session.execute(text("SELECT set_config('statement_timeout', :v, true)"),
                        {"v": f"{int(args.statement_timeout_ms)}ms"})
        session.execute(text("SELECT set_config('lock_timeout', '5000ms', true)"))

        selected = _selected_calibrations(
            session,
            field_id=field_id,
            limit=int(args.max_calibrations),
        )
        if not selected:
            raise SystemExit("No materialized SCIENCE calibrations matched the requested scope")

        session.execute(text("""
            CREATE TEMP TABLE gg_s3_selected_ids (
                science_measurement_id bigint PRIMARY KEY
            ) ON COMMIT DROP
        """))
        session.execute(text("""
            CREATE TEMP TABLE gg_s3_flag_overrides (
                calibration_version_id bigint NOT NULL,
                base_measurement_id bigint NOT NULL,
                override_flags jsonb NOT NULL,
                removed_keys text[] NOT NULL,
                PRIMARY KEY (calibration_version_id, base_measurement_id)
            ) ON COMMIT DROP
        """))

        print(
            f"SCIENCE S3: calibrations={len(selected)} field_id={field_id if field_id is not None else 'ALL'} "
            f"frames_per_cv={args.sample_frames_per_calibration}"
        )

        for idx, meta in enumerate(selected, start=1):
            cv_id = int(meta["calibration_version_id"])
            frame_ids = [int(v) for v in session.execute(text("""
                SELECT frame_id
                FROM science_measurements
                WHERE calibration_version_id = :cv
                GROUP BY frame_id
                ORDER BY frame_id
            """), {"cv": cv_id}).scalars().all()]
            chosen = _evenly_spaced(frame_ids, int(args.sample_frames_per_calibration))
            if not chosen:
                audit = CalibrationAudit(
                    calibration_version_id=cv_id,
                    calibration_name=str(meta["name"]),
                    lifecycle_status=str(meta.get("lifecycle_status") or ""),
                    materialization_sources=str(meta.get("materialization_sources") or ""),
                    v1_rows=int(meta["v1_rows"]),
                    cache_rows=int(meta["cache_rows"]),
                    sampled_frames=0, sampled_rows=0,
                    scalar_mismatches=0, identity_mismatches=0,
                    flags_exact_rows=0, flags_patch_rows=0, flag_patch_failures=0,
                    override_key_occurrences=0, removed_key_occurrences=0,
                    old_science_flags_json_bytes=0, old_effective_flags_json_bytes=0,
                    reconstructed_flags_json_bytes=0, patch_json_bytes=0,
                    status="BLOCKED", reason="NO_FRAMES",
                )
                calibration_rows.append(asdict(audit))
                continue

            try:
                reconstructed = reconstruct_calibration_measurement_states(
                    session,
                    calibration_version_id=cv_id,
                    frame_ids=chosen,
                )
            except Exception as exc:
                audit = CalibrationAudit(
                    calibration_version_id=cv_id,
                    calibration_name=str(meta["name"]),
                    lifecycle_status=str(meta.get("lifecycle_status") or ""),
                    materialization_sources=str(meta.get("materialization_sources") or ""),
                    v1_rows=int(meta["v1_rows"]), cache_rows=int(meta["cache_rows"]),
                    sampled_frames=len(chosen), sampled_rows=0,
                    scalar_mismatches=0, identity_mismatches=0,
                    flags_exact_rows=0, flags_patch_rows=0, flag_patch_failures=0,
                    override_key_occurrences=0, removed_key_occurrences=0,
                    old_science_flags_json_bytes=0, old_effective_flags_json_bytes=0,
                    reconstructed_flags_json_bytes=0, patch_json_bytes=0,
                    status="BLOCKED", reason=f"RECONSTRUCTION_FAILED: {exc}",
                )
                calibration_rows.append(asdict(audit))
                print(f"[{idx:03d}/{len(selected):03d}] cv={cv_id} BLOCKED reconstruction")
                continue

            stored = session.execute(text("""
                SELECT sm.science_measurement_id, sm.base_measurement_id,
                       sm.object_id, sm.frame_id,
                       sm.x, sm.y, sm.flux, sm.flux_err, sm.mag_inst, sm.mag_cal,
                       sm.mag_err, sm.mag_err_random, sm.mag_err_systematic,
                       sm.aperture_radius_pix, sm.local_background,
                       sm.local_zp_correction, sm.centroid_dx_pix, sm.centroid_dy_pix,
                       sm.flags AS science_flags,
                       bm.flags AS base_flags
                FROM science_measurements sm
                JOIN measurements bm ON bm.measurement_id = sm.base_measurement_id
                WHERE sm.calibration_version_id = :cv
                  AND sm.frame_id = ANY(:frame_ids)
                ORDER BY sm.object_id, sm.frame_id
            """), {"cv": cv_id, "frame_ids": chosen}).mappings().all()

            scalar_mismatches = 0
            identity_mismatches = 0
            flags_exact = 0
            flags_patch = 0
            patch_failures = 0
            old_science_bytes = 0
            old_effective_bytes = 0
            recon_flags_bytes = 0
            patch_bytes = 0
            override_occ = 0
            removed_occ = 0
            selected_ids_payload: list[dict[str, int]] = []
            patch_payload: list[dict[str, Any]] = []

            for row in stored:
                key = (int(row["object_id"]), int(row["frame_id"]))
                state = reconstructed.states.get(key)
                selected_ids_payload.append({"id": int(row["science_measurement_id"])})
                if state is None:
                    identity_mismatches += 1
                    continue
                if int(state.get("base_measurement_id") or -1) != int(row["base_measurement_id"] or -2):
                    identity_mismatches += 1
                for field in _RECONSTRUCTED_FIELDS:
                    if not _equivalent(state.get(field), row[field], float(args.scalar_tolerance)):
                        scalar_mismatches += 1

                science_flags = dict(row["science_flags"] or {})
                base_flags = dict(row["base_flags"] or {})
                historical = merge_effective_science_flags(base_flags, science_flags)
                predicted = dict(state.get("flags") or {})
                old_science_bytes += _json_bytes(science_flags)
                old_effective_bytes += _json_bytes(historical)
                recon_flags_bytes += _json_bytes(predicted)
                if predicted == historical:
                    flags_exact += 1
                    continue
                overrides, removed, exact = _lossless_flag_patch(predicted, historical)
                if not exact:
                    patch_failures += 1
                    continue
                flags_patch += 1
                override_occ += len(overrides)
                removed_occ += len(removed)
                patch_doc = {"o": overrides, "r": removed}
                patch_bytes += _json_bytes(patch_doc)
                for key_name, value in overrides.items():
                    override_key_counts[key_name] += 1
                    override_key_bytes[key_name] += _json_bytes(value) + len(key_name.encode("utf-8"))
                for key_name in removed:
                    removed_key_counts[key_name] += 1
                patch_payload.append({
                    "cv": cv_id,
                    "base_id": int(row["base_measurement_id"]),
                    "overrides": json.dumps(overrides, ensure_ascii=False, separators=(",", ":")),
                    "removed": removed,
                })

            if selected_ids_payload:
                session.execute(text(
                    "INSERT INTO gg_s3_selected_ids(science_measurement_id) VALUES (:id) "
                    "ON CONFLICT DO NOTHING"
                ), selected_ids_payload)
            if patch_payload:
                session.execute(text("""
                    INSERT INTO gg_s3_flag_overrides(
                        calibration_version_id, base_measurement_id, override_flags, removed_keys
                    ) VALUES (
                        :cv, :base_id, CAST(:overrides AS jsonb), :removed
                    )
                    ON CONFLICT DO NOTHING
                """), patch_payload)

            status = "READY_NO_FLAG_PATCH" if flags_patch == 0 and patch_failures == 0 else (
                "READY_SPARSE_FLAG_PATCH" if patch_failures == 0 else "BLOCKED_PATCH_FAILURE"
            )
            reason = None if identity_mismatches == 0 and scalar_mismatches == 0 else (
                f"identity_mismatches={identity_mismatches}; scalar_mismatches={scalar_mismatches}"
            )
            if reason is not None:
                status = "BLOCKED_PARITY_MISMATCH"

            audit = CalibrationAudit(
                calibration_version_id=cv_id,
                calibration_name=str(meta["name"]),
                lifecycle_status=str(meta.get("lifecycle_status") or ""),
                materialization_sources=str(meta.get("materialization_sources") or ""),
                v1_rows=int(meta["v1_rows"]), cache_rows=int(meta["cache_rows"]),
                sampled_frames=len(chosen), sampled_rows=len(stored),
                scalar_mismatches=scalar_mismatches,
                identity_mismatches=identity_mismatches,
                flags_exact_rows=flags_exact,
                flags_patch_rows=flags_patch,
                flag_patch_failures=patch_failures,
                override_key_occurrences=override_occ,
                removed_key_occurrences=removed_occ,
                old_science_flags_json_bytes=old_science_bytes,
                old_effective_flags_json_bytes=old_effective_bytes,
                reconstructed_flags_json_bytes=recon_flags_bytes,
                patch_json_bytes=patch_bytes,
                status=status,
                reason=reason,
            )
            calibration_rows.append(asdict(audit))
            frac = 0.0 if not stored else flags_patch / len(stored)
            print(
                f"[{idx:03d}/{len(selected):03d}] cv={cv_id} rows={len(stored):,} "
                f"scalar_mismatch={scalar_mismatches} flag_patch={flags_patch:,} ({frac:.1%}) "
                f"status={status}"
            )

        # Physical projection of the exact sampled V1 rows vs sparse override table.
        session.execute(text("""
            CREATE TEMP TABLE gg_s3_v1_sample ON COMMIT DROP AS
            SELECT sm.*
            FROM science_measurements sm
            JOIN gg_s3_selected_ids i USING (science_measurement_id)
        """))
        session.execute(text("CREATE UNIQUE INDEX ON gg_s3_v1_sample(science_measurement_id)"))
        session.execute(text("""
            CREATE UNIQUE INDEX ON gg_s3_v1_sample(calibration_version_id, object_id, frame_id)
        """))
        session.execute(text("""
            CREATE INDEX ON gg_s3_v1_sample(calibration_version_id, frame_id, object_id)
        """))

        v1_size = _relation_size(session, "pg_temp.gg_s3_v1_sample")
        patch_size = _relation_size(session, "pg_temp.gg_s3_flag_overrides")
        v1_sample_rows = int(session.scalar(text("SELECT COUNT(*) FROM gg_s3_v1_sample")) or 0)
        patch_rows = int(session.scalar(text("SELECT COUNT(*) FROM gg_s3_flag_overrides")) or 0)

        fk_rows = [dict(r) for r in session.execute(text("""
            SELECT con.conname,
                   con.conrelid::regclass::text AS referencing_table,
                   pg_get_constraintdef(con.oid) AS definition
            FROM pg_constraint con
            WHERE con.contype = 'f'
              AND con.confrelid = 'science_measurements'::regclass
            ORDER BY referencing_table, con.conname
        """)).mappings().all()]

        total_v1_rows = sum(int(row["v1_rows"]) for row in calibration_rows)
        sampled_rows = sum(int(row["sampled_rows"]) for row in calibration_rows)
        exact_rows = sum(int(row["flags_exact_rows"]) for row in calibration_rows)
        sparse_rows = sum(int(row["flags_patch_rows"]) for row in calibration_rows)
        patch_failures = sum(int(row["flag_patch_failures"]) for row in calibration_rows)
        scalar_mismatches = sum(int(row["scalar_mismatches"]) for row in calibration_rows)
        identity_mismatches = sum(int(row["identity_mismatches"]) for row in calibration_rows)
        old_science_bytes = sum(int(row["old_science_flags_json_bytes"]) for row in calibration_rows)
        patch_json_bytes = sum(int(row["patch_json_bytes"]) for row in calibration_rows)

        status_counts = Counter(str(row["status"]) for row in calibration_rows)
        report = {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "scope": {"field_id": field_id, "all_fields": bool(args.all_fields)},
            "temporary_only": True,
            "public_schema_modified": False,
            "selected_calibrations": len(calibration_rows),
            "selected_v1_rows": total_v1_rows,
            "sampled_rows": sampled_rows,
            "scalar_mismatches": scalar_mismatches,
            "identity_mismatches": identity_mismatches,
            "flags_exact_rows": exact_rows,
            "flags_patch_rows": sparse_rows,
            "flag_patch_failures": patch_failures,
            "sample_patch_fraction": (sparse_rows / sampled_rows if sampled_rows else None),
            "sample_old_science_flags_json_bytes": old_science_bytes,
            "sample_patch_json_bytes": patch_json_bytes,
            "sample_patch_json_fraction_vs_old_science_flags": (
                patch_json_bytes / old_science_bytes if old_science_bytes else None
            ),
            "status_counts": dict(status_counts),
            "physical_projection": {
                "v1_sample_rows": v1_sample_rows,
                "flag_override_rows": patch_rows,
                "v1_sample": v1_size,
                "sparse_flag_overrides": patch_size,
                "saving_fraction_vs_v1_sample": (
                    1.0 - patch_size["total_bytes"] / v1_size["total_bytes"]
                    if v1_size["total_bytes"] else None
                ),
            },
            "foreign_keys_referencing_science_measurements": fk_rows,
            "retirement_gate": (
                "READY_FOR_ADDITIVE_OVERRIDE_SCHEMA"
                if scalar_mismatches == 0 and identity_mismatches == 0 and patch_failures == 0
                else "BLOCKED"
            ),
            "notes": [
                "This is a complete-frame sample, not an all-row retirement proof.",
                "Sparse flag patches preserve exact top-level historical effective flags: "
                "(reconstructed - removed_keys) || override_flags.",
                "No persistent production rows or schema objects are written by this audit.",
            ],
        }

        key_rows: list[dict[str, Any]] = []
        for key in sorted(set(override_key_counts) | set(removed_key_counts)):
            key_rows.append({
                "flag_key": key,
                "override_occurrences": int(override_key_counts[key]),
                "removed_occurrences": int(removed_key_counts[key]),
                "approx_override_value_and_key_bytes": int(override_key_bytes[key]),
            })
        key_rows.sort(
            key=lambda r: (
                -int(r["approx_override_value_and_key_bytes"]),
                -int(r["override_occurrences"]),
                str(r["flag_key"]),
            )
        )

        _write_csv(out / "calibrations.csv", calibration_rows)
        _write_csv(out / "flag_keys.csv", key_rows)
        _write_csv(out / "foreign_keys.csv", fk_rows)
        (out / "summary.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        md = [
            "# Grandgousier S3 sparse historical flag override audit",
            "",
            f"- Retirement gate: **{report['retirement_gate']}**",
            f"- Selected calibrations: {len(calibration_rows)}",
            f"- Selected V1 rows: {total_v1_rows:,}",
            f"- Sampled rows: {sampled_rows:,}",
            f"- Scalar mismatches: {scalar_mismatches:,}",
            f"- Identity mismatches: {identity_mismatches:,}",
            f"- Exact reconstructed flags: {exact_rows:,}",
            f"- Rows requiring sparse patch: {sparse_rows:,}",
            f"- Patch failures: {patch_failures:,}",
            "",
            "## Physical sample",
            "",
            f"- V1 sample total: {v1_size['total_bytes'] / 1024**2:.2f} MiB",
            f"- Sparse overrides total: {patch_size['total_bytes'] / 1024**2:.2f} MiB",
            f"- Physical saving: {report['physical_projection']['saving_fraction_vs_v1_sample']:.2%}",
            "",
            "This report is a sample-based design gate only; destructive cleanup still requires an all-row proof.",
        ]
        (out / "summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")

        print(f"Report root: {out}")
        print(
            f"S3 result: sample_rows={sampled_rows:,} patch_rows={sparse_rows:,} "
            f"scalar_mismatches={scalar_mismatches} patch_failures={patch_failures}"
        )
        print(
            f"  V1 sample={v1_size['total_bytes']/1024**2:.1f} MiB; "
            f"sparse overrides={patch_size['total_bytes']/1024**2:.1f} MiB; "
            f"saving={report['physical_projection']['saving_fraction_vs_v1_sample']:.1%}"
        )
        print(f"  retirement_gate={report['retirement_gate']}")
        session.rollback()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
