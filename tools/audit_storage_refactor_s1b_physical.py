from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from grandgousier.db.session import make_session_factory


INHERITED_SCIENCE_COLUMNS = {
    "x",
    "y",
    "aperture_radius_pix",
    "local_background",
    "centroid_dx_pix",
    "centroid_dy_pix",
}


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Transient TEMP-table physical storage probe for Grandgousier 0.6.3.2. "
            "Measures BASE flag factoring and narrow ScienceMeasurement V2 using the same "
            "PostgreSQL storage/TOAST machinery. No persistent/public table is modified."
        )
    )
    p.add_argument("--database-url", default=None)
    p.add_argument(
        "--s1-report-root",
        default="reports/storage_refactor_s1_0632_real",
        help="Existing S1 report root containing base_frame_groups.csv and science_v2_projection.csv.",
    )
    p.add_argument(
        "--out-root",
        default="reports/storage_refactor_s1b_physical_0632",
    )
    p.add_argument(
        "--base-frames",
        type=int,
        default=32,
        help="Number of complete BASE frames reused from S1 for the TEMP physical probe (default: 32).",
    )
    p.add_argument(
        "--science-sample-rows",
        type=int,
        default=20000,
        help="Maximum rows per science calibration selected by the S1 report (default: 20000).",
    )
    p.add_argument(
        "--statement-timeout-ms",
        type=int,
        default=600000,
    )
    return p


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _evenly_spaced(rows: list[dict[str, str]], count: int) -> list[dict[str, str]]:
    if count <= 0 or not rows:
        return []
    if count >= len(rows):
        return list(rows)
    if count == 1:
        return [rows[len(rows) // 2]]
    indices: list[int] = []
    for i in range(count):
        idx = round(i * (len(rows) - 1) / (count - 1))
        if idx not in indices:
            indices.append(idx)
    return [rows[i] for i in indices]


def _select_base_frames(rows: list[dict[str, str]], target: int) -> list[dict[str, str]]:
    """Preserve calibration diversity first, then fill evenly from the S1 frame list."""
    if target >= len(rows):
        return list(rows)
    by_cv: dict[int, list[dict[str, str]]] = {}
    for row in rows:
        by_cv.setdefault(int(row["calibration_version_id"]), []).append(row)
    selected: list[dict[str, str]] = []
    cvs = sorted(by_cv)
    # At least one frame per represented calibration where possible.
    for cv in cvs:
        if len(selected) >= target:
            break
        group = by_cv[cv]
        selected.append(group[len(group) // 2])
    selected_keys = {(int(r["calibration_version_id"]), int(r["frame_id"])) for r in selected}
    remainder = [
        r for r in rows
        if (int(r["calibration_version_id"]), int(r["frame_id"])) not in selected_keys
    ]
    selected.extend(_evenly_spaced(remainder, target - len(selected)))
    return selected[:target]


def _qident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _table_columns(session, table: str) -> list[str]:
    rows = session.execute(
        text(
            """
            SELECT a.attname
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = current_schema()
              AND c.relname = :table
              AND a.attnum > 0
              AND NOT a.attisdropped
            ORDER BY a.attnum
            """
        ),
        {"table": table},
    ).scalars().all()
    if not rows:
        raise RuntimeError(f"Could not introspect public table {table!r}")
    return [str(v) for v in rows]


def _size_row(session, table: str) -> dict[str, int]:
    row = session.execute(
        text(
            """
            SELECT
                pg_relation_size(to_regclass(:name))::bigint AS heap_bytes,
                pg_table_size(to_regclass(:name))::bigint AS table_bytes,
                pg_indexes_size(to_regclass(:name))::bigint AS indexes_bytes,
                pg_total_relation_size(to_regclass(:name))::bigint AS total_bytes
            """
        ),
        {"name": f"pg_temp.{table}"},
    ).mappings().one()
    return {k: int(v or 0) for k, v in row.items()}


def _relation_stats(session, table: str) -> dict[str, Any]:
    size = _size_row(session, table)
    count = int(session.scalar(text(f"SELECT COUNT(*) FROM pg_temp.{_qident(table)}")) or 0)
    return {"table": table, "rows": count, **size}


def _create_selected_base_frames(session, frames: list[dict[str, str]]) -> None:
    session.execute(
        text(
            """
            CREATE TEMP TABLE gg_s1b_selected_base_frames (
                calibration_version_id bigint NOT NULL,
                frame_id bigint NOT NULL,
                PRIMARY KEY (calibration_version_id, frame_id)
            ) ON COMMIT DROP
            """
        )
    )
    session.execute(
        text(
            """
            INSERT INTO gg_s1b_selected_base_frames (calibration_version_id, frame_id)
            VALUES (:cv, :frame)
            """
        ),
        [
            {"cv": int(r["calibration_version_id"]), "frame": int(r["frame_id"])}
            for r in frames
        ],
    )


def _create_base_probes(session, measurement_columns: list[str]) -> dict[str, Any]:
    session.execute(
        text(
            """
            CREATE TEMP TABLE gg_s1b_base_context ON COMMIT DROP AS
            WITH selected_rows AS MATERIALIZED (
                SELECT
                    m.measurement_id,
                    m.calibration_version_id,
                    m.frame_id,
                    COALESCE(m.flags, '{}'::jsonb) AS flags
                FROM measurements m
                JOIN gg_s1b_selected_base_frames s
                  ON s.calibration_version_id = m.calibration_version_id
                 AND s.frame_id = m.frame_id
            ),
            frame_counts AS (
                SELECT calibration_version_id, frame_id, COUNT(*)::bigint AS n_rows
                FROM selected_rows
                GROUP BY calibration_version_id, frame_id
            ),
            kv AS (
                SELECT
                    r.measurement_id,
                    r.calibration_version_id,
                    r.frame_id,
                    e.key,
                    e.value
                FROM selected_rows r
                CROSS JOIN LATERAL jsonb_each(r.flags) e(key, value)
            ),
            constant_keys AS (
                SELECT
                    kv.calibration_version_id,
                    kv.frame_id,
                    kv.key,
                    (array_agg(kv.value ORDER BY kv.measurement_id))[1] AS value
                FROM kv
                JOIN frame_counts fc
                  ON fc.calibration_version_id = kv.calibration_version_id
                 AND fc.frame_id = kv.frame_id
                GROUP BY kv.calibration_version_id, kv.frame_id, kv.key, fc.n_rows
                HAVING COUNT(*) = fc.n_rows
                   AND COUNT(DISTINCT kv.value) = 1
            )
            SELECT
                sf.calibration_version_id,
                sf.frame_id,
                COALESCE(jsonb_object_agg(ck.key, ck.value) FILTER (WHERE ck.key IS NOT NULL), '{}'::jsonb) AS context_flags,
                COALESCE(array_agg(ck.key ORDER BY ck.key) FILTER (WHERE ck.key IS NOT NULL), ARRAY[]::text[]) AS context_keys
            FROM gg_s1b_selected_base_frames sf
            LEFT JOIN constant_keys ck
              ON ck.calibration_version_id = sf.calibration_version_id
             AND ck.frame_id = sf.frame_id
            GROUP BY sf.calibration_version_id, sf.frame_id
            """
        )
    )

    session.execute(
        text(
            """
            CREATE TEMP TABLE gg_s1b_base_v1 ON COMMIT DROP AS
            SELECT m.*
            FROM measurements m
            JOIN gg_s1b_selected_base_frames s
              ON s.calibration_version_id = m.calibration_version_id
             AND s.frame_id = m.frame_id
            """
        )
    )

    select_parts = []
    for col in measurement_columns:
        if col == "flags":
            select_parts.append("(COALESCE(m.flags, '{}'::jsonb) - c.context_keys) AS flags")
        else:
            select_parts.append(f"m.{_qident(col)}")
    session.execute(
        text(
            f"""
            CREATE TEMP TABLE gg_s1b_base_v2 ON COMMIT DROP AS
            SELECT {', '.join(select_parts)}
            FROM measurements m
            JOIN gg_s1b_selected_base_frames s
              ON s.calibration_version_id = m.calibration_version_id
             AND s.frame_id = m.frame_id
            JOIN gg_s1b_base_context c
              ON c.calibration_version_id = m.calibration_version_id
             AND c.frame_id = m.frame_id
            """
        )
    )

    parity = session.execute(
        text(
            """
            SELECT
                COUNT(*)::bigint AS rows_total,
                COUNT(*) FILTER (
                    WHERE (c.context_flags || COALESCE(v2.flags, '{}'::jsonb))
                          = COALESCE(v1.flags, '{}'::jsonb)
                )::bigint AS exact_rows
            FROM gg_s1b_base_v1 v1
            JOIN gg_s1b_base_v2 v2 USING (measurement_id)
            JOIN gg_s1b_base_context c
              ON c.calibration_version_id = v1.calibration_version_id
             AND c.frame_id = v1.frame_id
            """
        )
    ).mappings().one()

    return {
        "parity_rows": int(parity["rows_total"] or 0),
        "parity_exact_rows": int(parity["exact_rows"] or 0),
    }


def _create_selected_science_cvs(session, science_rows: list[dict[str, str]]) -> list[int]:
    ids = sorted({int(r["calibration_version_id"]) for r in science_rows})
    if not ids:
        ids = [
            int(v)
            for v in session.execute(
                text(
                    """
                    SELECT DISTINCT calibration_version_id
                    FROM science_measurements
                    ORDER BY calibration_version_id
                    """
                )
            ).scalars().all()
        ]
        ids = [r["calibration_version_id"] for r in _evenly_spaced(
            [{"calibration_version_id": str(v)} for v in ids], 8
        )]
        ids = [int(v) for v in ids]
    session.execute(
        text(
            """
            CREATE TEMP TABLE gg_s1b_selected_science_cvs (
                calibration_version_id bigint PRIMARY KEY
            ) ON COMMIT DROP
            """
        )
    )
    session.execute(
        text("INSERT INTO gg_s1b_selected_science_cvs VALUES (:cv)"),
        [{"cv": v} for v in ids],
    )
    return ids


def _create_science_probes(
    session,
    science_columns: list[str],
    *,
    sample_rows: int,
) -> dict[str, Any]:
    session.execute(
        text(
            f"""
            CREATE TEMP TABLE gg_s1b_science_sample_ids ON COMMIT DROP AS
            SELECT science_measurement_id
            FROM (
                SELECT
                    sm.science_measurement_id,
                    sm.calibration_version_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY sm.calibration_version_id
                        ORDER BY sm.science_measurement_id
                    ) AS rn
                FROM science_measurements sm
                JOIN gg_s1b_selected_science_cvs s USING (calibration_version_id)
            ) ranked
            WHERE rn <= {int(sample_rows)}
            """
        )
    )
    session.execute(text("CREATE UNIQUE INDEX ON gg_s1b_science_sample_ids (science_measurement_id)"))

    session.execute(
        text(
            """
            CREATE TEMP TABLE gg_s1b_science_v1 ON COMMIT DROP AS
            SELECT sm.*
            FROM science_measurements sm
            JOIN gg_s1b_science_sample_ids ids USING (science_measurement_id)
            """
        )
    )

    compat_cols = [c for c in science_columns if c not in INHERITED_SCIENCE_COLUMNS]
    core_cols = [c for c in compat_cols if c != "source_measurement_id"]
    session.execute(
        text(
            f"""
            CREATE TEMP TABLE gg_s1b_science_v2_compat ON COMMIT DROP AS
            SELECT {', '.join('sm.' + _qident(c) for c in compat_cols)}
            FROM science_measurements sm
            JOIN gg_s1b_science_sample_ids ids USING (science_measurement_id)
            """
        )
    )
    session.execute(
        text(
            f"""
            CREATE TEMP TABLE gg_s1b_science_v2_core ON COMMIT DROP AS
            SELECT {', '.join('sm.' + _qident(c) for c in core_cols)}
            FROM science_measurements sm
            JOIN gg_s1b_science_sample_ids ids USING (science_measurement_id)
            """
        )
    )

    for table in ("gg_s1b_science_v1", "gg_s1b_science_v2_compat", "gg_s1b_science_v2_core"):
        session.execute(text(f"CREATE UNIQUE INDEX ON {table} (science_measurement_id)"))
        session.execute(
            text(
                f"CREATE UNIQUE INDEX ON {table} "
                "(calibration_version_id, object_id, frame_id)"
            )
        )
        session.execute(
            text(
                f"CREATE INDEX ON {table} "
                "(calibration_version_id, frame_id, object_id)"
            )
        )

    parity = session.execute(
        text(
            """
            SELECT
                COUNT(*)::bigint AS rows_total,
                COUNT(*) FILTER (WHERE v1.x IS NOT DISTINCT FROM bm.x)::bigint AS x_equal,
                COUNT(*) FILTER (WHERE v1.y IS NOT DISTINCT FROM bm.y)::bigint AS y_equal,
                COUNT(*) FILTER (WHERE v1.aperture_radius_pix IS NOT DISTINCT FROM bm.aperture_radius_pix)::bigint AS aperture_equal,
                COUNT(*) FILTER (WHERE v1.local_background IS NOT DISTINCT FROM bm.local_background)::bigint AS background_equal,
                COUNT(*) FILTER (WHERE v1.centroid_dx_pix IS NOT DISTINCT FROM bm.centroid_dx_pix)::bigint AS centroid_dx_equal,
                COUNT(*) FILTER (WHERE v1.centroid_dy_pix IS NOT DISTINCT FROM bm.centroid_dy_pix)::bigint AS centroid_dy_equal,
                COUNT(*) FILTER (WHERE bm.measurement_id IS NULL)::bigint AS missing_base
            FROM gg_s1b_science_v1 v1
            LEFT JOIN measurements bm ON bm.measurement_id = v1.base_measurement_id
            """
        )
    ).mappings().one()
    return {k: int(v or 0) for k, v in parity.items()}


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _pct_saved(old: int, new: int) -> float | None:
    if old <= 0:
        return None
    return 1.0 - (new / old)


def main() -> int:
    args = _parser().parse_args()
    if args.base_frames < 1:
        raise SystemExit("--base-frames must be >= 1")
    if args.science_sample_rows < 1:
        raise SystemExit("--science-sample-rows must be >= 1")

    report_root = Path(args.s1_report_root)
    base_csv = report_root / "base_frame_groups.csv"
    science_csv = report_root / "science_v2_projection.csv"
    if not base_csv.exists():
        raise SystemExit(f"Missing S1 base report: {base_csv}")
    base_rows = _read_csv(base_csv)
    science_rows = _read_csv(science_csv) if science_csv.exists() else []
    selected_frames = _select_base_frames(base_rows, int(args.base_frames))

    out_root = Path(args.out_root)
    out_root.mkdir(parents=True, exist_ok=True)

    Session = make_session_factory(args.database_url)
    relation_rows: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "s1_report_root": str(report_root),
        "temporary_only": True,
        "public_schema_modified": False,
    }

    with Session() as session:
        if session.get_bind().dialect.name != "postgresql":
            raise RuntimeError("S1b physical probe requires PostgreSQL")
        # Unlike S1, this probe intentionally creates session-local TEMP relations.
        # It never writes public tables and the whole transaction is rolled back.
        session.execute(
            text("SELECT set_config('statement_timeout', :v, true)"),
            {"v": f"{int(args.statement_timeout_ms)}ms"},
        )
        session.execute(text("SELECT set_config('lock_timeout', '5000ms', true)"))

        measurement_columns = _table_columns(session, "measurements")
        science_columns = _table_columns(session, "science_measurements")

        print(f"BASE physical probe: selected_frames={len(selected_frames)}")
        _create_selected_base_frames(session, selected_frames)
        base_parity = _create_base_probes(session, measurement_columns)
        for name in ("gg_s1b_base_v1", "gg_s1b_base_v2", "gg_s1b_base_context"):
            relation_rows.append({"family": "BASE", **_relation_stats(session, name)})
        base_v1 = next(r for r in relation_rows if r["table"] == "gg_s1b_base_v1")
        base_v2 = next(r for r in relation_rows if r["table"] == "gg_s1b_base_v2")
        base_ctx = next(r for r in relation_rows if r["table"] == "gg_s1b_base_context")
        report["base"] = {
            "selected_frames": len(selected_frames),
            **base_parity,
            "v1_table_bytes": base_v1["table_bytes"],
            "v2_plus_context_table_bytes": base_v2["table_bytes"] + base_ctx["table_bytes"],
            "physical_table_saving_fraction": _pct_saved(
                base_v1["table_bytes"], base_v2["table_bytes"] + base_ctx["table_bytes"]
            ),
        }
        print(
            "BASE TEMP storage: "
            f"v1={base_v1['table_bytes']/1024**2:.1f} MiB "
            f"v2+context={(base_v2['table_bytes']+base_ctx['table_bytes'])/1024**2:.1f} MiB "
            f"saving={(report['base']['physical_table_saving_fraction'] or 0):.1%}"
        )

        selected_science_ids = _create_selected_science_cvs(session, science_rows)
        print(
            f"SCIENCE physical probe: calibrations={len(selected_science_ids)} "
            f"sample_rows_per_cv={args.science_sample_rows}"
        )
        sci_parity = _create_science_probes(
            session,
            science_columns,
            sample_rows=int(args.science_sample_rows),
        )
        for name in ("gg_s1b_science_v1", "gg_s1b_science_v2_compat", "gg_s1b_science_v2_core"):
            relation_rows.append({"family": "SCIENCE", **_relation_stats(session, name)})
        sci_v1 = next(r for r in relation_rows if r["table"] == "gg_s1b_science_v1")
        sci_compat = next(r for r in relation_rows if r["table"] == "gg_s1b_science_v2_compat")
        sci_core = next(r for r in relation_rows if r["table"] == "gg_s1b_science_v2_core")
        report["science"] = {
            "selected_calibration_version_ids": selected_science_ids,
            **sci_parity,
            "v1_total_bytes": sci_v1["total_bytes"],
            "v2_compat_total_bytes": sci_compat["total_bytes"],
            "v2_core_total_bytes": sci_core["total_bytes"],
            "v2_compat_saving_fraction": _pct_saved(sci_v1["total_bytes"], sci_compat["total_bytes"]),
            "v2_core_saving_fraction": _pct_saved(sci_v1["total_bytes"], sci_core["total_bytes"]),
        }
        print(
            "SCIENCE TEMP storage incl indexes: "
            f"v1={sci_v1['total_bytes']/1024**2:.1f} MiB "
            f"v2_compat={sci_compat['total_bytes']/1024**2:.1f} MiB "
            f"v2_core={sci_core['total_bytes']/1024**2:.1f} MiB"
        )

        temp_schema = session.scalar(text("SELECT pg_my_temp_schema()::bigint"))
        if not temp_schema:
            raise RuntimeError("TEMP schema was not created as expected")
        report["temp_schema_oid"] = int(temp_schema)

        # Explicit rollback is the safety boundary: all TEMP DDL/DML is discarded.
        session.rollback()

    _write_csv(out_root / "physical_relation_sizes.csv", relation_rows)
    _write_csv(out_root / "selected_base_frames.csv", selected_frames)
    (out_root / "summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    lines = [
        "# Grandgousier S1b transient physical storage probe",
        "",
        "This probe uses PostgreSQL TEMP tables only and rolls the transaction back. No public table is modified.",
        "",
        "## BASE",
        "",
        f"- sampled complete frames: {report['base']['selected_frames']}",
        f"- exact merge parity: {report['base']['parity_exact_rows']}/{report['base']['parity_rows']}",
        f"- V1 TEMP table bytes: {report['base']['v1_table_bytes']:,}",
        f"- V2 remainder + frame context bytes: {report['base']['v2_plus_context_table_bytes']:,}",
        f"- physical table saving: {(report['base']['physical_table_saving_fraction'] or 0):.2%}",
        "",
        "## SCIENCE narrow-row probe",
        "",
        f"- sampled rows: {report['science']['rows_total']:,}",
        f"- missing BASE rows: {report['science']['missing_base']:,}",
        f"- x equality: {report['science']['x_equal']:,}/{report['science']['rows_total']:,}",
        f"- y equality: {report['science']['y_equal']:,}/{report['science']['rows_total']:,}",
        f"- aperture equality: {report['science']['aperture_equal']:,}/{report['science']['rows_total']:,}",
        f"- background equality: {report['science']['background_equal']:,}/{report['science']['rows_total']:,}",
        f"- centroid dx equality: {report['science']['centroid_dx_equal']:,}/{report['science']['rows_total']:,}",
        f"- centroid dy equality: {report['science']['centroid_dy_equal']:,}/{report['science']['rows_total']:,}",
        f"- V1 total bytes incl indexes: {report['science']['v1_total_bytes']:,}",
        f"- V2 compat total bytes: {report['science']['v2_compat_total_bytes']:,}",
        f"- V2 core total bytes: {report['science']['v2_core_total_bytes']:,}",
        f"- V2 compat physical saving: {(report['science']['v2_compat_saving_fraction'] or 0):.2%}",
        f"- V2 core physical saving: {(report['science']['v2_core_saving_fraction'] or 0):.2%}",
        "",
        "The SCIENCE probe deliberately keeps the existing `flags` payload unchanged. It therefore isolates the physical benefit of dropping the six BASE-inherited columns (and, for core, `source_measurement_id`).",
    ]
    (out_root / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Report root: {out_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
