from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from internal_tools.master_remote import (
    build_master_fetch_tasks,
    download_master_task,
    get_master_database_url,
    get_master_http_base_url,
    make_master_http_session,
    read_master_inventory_csv,
    resolve_master_http_credentials,
    search_master_archive,
    search_master_archive_http,
    summarize_master_inventory,
    summarize_master_observing_series,
    write_master_fetch_report,
    write_master_inventory_csv,
)


def _print_rows(rows: list[dict], columns: list[str], fmt: str) -> None:
    if fmt == "json":
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
        return
    if fmt == "csv":
        writer = csv.DictWriter(sys.stdout, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        return
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

    print("  ".join(cell(col, widths[col]) for col in columns))
    print("  ".join("-" * widths[col] for col in columns))
    for row in rows:
        print("  ".join(cell(row.get(col), widths[col]) for col in columns))


def register_master_remote_commands(sub: argparse._SubParsersAction) -> None:
    p_master_search = sub.add_parser(
        "master-archive-search",
        help="Search the remote MASTER processed_frames archive and write a local inventory CSV",
    )
    p_master_search.add_argument("--ra", type=float, required=True, help="ICRS RA center in degrees")
    p_master_search.add_argument("--dec", type=float, required=True, help="ICRS Dec center in degrees")
    p_master_search.add_argument(
        "--radius-deg", type=float, default=0.8, help="Search half-size in Dec degrees; RA width is cos(dec)-corrected; HTTPS transport currently supports <=0.95"
    )
    p_master_search.add_argument(
        "--datetime-start", default=None, help="Optional inclusive MASTER datetime lower bound"
    )
    p_master_search.add_argument(
        "--datetime-end", default=None, help="Optional exclusive MASTER datetime upper bound"
    )
    p_master_search.add_argument(
        "--filter", dest="master_filters", action="append", default=[], help="MASTER filter; may be repeated"
    )
    p_master_search.add_argument("--limit", type=int, default=None, help="Optional row limit for smoke tests")
    p_master_search.add_argument(
        "--search-transport",
        choices=("auto", "postgres", "http"),
        default="auto",
        help="Discovery transport: auto tries PostgreSQL then HTTPS fallback; default auto",
    )
    p_master_search.add_argument(
        "--master-database-url", default=None, help="MASTER PostgreSQL URL; otherwise GRANDGOUSIER_MASTER_DATABASE_URL"
    )
    p_master_search.add_argument(
        "--http-user", default=None, help="MASTER HTTP Basic user for HTTPS discovery; otherwise GRANDGOUSIER_MASTER_HTTP_USER"
    )
    p_master_search.add_argument(
        "--http-base-url", default=None, help="MASTER HTTPS base URL; default https://master.kourovka.ru"
    )
    p_master_search.add_argument(
        "--http-page-size", type=int, default=500, help="Rows requested per processing.php page for HTTPS discovery; default 500"
    )
    p_master_search.add_argument("--out", required=True, help="Output inventory CSV")
    p_master_search.add_argument(
        "--format", choices=("table", "json"), default="table", help="Console summary format"
    )
    p_master_search.set_defaults(func=cmd_master_archive_search)

    p_master_series = sub.add_parser(
        "master-archive-series",
        help="Summarize provisional MASTER observing series from an inventory CSV",
    )
    p_master_series.add_argument("--manifest", required=True, help="Inventory CSV from master-archive-search")
    p_master_series.add_argument(
        "--max-field-separation-arcmin", type=float, default=30.0,
        help="Maximum field-center separation for one provisional field; default 30",
    )
    p_master_series.add_argument(
        "--max-series-gap-minutes", type=float, default=60.0,
        help="Start a new observing series after this time gap; default 60",
    )
    p_master_series.add_argument("--format", choices=("table", "json", "csv"), default="table", help="Output format")
    p_master_series.set_defaults(func=cmd_master_archive_series)

    p_master_fetch = sub.add_parser(
        "master-archive-fetch",
        help="Download raw/header/dark/flat files listed by master-archive-search",
    )
    p_master_fetch.add_argument("--manifest", required=True, help="Inventory CSV from master-archive-search")
    p_master_fetch.add_argument(
        "--out-root", default=r"C:\master\gpx", help=r"Local cache root; default C:\master\gpx"
    )
    p_master_fetch.add_argument(
        "--role", action="append", choices=("raw", "header", "dark", "flat"), default=[], help="Component to fetch; repeat as needed; default all"
    )
    p_master_fetch.add_argument(
        "--raw-id", dest="master_fetch_raw_ids", type=int, action="append", default=[],
        help="Restrict fetch to inventory rows with this science raw_id; may be repeated",
    )
    p_master_fetch.add_argument(
        "--http-user", default=None, help="MASTER HTTP Basic user; otherwise GRANDGOUSIER_MASTER_HTTP_USER"
    )
    p_master_fetch.add_argument(
        "--http-base-url", default=None, help="MASTER HTTPS base URL; default https://master.kourovka.ru"
    )
    p_master_fetch.add_argument("--execute", action="store_true", help="Actually download; default is plan-only")
    p_master_fetch.add_argument("--overwrite", action="store_true", help="Replace existing local files")
    p_master_fetch.add_argument("--no-checksum", action="store_true", help="Skip SHA-256 calculation")
    p_master_fetch.add_argument("--retries", type=int, default=4, help="HTTP retry count for transient failures")
    p_master_fetch.add_argument("--timeout-connect", type=float, default=15.0, help="HTTP connect timeout seconds")
    p_master_fetch.add_argument("--timeout-read", type=float, default=180.0, help="HTTP read timeout seconds")
    p_master_fetch.add_argument(
        "--report-out", default=None, help="Download report CSV; default <out-root>/master_fetch_report.csv"
    )
    p_master_fetch.add_argument(
        "--format", choices=("table", "json"), default="table", help="Console plan/result format"
    )
    p_master_fetch.set_defaults(func=cmd_master_archive_fetch)



def cmd_master_archive_series(args: argparse.Namespace) -> int:
    try:
        rows = read_master_inventory_csv(args.manifest)
        summary = summarize_master_observing_series(
            rows,
            max_field_separation_arcmin=args.max_field_separation_arcmin,
            max_series_gap_minutes=args.max_series_gap_minutes,
        )
    except Exception as exc:
        print(f"MASTER archive series failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    print(
        "MASTER archive series: "
        f"manifest_rows={len(rows)}, series={len(summary)}, "
        f"field_clusters={len({row.get('field_cluster') for row in summary})}"
    )
    columns = [
        "archive_series", "field_cluster", "night_utc", "filter", "n_frames",
        "datetime_start", "datetime_end", "duration_min",
        "center_ra_deg", "center_dec_deg", "raw_id_first", "raw_id_last",
        "unique_darks", "unique_flats",
    ]
    _print_rows(summary, columns, args.format)
    return 0


def cmd_master_archive_search(args: argparse.Namespace) -> int:
    base_url = get_master_http_base_url(args.http_base_url)
    rows = None
    transport_used = None
    postgres_error = None
    session = None
    try:
        database_url = get_master_database_url(args.master_database_url, required=False)
        if args.search_transport in {"auto", "postgres"}:
            if not database_url:
                if args.search_transport == "postgres":
                    raise RuntimeError(
                        "MASTER PostgreSQL URL is required for --search-transport postgres; "
                        "set GRANDGOUSIER_MASTER_DATABASE_URL or pass --master-database-url"
                    )
            else:
                try:
                    rows = search_master_archive(
                        database_url=database_url,
                        ra_deg=args.ra,
                        dec_deg=args.dec,
                        radius_deg=args.radius_deg,
                        datetime_start=args.datetime_start,
                        datetime_end=args.datetime_end,
                        filters=args.master_filters,
                        limit=args.limit,
                        base_url=base_url,
                    )
                    transport_used = "postgres"
                except Exception as exc:
                    if args.search_transport == "postgres":
                        raise
                    postgres_error = exc

        if rows is None:
            http_user, http_password = resolve_master_http_credentials(user=args.http_user)
            session = make_master_http_session(user=http_user, password=http_password, retries=4)
            rows = search_master_archive_http(
                session=session,
                ra_deg=args.ra,
                dec_deg=args.dec,
                radius_deg=args.radius_deg,
                datetime_start=args.datetime_start,
                datetime_end=args.datetime_end,
                filters=args.master_filters,
                limit=args.limit,
                base_url=base_url,
                page_size=args.http_page_size,
            )
            transport_used = "http" if postgres_error is None else "http_fallback"

        out_path = write_master_inventory_csv(rows, args.out)
    except Exception as exc:
        print(f"MASTER archive search failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        if session is not None:
            close = getattr(session, "close", None)
            if callable(close):
                close()

    if postgres_error is not None:
        print(
            "WARNING: MASTER PostgreSQL discovery failed; used authenticated HTTPS fallback: "
            f"{type(postgres_error).__name__}: {postgres_error}",
            file=sys.stderr,
        )

    summary = summarize_master_inventory(rows)
    print(
        "MASTER archive search: "
        f"transport={transport_used}, rows={len(rows)}, "
        f"nights={len({row.get('night_utc') for row in rows if row.get('night_utc')})}, "
        f"inventory={out_path}"
    )
    _print_rows(summary, ["night_utc", "filter", "n_frames", "unique_darks", "unique_flats"], args.format)

    unresolved = []
    for row in rows:
        for role in ("raw", "header", "dark", "flat"):
            role_path = row.get(f"{role}_path")
            role_id = row.get(f"{role}_id")
            if (role_path and not row.get(f"{role}_url")) or (role_id and not role_path):
                unresolved.append((row.get("processed_frame_id"), role, role_path or role_id))
    if unresolved:
        print(f"WARNING: unresolved remote artifacts={len(unresolved)}", file=sys.stderr)
        for processed_frame_id, role, remote_value in unresolved[:10]:
            print(
                f"  processed_frame_id={processed_frame_id} role={role} value={remote_value}",
                file=sys.stderr,
            )
        if len(unresolved) > 10:
            print(f"  ... {len(unresolved) - 10} more", file=sys.stderr)
        return 2
    return 0


def cmd_master_archive_fetch(args: argparse.Namespace) -> int:
    try:
        rows = read_master_inventory_csv(args.manifest)
        if args.master_fetch_raw_ids:
            wanted = {int(value) for value in args.master_fetch_raw_ids}
            rows = [row for row in rows if str(row.get("raw_id") or "").strip().isdigit() and int(row["raw_id"]) in wanted]
            found = {int(row["raw_id"]) for row in rows}
            missing = sorted(wanted - found)
            if missing:
                raise ValueError(f"requested raw_id values are absent from manifest: {missing}")
        roles = args.role or ["raw", "header", "dark", "flat"]
        base_url = get_master_http_base_url(args.http_base_url)
        tasks = build_master_fetch_tasks(
            rows,
            output_root=args.out_root,
            roles=roles,
            base_url=base_url,
        )
    except Exception as exc:
        print(f"MASTER archive fetch planning failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    total_roles = sum(len(task.roles) for task in tasks)
    print(
        "MASTER archive fetch plan: "
        f"manifest_rows={len(rows)}, unique_files={len(tasks)}, role_references={total_roles}, "
        f"out_root={Path(args.out_root)}"
    )
    plan_rows = [
        {
            "roles": ",".join(task.roles),
            "remote_path": task.remote_path,
            "local_path": str(task.local_path),
        }
        for task in tasks
    ]
    if args.format == "json":
        _print_rows(plan_rows, ["roles", "remote_path", "local_path"], "json")
    else:
        _print_rows(plan_rows[:50], ["roles", "remote_path", "local_path"], "table")
        if len(plan_rows) > 50:
            print(f"... {len(plan_rows) - 50} additional unique files omitted from console plan")

    if not args.execute:
        print("Plan-only: no files downloaded. Add --execute to start transfer.")
        return 0

    try:
        http_user, http_password = resolve_master_http_credentials(user=args.http_user)
        session = make_master_http_session(user=http_user, password=http_password, retries=args.retries)
    except Exception as exc:
        print(f"MASTER HTTP setup failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    results = []
    try:
        for index, task in enumerate(tasks, start=1):
            result = download_master_task(
                task,
                session=session,
                timeout_connect=args.timeout_connect,
                timeout_read=args.timeout_read,
                overwrite=args.overwrite,
                checksum=not args.no_checksum,
            )
            results.append(result)
            print(
                f"[{index}/{len(tasks)}] {result.status}: {','.join(task.roles)} "
                f"{task.remote_path} -> {task.local_path}"
            )
            if result.error:
                print(f"  {result.error}", file=sys.stderr)
    finally:
        close = getattr(session, "close", None)
        if callable(close):
            close()

    report_path = Path(args.report_out) if args.report_out else Path(args.out_root) / "master_fetch_report.csv"
    write_master_fetch_report(results, report_path)
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    print(
        "MASTER archive fetch result: "
        f"unique_files={len(results)}, downloaded={counts.get('DOWNLOADED', 0) + counts.get('DOWNLOADED_RESUMED', 0)}, "
        f"skipped_existing={counts.get('SKIPPED_EXISTING', 0)}, failed={counts.get('FAILED', 0)}, "
        f"report={report_path}"
    )
    return 0 if counts.get("FAILED", 0) == 0 else 2


