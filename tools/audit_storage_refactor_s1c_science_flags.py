from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from grandgousier.db.session import make_session_factory

INHERITED_SCIENCE_COLUMNS = {
    "x", "y", "aperture_radius_pix", "local_background",
    "centroid_dx_pix", "centroid_dy_pix",
}


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Grandgousier 0.6.3.2 S1c TEMP-table probe. Separates physical savings from "
            "(a) narrow SCIENCE rows and (b) exact BASE-delta SCIENCE flags. "
            "Only session-local TEMP tables are written; transaction is rolled back."
        )
    )
    p.add_argument("--database-url", default=None)
    p.add_argument("--s1-report-root", default="reports/storage_refactor_s1_0632_real")
    p.add_argument("--out-root", default="reports/storage_refactor_s1c_science_flags_0632")
    p.add_argument("--science-sample-rows", type=int, default=20000)
    p.add_argument("--statement-timeout-ms", type=int, default=600000)
    return p


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _evenly_spaced(values: list[int], n: int) -> list[int]:
    if not values or n <= 0:
        return []
    if len(values) <= n:
        return values
    if n == 1:
        return [values[len(values)//2]]
    out = []
    for i in range(n):
        j = round(i * (len(values)-1) / (n-1))
        if values[j] not in out:
            out.append(values[j])
    return out


def _table_columns(session, table: str) -> list[str]:
    rows = session.execute(text("""
        SELECT a.attname
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = current_schema()
          AND c.relname = :table
          AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY a.attnum
    """), {"table": table}).scalars().all()
    if not rows:
        raise RuntimeError(f"Could not introspect {table}")
    return [str(x) for x in rows]


def _size(session, table: str) -> dict[str, int]:
    row = session.execute(text("""
        SELECT
          pg_relation_size(to_regclass(:n))::bigint AS heap_bytes,
          pg_table_size(to_regclass(:n))::bigint AS table_bytes,
          pg_indexes_size(to_regclass(:n))::bigint AS indexes_bytes,
          pg_total_relation_size(to_regclass(:n))::bigint AS total_bytes
    """), {"n": f"pg_temp.{table}"}).mappings().one()
    return {k: int(v or 0) for k, v in row.items()}


def _stats(session, table: str) -> dict[str, Any]:
    n = int(session.scalar(text(f'SELECT COUNT(*) FROM pg_temp."{table}"')) or 0)
    return {"table": table, "rows": n, **_size(session, table)}


def _create_indexes(session, table: str) -> None:
    session.execute(text(f'CREATE UNIQUE INDEX ON "{table}" (science_measurement_id)'))
    session.execute(text(
        f'CREATE UNIQUE INDEX ON "{table}" (calibration_version_id, object_id, frame_id)'
    ))
    session.execute(text(
        f'CREATE INDEX ON "{table}" (calibration_version_id, frame_id, object_id)'
    ))


def _pct(old: int, new: int) -> float:
    return 0.0 if old <= 0 else 1.0 - new / old


def main() -> int:
    args = _parser().parse_args()
    if args.science_sample_rows < 1:
        raise SystemExit("--science-sample-rows must be >= 1")

    s1 = Path(args.s1_report_root)
    projection = s1 / "science_v2_projection.csv"
    rows = _read_csv(projection) if projection.exists() else []
    cv_ids = sorted({int(r["calibration_version_id"]) for r in rows})
    if not cv_ids:
        raise SystemExit(f"No science calibration IDs found in {projection}")

    out = Path(args.out_root)
    out.mkdir(parents=True, exist_ok=True)

    Session = make_session_factory(args.database_url)
    report: dict[str, Any] = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "temporary_only": True,
        "public_schema_modified": False,
        "selected_calibration_version_ids": cv_ids,
    }
    relation_rows: list[dict[str, Any]] = []
    cv_rows: list[dict[str, Any]] = []

    with Session() as session:
        if session.get_bind().dialect.name != "postgresql":
            raise RuntimeError("S1c requires PostgreSQL")
        session.execute(text("SELECT set_config('statement_timeout', :v, true)"),
                        {"v": f"{int(args.statement_timeout_ms)}ms"})
        session.execute(text("SELECT set_config('lock_timeout', '5000ms', true)"))

        cols = _table_columns(session, "science_measurements")
        compat_cols = [c for c in cols if c not in INHERITED_SCIENCE_COLUMNS]

        session.execute(text("""
            CREATE TEMP TABLE gg_s1c_selected_cvs (
                calibration_version_id bigint PRIMARY KEY
            ) ON COMMIT DROP
        """))
        session.execute(
            text("INSERT INTO gg_s1c_selected_cvs VALUES (:cv)"),
            [{"cv": x} for x in cv_ids],
        )

        session.execute(text(f"""
            CREATE TEMP TABLE gg_s1c_ids ON COMMIT DROP AS
            SELECT science_measurement_id
            FROM (
                SELECT sm.science_measurement_id, sm.calibration_version_id,
                       ROW_NUMBER() OVER (
                         PARTITION BY sm.calibration_version_id
                         ORDER BY sm.science_measurement_id
                       ) AS rn
                FROM science_measurements sm
                JOIN gg_s1c_selected_cvs c USING (calibration_version_id)
            ) q
            WHERE rn <= {int(args.science_sample_rows)}
        """))
        session.execute(text("CREATE UNIQUE INDEX ON gg_s1c_ids (science_measurement_id)"))

        # Exact effective baseline and exact delta are materialized once for all variants.
        session.execute(text("""
            CREATE TEMP TABLE gg_s1c_source ON COMMIT DROP AS
            SELECT
                sm.*,
                COALESCE(bm.flags, '{}'::jsonb) AS base_flags,
                (COALESCE(bm.flags, '{}'::jsonb) || COALESCE(sm.flags, '{}'::jsonb))
                    AS effective_flags
            FROM science_measurements sm
            JOIN gg_s1c_ids i USING (science_measurement_id)
            JOIN measurements bm ON bm.measurement_id = sm.base_measurement_id
        """))
        session.execute(text("CREATE UNIQUE INDEX ON gg_s1c_source (science_measurement_id)"))

        session.execute(text("""
            CREATE TEMP TABLE gg_s1c_delta ON COMMIT DROP AS
            SELECT
                s.science_measurement_id,
                COALESCE(
                    jsonb_object_agg(e.key, e.value)
                      FILTER (
                        WHERE NOT (s.base_flags ? e.key)
                           OR (s.base_flags -> e.key) IS DISTINCT FROM e.value
                      ),
                    '{}'::jsonb
                ) AS delta_flags
            FROM gg_s1c_source s
            CROSS JOIN LATERAL jsonb_each(s.effective_flags) e(key, value)
            GROUP BY s.science_measurement_id
        """))
        session.execute(text("CREATE UNIQUE INDEX ON gg_s1c_delta (science_measurement_id)"))

        # V1: exact current physical row.
        session.execute(text("""
            CREATE TEMP TABLE gg_s1c_v1 ON COMMIT DROP AS
            SELECT sm.*
            FROM science_measurements sm
            JOIN gg_s1c_ids i USING (science_measurement_id)
        """))

        # Same columns as V1, only flags converted to exact BASE delta.
        full_select = []
        for c in cols:
            full_select.append("d.delta_flags AS flags" if c == "flags" else f's."{c}"')
        session.execute(text(f"""
            CREATE TEMP TABLE gg_s1c_full_delta ON COMMIT DROP AS
            SELECT {", ".join(full_select)}
            FROM gg_s1c_source s
            JOIN gg_s1c_delta d USING (science_measurement_id)
        """))

        # Narrow row, old flags: isolates scalar-column removal.
        narrow_same = [f's."{c}"' for c in compat_cols]
        session.execute(text(f"""
            CREATE TEMP TABLE gg_s1c_narrow_sameflags ON COMMIT DROP AS
            SELECT {", ".join(narrow_same)}
            FROM gg_s1c_source s
        """))

        # Narrow row + exact BASE-delta flags: target combined physical model.
        narrow_delta = []
        for c in compat_cols:
            narrow_delta.append("d.delta_flags AS flags" if c == "flags" else f's."{c}"')
        session.execute(text(f"""
            CREATE TEMP TABLE gg_s1c_narrow_delta ON COMMIT DROP AS
            SELECT {", ".join(narrow_delta)}
            FROM gg_s1c_source s
            JOIN gg_s1c_delta d USING (science_measurement_id)
        """))

        tables = [
            "gg_s1c_v1", "gg_s1c_full_delta",
            "gg_s1c_narrow_sameflags", "gg_s1c_narrow_delta",
        ]
        for t in tables:
            _create_indexes(session, t)

        parity = session.execute(text("""
            SELECT
              COUNT(*)::bigint AS rows_total,
              COUNT(*) FILTER (
                WHERE (s.base_flags || d.delta_flags) = s.effective_flags
              )::bigint AS exact_effective_rows,
              COUNT(*) FILTER (WHERE s.base_measurement_id IS NULL)::bigint AS null_base_ids,
              AVG(pg_column_size(COALESCE(s.flags, '{}'::jsonb)))::float8 AS avg_old_flags_pg,
              AVG(pg_column_size(d.delta_flags))::float8 AS avg_delta_flags_pg,
              AVG(octet_length(COALESCE(s.flags, '{}'::jsonb)::text))::float8 AS avg_old_flags_text,
              AVG(octet_length(d.delta_flags::text))::float8 AS avg_delta_flags_text
            FROM gg_s1c_source s
            JOIN gg_s1c_delta d USING (science_measurement_id)
        """)).mappings().one()

        report["parity"] = {
            k: (int(v) if k in {"rows_total","exact_effective_rows","null_base_ids"} else float(v or 0.0))
            for k, v in parity.items()
        }

        for t in tables:
            relation_rows.append(_stats(session, t))

        by_name = {r["table"]: r for r in relation_rows}
        old = by_name["gg_s1c_v1"]["total_bytes"]
        report["storage"] = {
            "v1_total_bytes": old,
            "full_delta_total_bytes": by_name["gg_s1c_full_delta"]["total_bytes"],
            "narrow_sameflags_total_bytes": by_name["gg_s1c_narrow_sameflags"]["total_bytes"],
            "narrow_delta_total_bytes": by_name["gg_s1c_narrow_delta"]["total_bytes"],
            "full_delta_saving_fraction": _pct(old, by_name["gg_s1c_full_delta"]["total_bytes"]),
            "narrow_sameflags_saving_fraction": _pct(old, by_name["gg_s1c_narrow_sameflags"]["total_bytes"]),
            "narrow_delta_saving_fraction": _pct(old, by_name["gg_s1c_narrow_delta"]["total_bytes"]),
        }

        for cv in cv_ids:
            r = session.execute(text("""
                SELECT
                  CAST(:cv AS bigint) AS calibration_version_id,
                  COUNT(*)::bigint AS rows,
                  AVG(pg_column_size(COALESCE(s.flags, '{}'::jsonb)))::float8 AS avg_old_flags_pg,
                  AVG(pg_column_size(d.delta_flags))::float8 AS avg_delta_flags_pg,
                  AVG(octet_length(COALESCE(s.flags, '{}'::jsonb)::text))::float8 AS avg_old_flags_text,
                  AVG(octet_length(d.delta_flags::text))::float8 AS avg_delta_flags_text,
                  COUNT(*) FILTER (
                    WHERE (s.base_flags || d.delta_flags) = s.effective_flags
                  )::bigint AS exact_rows
                FROM gg_s1c_source s
                JOIN gg_s1c_delta d USING (science_measurement_id)
                WHERE s.calibration_version_id = :cv
            """), {"cv": cv}).mappings().one()
            cv_rows.append(dict(r))

        print(
            f"SCIENCE S1c: rows={report['parity']['rows_total']:,} "
            f"exact={report['parity']['exact_effective_rows']:,}"
        )
        for k, label in [
            ("full_delta_total_bytes", "full-row + delta flags"),
            ("narrow_sameflags_total_bytes", "narrow + old flags"),
            ("narrow_delta_total_bytes", "narrow + delta flags"),
        ]:
            b = report["storage"][k]
            print(f"  {label}: {b/1024**2:.1f} MiB; saving={_pct(old,b):.1%}")
        print(
            f"  V1: {old/1024**2:.1f} MiB; "
            f"avg flags pg {report['parity']['avg_old_flags_pg']:.1f} -> "
            f"{report['parity']['avg_delta_flags_pg']:.1f} B"
        )

        # Explicit rollback destroys TEMP objects; no persistent/public data is modified.
        session.rollback()

    # CSVs
    if relation_rows:
        with (out/"physical_relation_sizes.csv").open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(relation_rows[0].keys()))
            w.writeheader(); w.writerows(relation_rows)
    if cv_rows:
        with (out/"per_calibration_flags.csv").open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(cv_rows[0].keys()))
            w.writeheader(); w.writerows(cv_rows)

    (out/"summary.json").write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    st = report["storage"]; pa = report["parity"]
    md = f"""# Grandgousier S1c SCIENCE exact-delta physical probe

TEMP tables only; transaction rolled back; public schema/data unchanged.

- rows: {pa['rows_total']:,}
- exact effective-flag parity: {pa['exact_effective_rows']:,}/{pa['rows_total']:,}
- average stored/current flag datum: {pa['avg_old_flags_pg']:.1f} B
- average exact BASE-delta flag datum: {pa['avg_delta_flags_pg']:.1f} B

## Physical storage including equivalent indexes

- V1 current row: {st['v1_total_bytes']/1024**2:.1f} MiB
- full row + exact delta flags: {st['full_delta_total_bytes']/1024**2:.1f} MiB ({st['full_delta_saving_fraction']:.2%} saving)
- narrow row + unchanged flags: {st['narrow_sameflags_total_bytes']/1024**2:.1f} MiB ({st['narrow_sameflags_saving_fraction']:.2%} saving)
- narrow row + exact delta flags: {st['narrow_delta_total_bytes']/1024**2:.1f} MiB ({st['narrow_delta_saving_fraction']:.2%} saving)
"""
    (out/"summary.md").write_text(md, encoding="utf-8")
    print(f"Report root: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
