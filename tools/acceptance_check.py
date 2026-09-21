from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from grandgousier.services.acceptance import run_acceptance


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Grandgousier frozen scientific acceptance checks")
    parser.add_argument("--dataset", default="gpx_tf17_2", help="Packaged dataset name or JSON specification path")
    parser.add_argument("--config", dest="config_file", default=None, help="Grandgousier YAML/JSON config")
    parser.add_argument("--database-url", default=None, help="SQLAlchemy PostgreSQL URL; environment fallback remains supported")
    parser.add_argument("--out-root", default=None, help="Keep generated M5 CSV/summary files under this directory")
    parser.add_argument("--report-out", default=None, help="Optional JSON report output")
    parser.add_argument("--format", choices=("table", "json"), default="table")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run_acceptance(
        database_url=args.database_url,
        dataset=args.dataset,
        config_file=args.config_file,
        out_root=args.out_root,
    )
    payload = result.to_dict()
    if args.report_out:
        path = Path(args.report_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"Grandgousier acceptance: {result.status} dataset={result.dataset}")
        width = max((len(row.name) for row in result.checks), default=4)
        for row in result.checks:
            print(f"{row.name:<{width}}  {row.status:<4}  {row.detail}")
        if args.report_out:
            print(f"Report: {args.report_out}")
    return 0 if result.ready else 6


if __name__ == "__main__":
    raise SystemExit(main())
