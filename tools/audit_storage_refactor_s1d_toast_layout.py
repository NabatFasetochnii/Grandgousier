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
DEFAULT_TOAST_TARGETS = [512, 1024, 1536, 2040]


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Grandgousier 0.6.3.2 S1d TEMP-table probe. Measures how PostgreSQL "
            "toast_tuple_target changes the physical size of narrow SCIENCE rows "
            "with exact BASE-delta flags. Only session-local TEMP tables are written; "
            "the transaction is rolled back."
        )
    )
    p.add_argument("--database-url", default=None)
    p.add_argument("--s1-report-root", default="reports/storage_refactor_s1_0632_real")
    p.add_argument("--out-root", default="reports/storage_refactor_s1d_toast_layout_0632")
    p.add_argument("--science-sample-rows", type=int, default=20000)
    p.add_argument(
        "--toast-target", action="append", type=int, dest="toast_targets",
        help="toast_tuple_target bytes; may be repeated. Default: 512,1024,1536,2040",
    )
    p.add_argument("--statement-timeout-ms", type=int, default=600000)
    return p


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


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
    out = {k: int(v or 0) for k, v in row.items()}
    out["table_aux_bytes"] = max(0, out["table_bytes"] - out["heap_bytes"])
    return out


def _stats(session, table: str, *, variant: str, toast_target: int | None) -> dict[str, Any]:
    n = int(session.scalar(text(f'SELECT COUNT(*) FROM pg_temp."{table}"')) or 0)
    size = _size(session, table)
    return {
        "variant": variant,
        "toast_tuple_target": "default" if toast_target is None else toast_target,
        "rows": n,
        **size,
        "heap_bytes_per_row": (size["heap_bytes"] / n) if n else 0.0,
        "table_aux_bytes_per_row": (size["table_aux_bytes"] / n) if n else 0.0,
        "total_bytes_per_row": (size["total_bytes"] / n) if n else 0.0,
    }


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


def _create_and_measure(
    session,
    *,
    table: str,
    select_sql: str,
    variant: str,
    toast_target: int | None,
) -> dict[str, Any]:
    session.execute(text(f'CREATE TEMP TABLE "{table}" ON COMMIT DROP AS {select_sql} WITH NO DATA'))
    if toast_target is not None:
        # Set before INSERT so TOAST decisions are made with the requested target.
        session.execute(text(f'ALTER TABLE "{table}" SET (toast_tuple_target = {int(toast_target)})'))
    session.execute(text(f'INSERT INTO "{table}" {select_sql}'))
    _create_indexes(session, table)
    session.execute(text(f'ANALYZE "{table}"'))
    result = _stats(session, table, variant=variant, toast_target=toast_target)
    session.execute(text(f'DROP TABLE "{table}"'))
    return result


def main() -> int:
    args = _parser().parse_args()
    if args.science_sample_rows < 1:
        raise SystemExit("--science-sample-rows must be >= 1")
    targets = args.toast_targets or list(DEFAULT_TOAST_TARGETS)
    if any(t < 128 or t > 8160 for t in targets):
        raise SystemExit("--toast-target must be between 128 and 8160 bytes")
    targets = list(dict.fromkeys(targets))

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
        "toast_targets": targets,
    }
    relation_rows: list[dict[str, Any]] = []

    with Session() as session:
        if session.get_bind().dialect.name != "postgresql":
            raise RuntimeError("S1d requires PostgreSQL")
        session.execute(text("SELECT set_config('statement_timeout', :v, true)"),
                        {"v": f"{int(args.statement_timeout_ms)}ms"})
        session.execute(text("SELECT set_config('lock_timeout', '5000ms', true)"))

        env = session.execute(text("""
            SELECT
              current_setting('server_version') AS server_version,
              current_setting('block_size') AS block_size,
              current_setting('default_toast_compression', true) AS default_toast_compression
        """)).mappings().one()
        report["postgresql"] = dict(env)

        cols = _table_columns(session, "science_measurements")
        compat_cols = [c for c in cols if c not in INHERITED_SCIENCE_COLUMNS]

        session.execute(text("""
            CREATE TEMP TABLE gg_s1d_selected_cvs (
                calibration_version_id bigint PRIMARY KEY
            ) ON COMMIT DROP
        """))
        session.execute(
            text("INSERT INTO gg_s1d_selected_cvs VALUES (:cv)"),
            [{"cv": x} for x in cv_ids],
        )

        session.execute(text(f"""
            CREATE TEMP TABLE gg_s1d_ids ON COMMIT DROP AS
            SELECT science_measurement_id
            FROM (
                SELECT sm.science_measurement_id, sm.calibration_version_id,
                       ROW_NUMBER() OVER (
                         PARTITION BY sm.calibration_version_id
                         ORDER BY sm.science_measurement_id
                       ) AS rn
                FROM science_measurements sm
                JOIN gg_s1d_selected_cvs c USING (calibration_version_id)
            ) q
            WHERE rn <= {int(args.science_sample_rows)}
        """))
        session.execute(text("CREATE UNIQUE INDEX ON gg_s1d_ids (science_measurement_id)"))

        # Keep only the compact delta payload in TEMP storage. The exact historical
        # effective flags are read from the live V1 row and BASE row while computing it.
        session.execute(text("""
            CREATE TEMP TABLE gg_s1d_delta ON COMMIT DROP AS
            SELECT
                sm.science_measurement_id,
                COALESCE(
                    jsonb_object_agg(e.key, e.value)
                      FILTER (
                        WHERE NOT (COALESCE(bm.flags, '{}'::jsonb) ? e.key)
                           OR (COALESCE(bm.flags, '{}'::jsonb) -> e.key)
                              IS DISTINCT FROM e.value
                      ),
                    '{}'::jsonb
                ) AS delta_flags
            FROM science_measurements sm
            JOIN gg_s1d_ids i USING (science_measurement_id)
            JOIN measurements bm ON bm.measurement_id = sm.base_measurement_id
            CROSS JOIN LATERAL jsonb_each(
                COALESCE(bm.flags, '{}'::jsonb) || COALESCE(sm.flags, '{}'::jsonb)
            ) e(key, value)
            GROUP BY sm.science_measurement_id
        """))
        session.execute(text("CREATE UNIQUE INDEX ON gg_s1d_delta (science_measurement_id)"))

        parity = session.execute(text("""
            SELECT
              COUNT(*)::bigint AS rows_total,
              COUNT(*) FILTER (
                WHERE (
                    COALESCE(bm.flags, '{}'::jsonb) || d.delta_flags
                ) = (
                    COALESCE(bm.flags, '{}'::jsonb) || COALESCE(sm.flags, '{}'::jsonb)
                )
              )::bigint AS exact_effective_rows,
              COUNT(*) FILTER (WHERE sm.base_measurement_id IS NULL)::bigint AS null_base_ids,
              AVG(pg_column_size(COALESCE(sm.flags, '{}'::jsonb)))::float8 AS avg_old_flags_pg,
              AVG(pg_column_size(d.delta_flags))::float8 AS avg_delta_flags_pg,
              AVG(octet_length(COALESCE(sm.flags, '{}'::jsonb)::text))::float8 AS avg_old_flags_text,
              AVG(octet_length(d.delta_flags::text))::float8 AS avg_delta_flags_text
            FROM science_measurements sm
            JOIN gg_s1d_ids i USING (science_measurement_id)
            JOIN measurements bm ON bm.measurement_id = sm.base_measurement_id
            JOIN gg_s1d_delta d USING (science_measurement_id)
        """)).mappings().one()
        report["parity"] = {
            k: (int(v) if k in {"rows_total", "exact_effective_rows", "null_base_ids"}
                else float(v or 0.0))
            for k, v in parity.items()
        }
        if report["parity"]["exact_effective_rows"] != report["parity"]["rows_total"]:
            raise RuntimeError("Exact effective-flag parity failed; refusing physical comparison")

        v1_select = """
            SELECT sm.*
            FROM science_measurements sm
            JOIN gg_s1d_ids i USING (science_measurement_id)
        """
        relation_rows.append(_create_and_measure(
            session, table="gg_s1d_v1", select_sql=v1_select,
            variant="v1_current", toast_target=None,
        ))

        full_select = ["d.delta_flags AS flags" if c == "flags" else f'sm."{c}"' for c in cols]
        full_delta_select = f"""
            SELECT {', '.join(full_select)}
            FROM science_measurements sm
            JOIN gg_s1d_ids i USING (science_measurement_id)
            JOIN gg_s1d_delta d USING (science_measurement_id)
        """
        relation_rows.append(_create_and_measure(
            session, table="gg_s1d_full_delta", select_sql=full_delta_select,
            variant="full_row_delta", toast_target=None,
        ))

        narrow_select_cols = [
            "d.delta_flags AS flags" if c == "flags" else f'sm."{c}"'
            for c in compat_cols
        ]
        narrow_delta_select = f"""
            SELECT {', '.join(narrow_select_cols)}
            FROM science_measurements sm
            JOIN gg_s1d_ids i USING (science_measurement_id)
            JOIN gg_s1d_delta d USING (science_measurement_id)
        """

        relation_rows.append(_create_and_measure(
            session, table="gg_s1d_narrow_default", select_sql=narrow_delta_select,
            variant="narrow_delta_default", toast_target=None,
        ))
        for target in targets:
            relation_rows.append(_create_and_measure(
                session,
                table=f"gg_s1d_narrow_t{target}",
                select_sql=narrow_delta_select,
                variant="narrow_delta_tuned",
                toast_target=target,
            ))

        by_variant = {r["variant"]: r for r in relation_rows if r["variant"] != "narrow_delta_tuned"}
        old = by_variant["v1_current"]["total_bytes"]
        candidates = [r for r in relation_rows if r["variant"].startswith("narrow_delta")]
        best = min(candidates, key=lambda r: r["total_bytes"])
        report["storage"] = {
            "v1_total_bytes": old,
            "full_delta_total_bytes": by_variant["full_row_delta"]["total_bytes"],
            "narrow_default_total_bytes": by_variant["narrow_delta_default"]["total_bytes"],
            "best_narrow_variant": best["variant"],
            "best_narrow_toast_tuple_target": best["toast_tuple_target"],
            "best_narrow_total_bytes": best["total_bytes"],
            "full_delta_saving_fraction": _pct(old, by_variant["full_row_delta"]["total_bytes"]),
            "narrow_default_saving_fraction": _pct(old, by_variant["narrow_delta_default"]["total_bytes"]),
            "best_narrow_saving_fraction": _pct(old, best["total_bytes"]),
        }

        print(
            f"SCIENCE S1d: rows={report['parity']['rows_total']:,} "
            f"exact={report['parity']['exact_effective_rows']:,}"
        )
        for r in relation_rows:
            target = r["toast_tuple_target"]
            print(
                f"  {r['variant']} target={target}: "
                f"total={r['total_bytes']/1024**2:.1f} MiB "
                f"heap={r['heap_bytes']/1024**2:.1f} MiB "
                f"aux={r['table_aux_bytes']/1024**2:.1f} MiB "
                f"saving={_pct(old, r['total_bytes']):.1%}"
            )
        print(
            f"  BEST narrow: target={best['toast_tuple_target']} "
            f"total={best['total_bytes']/1024**2:.1f} MiB "
            f"saving={_pct(old,best['total_bytes']):.1%}"
        )

        # Explicit rollback removes all remaining TEMP objects.
        session.rollback()

    if relation_rows:
        with (out / "physical_relation_sizes.csv").open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(relation_rows[0].keys()))
            w.writeheader()
            w.writerows(relation_rows)

    (out / "summary.json").write_text(
        json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
    )
    st = report["storage"]
    pa = report["parity"]
    lines = [
        "# Grandgousier S1d SCIENCE TOAST-layout physical probe",
        "",
        "TEMP tables only; transaction rolled back; public schema/data unchanged.",
        "",
        f"- rows: {pa['rows_total']:,}",
        f"- exact effective-flag parity: {pa['exact_effective_rows']:,}/{pa['rows_total']:,}",
        f"- V1 total: {st['v1_total_bytes']/1024**2:.1f} MiB",
        f"- full-row + exact delta: {st['full_delta_total_bytes']/1024**2:.1f} MiB ({st['full_delta_saving_fraction']:.2%} saving)",
        f"- narrow + exact delta, default layout: {st['narrow_default_total_bytes']/1024**2:.1f} MiB ({st['narrow_default_saving_fraction']:.2%} saving)",
        f"- best narrow target: {st['best_narrow_toast_tuple_target']}",
        f"- best narrow total: {st['best_narrow_total_bytes']/1024**2:.1f} MiB ({st['best_narrow_saving_fraction']:.2%} saving)",
        "",
        "See physical_relation_sizes.csv for heap/table-aux/index breakdown by toast_tuple_target.",
    ]
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Report root: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
