from __future__ import annotations

import csv
import json
import html
import math
import re
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from grandgousier.lightcurves.product import (
    DEFAULT_REJECT_FLAGS,
    LightCurveSelectionError,
    export_science_light_curve_csv,
    load_science_light_curve_rows,
    preflight_science_light_curve_selection,
)
from grandgousier.quality.candidate_triage import LOW_AMPLITUDE_DIAGNOSTIC_COLUMNS
from grandgousier.quality.candidate_axis_correlation import CANDIDATE_AXIS_CORRELATION_COLUMNS
from grandgousier.visualization.lightcurve_plot import (
    format_lightcurve_identity_subtitle,
    set_lightcurve_plot_title,
)


CANDIDATE_SUMMARY_COLUMNS: tuple[str, ...] = (
    "rank",
    "candidate_status",
    "candidate_rank_score",
    "candidate_export_status",
    "accepted_fraction",
    "candidate_origin",
    "candidate_diagnostic_class",
    "good_frame_confirmation_status",
    "good_frame_count",
    "good_frame_min_required",
    "good_frame_candidate_status",
    "good_frame_candidate_origin",
    "good_frame_n_measurements",
    "good_frame_mag_rms",
    "good_frame_mag_mad",
    "good_frame_robust_peak_to_peak",
    "good_frame_rms_over_mag_err",
    "good_frame_mad_over_mag_err",
    "good_frame_intrinsic_variability_score",
    *CANDIDATE_AXIS_CORRELATION_COLUMNS,
    "candidate_reject_override",
    "membership_change",
    "selection_candidate_status",
    "science_candidate_status",
    "selection_candidate_origin",
    "science_candidate_origin",
    *LOW_AMPLITUDE_DIAGNOSTIC_COLUMNS,
    "is_target",
    "target_reason",
    "object_id",
    "catalog_name",
    "source_id",
    "ra_deg",
    "dec_deg",
    "n_measurements",
    "n_points",
    "n_accepted",
    "n_rejected",
    "interseries_generation_ids",
    "mag_median",
    "mag_rms",
    "mag_mad",
    "mag_p10",
    "mag_p90",
    "robust_peak_to_peak",
    "mag_err_median",
    "mag_err_systematic_median",
    "mag_err_detector_median",
    "mag_err_epoch_floor_median",
    "mag_err_effective_detector_only_median",
    "mag_err_effective_total_median",
    "mag_err_effective_median",
    "residual_noise_floor_applied",
    "residual_floor_ab_rms_over_mag_err",
    "residual_floor_ab_mad_over_mag_err",
    "residual_floor_ab_low_amplitude_passed",
    "residual_floor_ab_failed_criteria",
    "residual_floor_ab_min_threshold_ratio",
    "residual_floor_ab_status",
    "baseline_rms",
    "rms_excess",
    "rms_over_mag_err",
    "mad_over_mag_err",
    "intrinsic_variability_score",
    "detection_fraction",
    "detection_frame_fraction",
    "n_detection_matches",
    "nearest_neighbor_arcsec",
    "separation_index",
    "mag_ref_delta",
    "abs_mag_ref_delta",
    "reference_residual_status",
    "object_qc_flags",
    "rejection_reasons",
    "light_curve_csv",
    "light_curve_json",
    "light_curve_png",
)


@dataclass
class CandidateLightCurveProduct:
    rows_total: int = 0
    rows_accepted: int = 0
    rows_rejected: int = 0
    triage_accepted_candidates: int = 0
    accepted_candidates: int = 0
    exported_light_curves: int = 0
    candidate_plot_count: int = 0
    candidate_plot_warnings_count: int = 0
    plot_backend: str | None = None
    diagnostic_light_curves: int = 0
    rejected_no_accepted_points: int = 0
    rejected_no_points: int = 0
    skipped_candidates: int = 0
    candidates_dir: str | None = None
    candidates_summary_csv: str | None = None
    candidates_summary_json: str | None = None
    candidates_index_html: str | None = None
    borderline_candidates: int = 0
    near_threshold_candidates: int = 0
    target_diagnostic_candidates: int = 0
    borderline_candidates_csv: str | None = None
    borderline_candidates_json: str | None = None
    near_threshold_candidates_csv: str | None = None
    near_threshold_candidates_json: str | None = None
    residual_floor_candidate_ab_csv: str | None = None
    residual_floor_candidate_ab_json: str | None = None
    target_diagnostics_csv: str | None = None
    target_diagnostics_json: str | None = None
    membership_history_json: str | None = None
    interseries_generation_ids: list[int] = field(default_factory=list)
    interseries_generation_scopes: list[dict[str, Any]] = field(default_factory=list)
    summary_rows: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows_total": self.rows_total,
            "rows_accepted": self.rows_accepted,
            "rows_rejected": self.rows_rejected,
            "triage_accepted_candidates": self.triage_accepted_candidates,
            "accepted_candidates": self.accepted_candidates,
            "exported_light_curves": self.exported_light_curves,
            "candidate_plot_count": self.candidate_plot_count,
            "candidate_plot_warnings_count": self.candidate_plot_warnings_count,
            "plot_backend": self.plot_backend,
            "diagnostic_light_curves": self.diagnostic_light_curves,
            "rejected_no_accepted_points": self.rejected_no_accepted_points,
            "rejected_no_points": self.rejected_no_points,
            "skipped_candidates": self.skipped_candidates,
            "candidates_dir": self.candidates_dir,
            "candidates_summary_csv": self.candidates_summary_csv,
            "candidates_summary_json": self.candidates_summary_json,
            "candidates_index_html": self.candidates_index_html,
            "borderline_candidates": self.borderline_candidates,
            "near_threshold_candidates": self.near_threshold_candidates,
            "target_diagnostic_candidates": self.target_diagnostic_candidates,
            "borderline_candidates_csv": self.borderline_candidates_csv,
            "borderline_candidates_json": self.borderline_candidates_json,
            "near_threshold_candidates_csv": self.near_threshold_candidates_csv,
            "near_threshold_candidates_json": self.near_threshold_candidates_json,
            "residual_floor_candidate_ab_csv": self.residual_floor_candidate_ab_csv,
            "residual_floor_candidate_ab_json": self.residual_floor_candidate_ab_json,
            "target_diagnostics_csv": self.target_diagnostics_csv,
            "target_diagnostics_json": self.target_diagnostics_json,
            "membership_history_json": self.membership_history_json,
            "interseries_generation_ids": list(self.interseries_generation_ids),
            "interseries_generation_scopes": list(self.interseries_generation_scopes),
            "summary_rows": self.summary_rows,
            "warnings": list(self.warnings),
        }


def candidate_summary_columns() -> list[str]:
    return list(CANDIDATE_SUMMARY_COLUMNS)


def export_accepted_candidate_light_curves(
    session: Session,
    *,
    candidate_rows: list[dict[str, Any]],
    calibration_name: str,
    catalog_name: str | None = None,
    series_id: int | None = None,
    frame_ids: list[int] | None = None,
    interseries_generation_ids: list[int] | None = None,
    reject_flags: set[str] | None = None,
    out_dir: str | Path,
    limit: int | None = None,
    export_plots: bool = True,
    membership_history: list[dict[str, Any]] | None = None,
) -> CandidateLightCurveProduct:
    """Build candidate products in a staging directory and replace the old tree.

    A failed export leaves the previously published candidate directory intact.
    A successful export replaces the complete directory, so stale object files
    from earlier candidate selections cannot survive a rerun.
    """

    final_out_path = Path(out_dir)
    scope_series_id = None if frame_ids else series_id
    selection_scope = preflight_science_light_curve_selection(
        session,
        calibration_names=[calibration_name],
        catalog_name=catalog_name,
        series_id=scope_series_id,
        frame_ids=frame_ids,
        interseries_generation_ids=interseries_generation_ids,
    )
    final_out_path.parent.mkdir(parents=True, exist_ok=True)
    staging_out_path = final_out_path.with_name(
        f".{final_out_path.name}.staging-{uuid.uuid4().hex}"
    )
    if staging_out_path.exists():
        shutil.rmtree(staging_out_path)
    staging_out_path.mkdir(parents=True, exist_ok=False)
    try:
        product = _export_accepted_candidate_light_curves_impl(
            session,
            candidate_rows=candidate_rows,
            calibration_name=calibration_name,
            catalog_name=catalog_name,
            series_id=series_id,
            frame_ids=frame_ids,
            interseries_generation_ids=interseries_generation_ids,
            reject_flags=reject_flags,
            out_dir=staging_out_path,
            public_out_dir=final_out_path,
            limit=limit,
            export_plots=export_plots,
            membership_history=membership_history,
            selection_scope=selection_scope,
        )
        _replace_directory_tree(staging_out_path, final_out_path)
        return product
    except Exception:
        if staging_out_path.exists():
            shutil.rmtree(staging_out_path, ignore_errors=True)
        raise


def _export_accepted_candidate_light_curves_impl(
    session: Session,
    *,
    candidate_rows: list[dict[str, Any]],
    calibration_name: str,
    catalog_name: str | None = None,
    series_id: int | None = None,
    frame_ids: list[int] | None = None,
    interseries_generation_ids: list[int] | None = None,
    reject_flags: set[str] | None = None,
    out_dir: str | Path,
    public_out_dir: str | Path,
    limit: int | None = None,
    export_plots: bool = True,
    membership_history: list[dict[str, Any]] | None = None,
    selection_scope: Any | None = None,
) -> CandidateLightCurveProduct:
    """Export one CSV/JSON light curve per accepted variable candidate.

    ``candidate_rows`` is normally taken directly from the field-QC candidate
    triage.  The function writes a compact candidate summary plus per-object
    science-light-curve products, optional PNG quick-look plots, and an HTML index.  Explicit ``frame_ids`` are preferred over
    ``series_id`` for the same path-scoping reason as target light-curve export.
    """

    product = CandidateLightCurveProduct(rows_total=len(candidate_rows))
    if selection_scope is not None:
        product.interseries_generation_ids = list(selection_scope.interseries_generation_ids)
        product.interseries_generation_scopes = list(selection_scope.interseries_generation_scopes)
        product.warnings.extend(selection_scope.warnings)
    out_path = Path(out_dir)
    public_out_path = Path(public_out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    product.candidates_dir = str(public_out_path)
    plot_backend, plot_backend_warning = _candidate_plot_backend(enabled=export_plots)
    product.plot_backend = plot_backend
    if plot_backend_warning:
        _append_plot_warning(product, plot_backend_warning)

    accepted = [dict(row) for row in candidate_rows if str(row.get("candidate_status") or "").upper() == "ACCEPTED"]
    ranked_accepted = rank_candidate_rows(accepted)
    product.rows_accepted = len(ranked_accepted)
    product.rows_rejected = sum(1 for row in candidate_rows if str(row.get("candidate_status") or "").upper() == "REJECTED")
    if limit is not None:
        ranked_accepted = ranked_accepted[: max(0, int(limit))]
    product.triage_accepted_candidates = len(ranked_accepted)

    diagnostic_rows = [
        dict(row) for row in candidate_rows
        if str(row.get("candidate_status") or "").upper() in {"BORDERLINE", "DIAGNOSTIC"}
        or (bool(row.get("is_target")) and str(row.get("candidate_status") or "").upper() != "ACCEPTED")
    ]
    diagnostic_rows.sort(
        key=lambda row: (
            0 if row.get("is_target") else 1,
            -float(row.get("low_amplitude_min_threshold_ratio") or 0.0),
            int(row.get("object_id") or 0),
        )
    )
    ranked = list(ranked_accepted)
    for row in diagnostic_rows:
        row["candidate_rank_score"] = candidate_rank_score(row)
        row["rank"] = len(ranked) + 1
        ranked.append(row)
    product.borderline_candidates = sum(1 for row in diagnostic_rows if str(row.get("candidate_status") or "").upper() == "BORDERLINE")
    product.near_threshold_candidates = sum(1 for row in diagnostic_rows if str(row.get("candidate_status") or "").upper() == "DIAGNOSTIC")
    product.target_diagnostic_candidates = sum(1 for row in diagnostic_rows if row.get("is_target"))

    scope_series_id = None if frame_ids else series_id
    summary_rows: list[dict[str, Any]] = []
    for row in ranked:
        object_id = _to_int(row.get("object_id"))
        if object_id is None:
            product.skipped_candidates += 1
            product.warnings.append("candidate row without object_id was skipped")
            continue
        source_id = row.get("source_id")
        stem = _candidate_file_stem(object_id=object_id, source_id=source_id)
        csv_path = out_path / f"{stem}_light_curve.csv"
        json_path = out_path / f"{stem}_summary.json"
        public_csv_path = public_out_path / f"{stem}_light_curve.csv"
        public_json_path = public_out_path / f"{stem}_summary.json"
        try:
            candidate_reject_flags, candidate_reject_override = _candidate_reject_flags_for_row(
                row, reject_flags
            )
            if candidate_reject_override:
                row["candidate_reject_override"] = candidate_reject_override
            light_curve_scope, lc_rows = load_science_light_curve_rows(
                session,
                calibration_names=[calibration_name],
                catalog_name=catalog_name,
                object_id=object_id,
                series_id=scope_series_id,
                frame_ids=frame_ids,
                interseries_generation_ids=(
                    interseries_generation_ids
                    if interseries_generation_ids is not None
                    else (product.interseries_generation_ids or None)
                ),
                reject_flags=candidate_reject_flags,
                exclude_rejected=False,
            )
            if (
                light_curve_scope is not None
                and (
                    list(getattr(light_curve_scope, "interseries_generation_ids", []) or [])
                    != product.interseries_generation_ids
                    or list(
                        getattr(light_curve_scope, "interseries_generation_scopes", []) or []
                    )
                    != product.interseries_generation_scopes
                )
            ):
                raise LightCurveSelectionError(
                    "M4 generation selection changed after candidate-export preflight; refusing staged output"
                )
            lc_summary = _summarize_light_curve(lc_rows)
            export_status = _candidate_export_status_for_row(row, lc_summary)
            lc_summary["candidate_export_status"] = export_status
            png_path = out_path / f"{stem}_light_curve.png"
            public_png_path = public_out_path / f"{stem}_light_curve.png"
            png_path_str: str | None = None
            plot_warning: str | None = None
            if _is_exported_status(export_status) and export_plots and plot_backend == "matplotlib":
                try:
                    _export_candidate_light_curve_plot(
                        lc_rows,
                        png_path,
                        candidate_row=row,
                        light_curve_summary=lc_summary,
                    )
                    png_path_str = str(public_png_path)
                    product.candidate_plot_count += 1
                except Exception as exc:  # noqa: BLE001 - plotting is a quick-look product only.
                    plot_warning = f"object_id={object_id} plot failed: {type(exc).__name__}: {exc}"
                    _append_plot_warning(product, plot_warning)

            science_candidate_row = _candidate_row_with_light_curve_metrics(row, lc_summary)
            science_candidate_row["interseries_generation_ids"] = ",".join(
                str(value) for value in product.interseries_generation_ids
            )
            json_payload = {
                "candidate": _json_safe(science_candidate_row),
                "candidate_export_status": export_status,
                "light_curve": lc_summary,
                "csv_path": str(public_csv_path),
                "json_path": str(public_json_path),
                "png_path": png_path_str,
                "plot_warning": plot_warning,
            }
            _write_json(json_payload, json_path)
            export_science_light_curve_csv(lc_rows, csv_path)
            summary = _candidate_summary_row(
                science_candidate_row,
                light_curve_summary=lc_summary,
                csv_path=str(public_csv_path),
                json_path=str(public_json_path),
                png_path=png_path_str,
            )
            summary_rows.append(summary)
            if str(row.get("candidate_status") or "").upper() == "ACCEPTED" and _is_exported_status(export_status):
                product.accepted_candidates += 1
                product.exported_light_curves += 1
            else:
                product.diagnostic_light_curves += 1
                if export_status.endswith("NO_ACCEPTED_POINTS"):
                    product.rejected_no_accepted_points += 1
                elif export_status.endswith("NO_POINTS"):
                    product.rejected_no_points += 1
        except LightCurveSelectionError:
            raise
        except SQLAlchemyError:
            # A database error is not a per-candidate cosmetic failure.  On
            # PostgreSQL it also aborts the current transaction, so swallowing
            # it here makes the later pipeline recorder fail with the misleading
            # InFailedSqlTransaction error.  Let the runner rollback the step
            # and persist the primary database exception instead.
            raise
        except Exception as exc:  # noqa: BLE001 - one non-DB candidate export failure must not break all exports.
            product.skipped_candidates += 1
            product.warnings.append(f"object_id={object_id} export failed: {type(exc).__name__}: {exc}")
            summary = _candidate_summary_row(row, light_curve_summary={"candidate_export_status": "EXPORT_FAILED"}, csv_path=None, json_path=None, png_path=None)
            summary["export_error"] = f"{type(exc).__name__}: {exc}"
            summary_rows.append(summary)

    product.summary_rows = summary_rows
    summary_csv = out_path / "candidates_summary.csv"
    summary_json = out_path / "candidates_summary.json"
    index_html = out_path / "index.html"
    product.candidates_summary_csv = str(public_out_path / summary_csv.name)
    product.candidates_summary_json = str(public_out_path / summary_json.name)
    product.candidates_index_html = str(public_out_path / index_html.name)
    borderline_rows = [row for row in summary_rows if str(row.get("candidate_status") or "").upper() == "BORDERLINE"]
    near_threshold_rows = [row for row in summary_rows if str(row.get("candidate_status") or "").upper() == "DIAGNOSTIC"]
    residual_floor_ab_rows = [row for row in summary_rows if row.get("mag_err_epoch_floor_median") is not None]
    target_rows = [row for row in summary_rows if row.get("is_target")]
    borderline_csv = out_path / "borderline_candidates.csv"
    borderline_json = out_path / "borderline_candidates.json"
    near_threshold_csv = out_path / "near_threshold_candidates.csv"
    near_threshold_json = out_path / "near_threshold_candidates.json"
    residual_floor_ab_csv = out_path / "residual_floor_candidate_ab.csv"
    residual_floor_ab_json = out_path / "residual_floor_candidate_ab.json"
    target_csv = out_path / "target_diagnostics.csv"
    target_json = out_path / "target_diagnostics.json"
    membership_json = out_path / "candidate_membership_history.json"
    product.borderline_candidates_csv = str(public_out_path / borderline_csv.name)
    product.borderline_candidates_json = str(public_out_path / borderline_json.name)
    product.near_threshold_candidates_csv = str(public_out_path / near_threshold_csv.name)
    product.near_threshold_candidates_json = str(public_out_path / near_threshold_json.name)
    product.residual_floor_candidate_ab_csv = str(public_out_path / residual_floor_ab_csv.name)
    product.residual_floor_candidate_ab_json = str(public_out_path / residual_floor_ab_json.name)
    product.target_diagnostics_csv = str(public_out_path / target_csv.name)
    product.target_diagnostics_json = str(public_out_path / target_json.name)
    product.membership_history_json = str(public_out_path / membership_json.name)
    export_candidates_summary_csv(borderline_rows, borderline_csv)
    _write_json({"summary": {"count": len(borderline_rows)}, "candidates": borderline_rows}, borderline_json)
    export_candidates_summary_csv(near_threshold_rows, near_threshold_csv)
    _write_json({"summary": {"count": len(near_threshold_rows)}, "candidates": near_threshold_rows}, near_threshold_json)
    export_candidates_summary_csv(residual_floor_ab_rows, residual_floor_ab_csv)
    _write_json({"summary": {"count": len(residual_floor_ab_rows), "floor_applied": False}, "candidates": residual_floor_ab_rows}, residual_floor_ab_json)
    export_candidates_summary_csv(target_rows, target_csv)
    _write_json({"summary": {"count": len(target_rows)}, "targets": target_rows}, target_json)
    _write_json({"history": membership_history or []}, membership_json)
    export_candidates_summary_csv(summary_rows, summary_csv)
    _write_json({"summary": _summary_counts(product), "candidates": summary_rows}, summary_json)
    export_candidates_index_html(
        summary_rows,
        index_html,
        summary=_summary_counts(product),
        title="Accepted candidate light curves",
    )
    return product


def _replace_directory_tree(staging_path: Path, final_path: Path) -> None:
    """Replace one directory tree while keeping a rollback copy until success."""

    backup_path = final_path.with_name(f".{final_path.name}.backup-{uuid.uuid4().hex}")
    moved_old = False
    try:
        if final_path.exists():
            final_path.rename(backup_path)
            moved_old = True
        staging_path.rename(final_path)
    except Exception:
        if final_path.exists() and not moved_old:
            shutil.rmtree(final_path, ignore_errors=True)
        if moved_old and backup_path.exists() and not final_path.exists():
            backup_path.rename(final_path)
        raise
    else:
        if backup_path.exists():
            shutil.rmtree(backup_path, ignore_errors=True)


def rank_candidate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = [dict(row) for row in rows]
    for row in out:
        row["candidate_rank_score"] = candidate_rank_score(row)
    out.sort(
        key=lambda row: (
            _origin_sort_key(row),
            -float(row.get("candidate_rank_score") or 0.0),
            -float(row.get("robust_peak_to_peak") or 0.0),
            -float(row.get("rms_over_mag_err") or 0.0),
            int(row.get("object_id") or 0),
        )
    )
    for idx, row in enumerate(out, start=1):
        row["rank"] = idx
    return out


def candidate_rank_score(row: dict[str, Any]) -> float:
    origins = _origin_set(row)
    score = 0.0
    if "target" in origins:
        score += 100.0
    if "rms_excess" in origins:
        score += 20.0
    if "intrinsic_variability" in origins:
        score += 15.0
    if "low_amplitude_intrinsic" in origins:
        score += 8.0

    score += min(_num(row.get("rms_excess")), 10.0) * 2.0
    score += min(_num(row.get("rms_over_mag_err")), 12.0) * 1.2
    score += min(_num(row.get("mad_over_mag_err")), 10.0) * 1.0
    score += min(_num(row.get("robust_peak_to_peak")) * 20.0, 10.0)
    score += min(_num(row.get("detection_frame_fraction"), default=_num(row.get("detection_fraction"))) * 5.0, 5.0)

    flags = _flag_set(row.get("object_qc_flags")) | _flag_set(row.get("rejection_reasons"))
    if flags & {"SATURATED_LIKE"}:
        score -= 8.0
    if flags & {"CROWDED", "BLENDED_NEIGHBOR", "DYNAMIC_CROWDED", "DYNAMIC_BLEND_RISK", "POSSIBLE_BLEND", "CROWDED_OR_BLEND"}:
        score -= 6.0
    if flags & {"NO_DETECTION_SUPPORT", "WEAK_DETECTION_SUPPORT", "WEAK_OR_NO_DETECTION_SUPPORT"}:
        score -= 5.0
    if flags & {"EDGE", "APERTURE_UNSTABLE", "APERTURE_MEDIAN_SHIFT"}:
        score -= 4.0
    return round(score, 6)


def export_candidates_summary_csv(rows: Iterable[dict[str, Any]], path: str | Path) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = candidate_summary_columns()
    count = 0
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_safe_value(row.get(key)) for key in columns})
            count += 1
    return count



def export_candidates_index_html(
    rows: Iterable[dict[str, Any]],
    path: str | Path,
    *,
    summary: dict[str, Any] | None = None,
    title: str = "Candidate light curves",
) -> int:
    """Write a standalone quick-look HTML page for exported candidates."""

    rows_list = list(rows)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    parts: list[str] = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{_h(title)}</title>",
        "<style>",
        "body{font-family:Arial,sans-serif;margin:24px;line-height:1.35}",
        "table{border-collapse:collapse;width:100%;font-size:13px}",
        "th,td{border:1px solid #ddd;padding:6px;vertical-align:top}",
        "th{background:#f2f2f2;position:sticky;top:0}",
        "img{max-width:360px;height:auto}",
        ".status{font-weight:bold}",
        ".small{font-size:12px;color:#555}",
        "</style>",
        "</head>",
        "<body>",
        f"<h1>{_h(title)}</h1>",
    ]
    if summary:
        parts.append("<h2>Summary</h2>")
        parts.append("<table>")
        parts.append("<tr><th>metric</th><th>value</th></tr>")
        for key in (
            "triage_accepted_candidates",
            "accepted_candidates",
            "exported_light_curves",
            "candidate_plot_count",
            "candidate_plot_warnings_count",
            "plot_backend",
            "diagnostic_light_curves",
            "rejected_no_accepted_points",
            "rejected_no_points",
            "skipped_candidates",
        ):
            if key in summary:
                parts.append(f"<tr><td>{_h(key)}</td><td>{_h(summary.get(key))}</td></tr>")
        parts.append("</table>")
    parts.append("<h2>Candidates</h2>")
    parts.append("<table>")
    parts.append(
        "<tr>"
        "<th>rank</th><th>status</th><th>origin</th><th>object</th><th>photometry</th>"
        "<th>detection/QC</th><th>files</th><th>quick-look</th>"
        "</tr>"
    )
    base = path.parent
    for row in rows_list:
        png_link = _relative_link(row.get("light_curve_png"), base)
        csv_link = _relative_link(row.get("light_curve_csv"), base)
        json_link = _relative_link(row.get("light_curve_json"), base)
        preview = f'<a href="{_h(png_link)}"><img src="{_h(png_link)}" alt="light curve"></a>' if png_link else ""
        file_links = []
        if csv_link:
            file_links.append(f'<a href="{_h(csv_link)}">CSV</a>')
        if json_link:
            file_links.append(f'<a href="{_h(json_link)}">JSON</a>')
        if png_link:
            file_links.append(f'<a href="{_h(png_link)}">PNG</a>')
        object_bits = [
            f"object_id={_h(row.get('object_id'))}",
            f"source_id={_h(row.get('source_id'))}",
            f"RA={_fmt(row.get('ra_deg'), 7)}",
            f"Dec={_fmt(row.get('dec_deg'), 7)}",
        ]
        phot_bits = [
            f"N={_h(row.get('n_points'))} / accepted={_h(row.get('n_accepted'))}",
            f"median={_fmt(row.get('mag_median'), 5)} mag",
            f"rms={_fmt(row.get('mag_rms'), 5)}",
            f"P90-P10={_fmt(row.get('robust_peak_to_peak'), 5)}",
            f"rms/err={_fmt(row.get('rms_over_mag_err'), 3)}",
            f"mad/err={_fmt(row.get('mad_over_mag_err'), 3)}",
        ]
        qc_bits = [
            f"det.frac={_fmt(row.get('detection_frame_fraction'), 3)}",
            f"sep.idx={_fmt(row.get('separation_index'), 3)}",
            f"flags={_h(row.get('object_qc_flags') or '')}",
            f"reject={_h(row.get('rejection_reasons') or '')}",
        ]
        parts.append(
            "<tr>"
            f"<td>{_h(row.get('rank'))}</td>"
            f"<td><span class=\"status\">{_h(row.get('candidate_export_status'))}</span><br>triage={_h(row.get('candidate_status'))}<br>accepted_fraction={_fmt(row.get('accepted_fraction'), 3)}</td>"
            f"<td>{_h(row.get('candidate_origin'))}<br>score={_fmt(row.get('candidate_rank_score'), 3)}</td>"
            f"<td>{'<br>'.join(object_bits)}</td>"
            f"<td>{'<br>'.join(phot_bits)}</td>"
            f"<td>{'<br>'.join(qc_bits)}</td>"
            f"<td>{' | '.join(file_links)}</td>"
            f"<td>{preview}</td>"
            "</tr>"
        )
    parts.extend(["</table>", "</body>", "</html>"])
    path.write_text("\n".join(parts), encoding="utf-8")
    return len(rows_list)


def _candidate_plot_backend(*, enabled: bool = True) -> tuple[str, str | None]:
    if not enabled:
        return "disabled", None
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot  # noqa: F401
    except Exception as exc:  # pragma: no cover - depends on installed optional runtime packages
        return "unavailable", f"candidate plotting skipped: matplotlib is unavailable ({type(exc).__name__}: {exc})"
    return "matplotlib", None


def _append_plot_warning(product: CandidateLightCurveProduct, warning: str) -> None:
    product.candidate_plot_warnings_count += 1
    product.warnings.append(warning)


def _export_candidate_light_curve_plot(
    rows: list[dict[str, Any]],
    path: str | Path,
    *,
    candidate_row: dict[str, Any],
    light_curve_summary: dict[str, Any],
) -> None:
    """Write a quick-look PNG for one candidate without requiring DB access."""

    points: list[tuple[float, float, float | None, bool]] = []
    for row in rows:
        x = row.get("mjd")
        y = row.get("mag_global") if row.get("mag_global") is not None else row.get("mag_cal")
        err = row.get("mag_global_err_random") if row.get("mag_global_err_random") is not None else row.get("mag_err")
        if not (_is_finite(x) and _is_finite(y)):
            continue
        points.append((float(x), float(y), float(err) if _is_finite(err) else None, bool(row.get("is_rejected"))))
    if not points:
        raise ValueError("no finite light-curve points to plot")

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:  # pragma: no cover - depends on optional dependency
        raise RuntimeError("matplotlib is required for candidate PNG export") from exc

    x0 = math.floor(min(p[0] for p in points))
    accepted = [(x - x0, y, e) for x, y, e, rejected in points if not rejected]
    rejected = [(x - x0, y, e) for x, y, e, rejected in points if rejected]

    fig, ax = plt.subplots(figsize=(8.0, 4.6))
    if accepted:
        xs = [p[0] for p in accepted]
        ys = [p[1] for p in accepted]
        es = [p[2] for p in accepted]
        if all(e is not None and e > 0 for e in es):
            ax.errorbar(xs, ys, yerr=[float(e) for e in es], marker="o", linestyle="-", capsize=2, label="accepted")
        else:
            ax.plot(xs, ys, marker="o", linestyle="-", label="accepted")
    if rejected:
        ax.plot([p[0] for p in rejected], [p[1] for p in rejected], marker="x", linestyle="None", label="rejected")

    source_id = candidate_row.get("source_id") or "unknown source"
    object_id = candidate_row.get("object_id") or "unknown object"
    origin = candidate_row.get("candidate_origin") or "candidate"
    plot_title = f"object {object_id} / {source_id} / {origin}"
    ax.set_xlabel(f"MJD - {x0:.0f}")
    ax.set_ylabel("Calibrated magnitude")
    ax.grid(True, alpha=0.3)
    ax.invert_yaxis()
    if accepted and rejected:
        ax.legend(loc="best", fontsize=8)
    stats = [
        f"N={light_curve_summary.get('rows_total')}",
        f"accepted={light_curve_summary.get('rows_accepted')}",
        f"median={_fmt(light_curve_summary.get('mag_median'), 5)}",
        f"rms={_fmt(light_curve_summary.get('mag_rms'), 5)}",
        f"P90-P10={_fmt(light_curve_summary.get('robust_peak_to_peak'), 5)}",
        f"rms/err={_fmt(candidate_row.get('rms_over_mag_err'), 3)}",
        f"mad/err={_fmt(candidate_row.get('mad_over_mag_err'), 3)}",
    ]
    ax.text(
        0.02,
        0.98,
        "\n".join(stats),
        transform=ax.transAxes,
        va="top",
        ha="left",
        fontsize=8,
        bbox={"boxstyle": "round", "facecolor": "white", "alpha": 0.75, "edgecolor": "0.7"},
    )
    set_lightcurve_plot_title(
        fig,
        ax,
        title=plot_title,
        subtitle=format_lightcurve_identity_subtitle(
            ra_deg=candidate_row.get("ra_deg"),
            dec_deg=candidate_row.get("dec_deg"),
            catalog_name=candidate_row.get("catalog_name"),
            source_id=candidate_row.get("source_id"),
            coordinate_label="Catalog coordinates",
        ),
    )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150)
    plt.close(fig)


SCIENCE_LIGHT_CURVE_METRIC_MAP: tuple[tuple[str, str], ...] = (
    ("mag_median", "mag_median"),
    ("mag_rms", "mag_rms"),
    ("mag_mad", "mag_mad"),
    ("mag_p10", "mag_p10"),
    ("mag_p90", "mag_p90"),
    ("robust_peak_to_peak", "robust_peak_to_peak"),
    ("mag_err_median", "mag_err_median"),
    ("mag_err_systematic_median", "mag_err_systematic_median"),
    ("mag_err_detector_median", "mag_err_detector_median"),
    ("mag_err_epoch_floor_median", "mag_err_epoch_floor_median"),
    (
        "mag_err_effective_detector_only_median",
        "mag_err_effective_detector_only_median",
    ),
    ("mag_err_effective_total_median", "mag_err_effective_total_median"),
    ("mag_err_effective_median", "mag_err_effective_median"),
    ("residual_noise_floor_applied", "residual_noise_floor_applied"),
)


def _candidate_row_with_light_curve_metrics(
    row: dict[str, Any],
    light_curve_summary: dict[str, Any],
) -> dict[str, Any]:
    """Return candidate metadata with photometry from the exported science curve.

    Candidate origin, rank, and selection diagnostics remain tied to the internal
    ensemble calibration.  Magnitudes, scatter metrics, errors, and point counts
    must instead describe the actual curve written to CSV/JSON.  Keeping those
    roles separate prevents mixed-scale candidate products after an absolute
    reference transformation.
    """

    out = dict(row)
    for candidate_key, summary_key in SCIENCE_LIGHT_CURVE_METRIC_MAP:
        out[candidate_key] = light_curve_summary.get(summary_key)
    out["n_points"] = light_curve_summary.get("rows_total")
    out["n_accepted"] = light_curve_summary.get("rows_accepted")
    out["n_rejected"] = light_curve_summary.get("rows_rejected")
    out["accepted_fraction"] = _accepted_fraction(light_curve_summary)
    out["candidate_export_status"] = (
        light_curve_summary.get("candidate_export_status")
        or _candidate_export_status(light_curve_summary)
    )
    out.update(_residual_floor_candidate_ab(out))
    return out


def _candidate_summary_row(
    row: dict[str, Any],
    *,
    light_curve_summary: dict[str, Any],
    csv_path: str | None,
    json_path: str | None,
    png_path: str | None,
) -> dict[str, Any]:
    out = {key: row.get(key) for key in candidate_summary_columns()}
    for candidate_key, summary_key in SCIENCE_LIGHT_CURVE_METRIC_MAP:
        if summary_key in light_curve_summary:
            out[candidate_key] = light_curve_summary.get(summary_key)
    out.update(
        {
            "rank": row.get("rank"),
            "candidate_rank_score": row.get("candidate_rank_score"),
            "candidate_export_status": light_curve_summary.get("candidate_export_status") or _candidate_export_status(light_curve_summary),
            "n_points": light_curve_summary.get("rows_total"),
            "n_accepted": light_curve_summary.get("rows_accepted"),
            "n_rejected": light_curve_summary.get("rows_rejected"),
            "accepted_fraction": _accepted_fraction(light_curve_summary),
            "light_curve_csv": csv_path,
            "light_curve_json": json_path,
            "light_curve_png": png_path,
        }
    )
    return out


def _summarize_light_curve(rows: list[dict[str, Any]]) -> dict[str, Any]:
    mags = [float(row["mag_global"]) for row in rows if _is_finite(row.get("mag_global"))]
    errs = [float(row["mag_global_err_random"]) for row in rows if _is_finite(row.get("mag_global_err_random"))]
    systematic_errs = [float(row["mag_global_err_systematic"]) for row in rows if _is_finite(row.get("mag_global_err_systematic"))]
    detector_errs = [float(row["mag_err_detector"]) for row in rows if _is_finite(row.get("mag_err_detector"))]
    epoch_floor_errs = [float(row["mag_err_epoch_floor"]) for row in rows if _is_finite(row.get("mag_err_epoch_floor"))]
    effective_detector_only_errs = [
        float(row["mag_err_effective_detector_only"])
        for row in rows
        if _is_finite(row.get("mag_err_effective_detector_only"))
    ]
    effective_total_errs = [
        float(row["mag_err_effective_total"])
        for row in rows
        if _is_finite(row.get("mag_err_effective_total"))
    ]
    effective_errs = [float(row["mag_err_effective"]) for row in rows if _is_finite(row.get("mag_err_effective"))]
    mjds = [float(row["mjd"]) for row in rows if _is_finite(row.get("mjd"))]
    effective_total_median = _median(effective_total_errs)
    if effective_total_median is None:
        effective_total_median = _median(effective_errs)
    return {
        "rows_total": len(rows),
        "rows_accepted": sum(1 for row in rows if not row.get("is_rejected")),
        "rows_rejected": sum(1 for row in rows if row.get("is_rejected")),
        "mag_median": _median(mags),
        "mag_mean": _mean(mags),
        "mag_rms": _sample_rms(mags),
        "mag_mad": _mad(mags),
        "mag_p10": _percentile(sorted(mags), 10.0) if mags else None,
        "mag_p90": _percentile(sorted(mags), 90.0) if mags else None,
        "mag_err_median": _median(errs),
        "mag_err_systematic_median": _median(systematic_errs),
        "mag_err_detector_median": _median(detector_errs),
        "mag_err_epoch_floor_median": _median(epoch_floor_errs),
        "mag_err_effective_detector_only_median": _median(effective_detector_only_errs),
        "mag_err_effective_total_median": effective_total_median,
        "mag_err_effective_median": effective_total_median,
        "residual_noise_floor_applied": any(bool(row.get("residual_noise_floor_applied")) for row in rows),
        "mag_min": min(mags) if mags else None,
        "mag_max": max(mags) if mags else None,
        "peak_to_peak": (max(mags) - min(mags)) if mags else None,
        "robust_peak_to_peak": _robust_peak_to_peak(mags),
        "mjd_min": min(mjds) if mjds else None,
        "mjd_max": max(mjds) if mjds else None,
    }


def _residual_floor_candidate_ab(row: dict[str, Any]) -> dict[str, Any]:
    effective_err = _finite_or_none(row.get("mag_err_effective_total_median"))
    if effective_err is None:
        effective_err = _finite_or_none(row.get("mag_err_effective_median"))
    mag_rms = _finite_or_none(row.get("mag_rms"))
    mag_mad = _finite_or_none(row.get("mag_mad"))
    if effective_err is None or effective_err <= 0:
        return {
            "residual_floor_ab_rms_over_mag_err": None,
            "residual_floor_ab_mad_over_mag_err": None,
            "residual_floor_ab_low_amplitude_passed": None,
            "residual_floor_ab_failed_criteria": "",
            "residual_floor_ab_min_threshold_ratio": None,
            "residual_floor_ab_status": "NOT_AVAILABLE",
        }
    effective_err = max(effective_err, 0.01)
    rms_over = mag_rms / effective_err if mag_rms is not None else None
    mad_over = mag_mad / effective_err if mag_mad is not None else None
    values = {
        "n_measurements": _finite_or_none(row.get("n_measurements")),
        "intrinsic_rms": mag_rms,
        "rms_over_mag_err": rms_over,
        "mad_over_mag_err": mad_over,
        "robust_peak_to_peak": _finite_or_none(row.get("robust_peak_to_peak")),
    }
    failed: list[str] = []
    ratios: list[float] = []
    for name, value in values.items():
        threshold = _finite_or_none(row.get(f"low_amplitude_{name}_threshold"))
        if threshold is None or threshold <= 0:
            continue
        ratio = value / threshold if value is not None else None
        if ratio is not None and math.isfinite(ratio):
            ratios.append(ratio)
        if value is None or value < threshold:
            failed.append(name)
    return {
        "residual_floor_ab_rms_over_mag_err": rms_over,
        "residual_floor_ab_mad_over_mag_err": mad_over,
        "residual_floor_ab_low_amplitude_passed": not failed,
        "residual_floor_ab_failed_criteria": ",".join(failed),
        "residual_floor_ab_min_threshold_ratio": min(ratios) if ratios else None,
        "residual_floor_ab_status": "PASS" if not failed else "FAIL",
    }


def _summary_counts(product: CandidateLightCurveProduct) -> dict[str, Any]:
    return {
        "triage_accepted_candidates": product.triage_accepted_candidates,
        "accepted_candidates": product.accepted_candidates,
        "exported_light_curves": product.exported_light_curves,
        "candidate_plot_count": product.candidate_plot_count,
        "candidate_plot_warnings_count": product.candidate_plot_warnings_count,
        "plot_backend": product.plot_backend,
        "diagnostic_light_curves": product.diagnostic_light_curves,
        "rejected_no_accepted_points": product.rejected_no_accepted_points,
        "rejected_no_points": product.rejected_no_points,
        "skipped_candidates": product.skipped_candidates,
        "candidates_dir": product.candidates_dir,
        "candidates_summary_csv": product.candidates_summary_csv,
        "candidates_summary_json": product.candidates_summary_json,
        "candidates_index_html": product.candidates_index_html,
        "borderline_candidates": product.borderline_candidates,
        "near_threshold_candidates": product.near_threshold_candidates,
        "target_diagnostic_candidates": product.target_diagnostic_candidates,
        "borderline_candidates_csv": product.borderline_candidates_csv,
        "borderline_candidates_json": product.borderline_candidates_json,
        "near_threshold_candidates_csv": product.near_threshold_candidates_csv,
        "near_threshold_candidates_json": product.near_threshold_candidates_json,
        "residual_floor_candidate_ab_csv": product.residual_floor_candidate_ab_csv,
        "residual_floor_candidate_ab_json": product.residual_floor_candidate_ab_json,
        "target_diagnostics_csv": product.target_diagnostics_csv,
        "target_diagnostics_json": product.target_diagnostics_json,
        "membership_history_json": product.membership_history_json,
        "interseries_generation_ids": list(product.interseries_generation_ids),
        "interseries_generation_scopes": list(product.interseries_generation_scopes),
        "warnings": list(product.warnings),
    }


def _candidate_reject_flags_for_row(
    row: dict[str, Any],
    reject_flags: set[str] | None,
) -> tuple[set[str], str | None]:
    """Return candidate-export reject flags with one narrow variability exception.

    ``HIGH_RMS`` is a useful science-light-curve guard for ordinary objects, but
    it is logically incompatible with a candidate that has already passed the
    ordinary variability evaluator on the GOOD-frame subset.  For such an object
    the flag remains present in ``object_qc_flags`` for provenance, but it does not
    reject every point of the candidate light curve.  All other science reject
    flags remain unchanged.
    """

    effective = set(DEFAULT_REJECT_FLAGS if reject_flags is None else reject_flags)
    confirmation = str(row.get("good_frame_confirmation_status") or "").upper()
    status = str(row.get("candidate_status") or "").upper()
    if status == "ACCEPTED" and confirmation == "GOOD_FRAME_CONFIRMED":
        if "HIGH_RMS" in effective:
            effective.discard("HIGH_RMS")
            return effective, "HIGH_RMS_DIAGNOSTIC_ONLY_FOR_GOOD_FRAME_CONFIRMED"
    return effective, None


def _candidate_export_status_for_row(row: dict[str, Any], light_curve_summary: dict[str, Any]) -> str:
    base = _candidate_export_status(light_curve_summary)
    status = str(row.get("candidate_status") or "").upper()
    if status == "BORDERLINE":
        return "BORDERLINE_EXPORTED" if base == "EXPORTED" else f"BORDERLINE_{base}"
    if status == "DIAGNOSTIC":
        return "DIAGNOSTIC_EXPORTED" if base == "EXPORTED" else f"DIAGNOSTIC_{base}"
    if row.get("is_target") and status != "ACCEPTED":
        return "TARGET_DIAGNOSTIC_EXPORTED" if base == "EXPORTED" else f"TARGET_DIAGNOSTIC_{base}"
    return base


def _is_exported_status(status: str | None) -> bool:
    return bool(status) and str(status).endswith("EXPORTED")


def _candidate_export_status(light_curve_summary: dict[str, Any]) -> str:
    rows_total = _to_int(light_curve_summary.get("rows_total")) or 0
    rows_accepted = _to_int(light_curve_summary.get("rows_accepted")) or 0
    if rows_total <= 0:
        return "REJECTED_NO_POINTS"
    if rows_accepted <= 0:
        return "REJECTED_NO_ACCEPTED_POINTS"
    return "EXPORTED"


def _accepted_fraction(light_curve_summary: dict[str, Any]) -> float | None:
    rows_total = _to_int(light_curve_summary.get("rows_total")) or 0
    rows_accepted = _to_int(light_curve_summary.get("rows_accepted")) or 0
    if rows_total <= 0:
        return None
    return rows_accepted / rows_total


def _candidate_file_stem(*, object_id: int, source_id: Any | None) -> str:
    source = _safe_token(source_id) if source_id not in (None, "") else "nosource"
    return f"object_{int(object_id)}_src_{source}"


def _origin_set(row: dict[str, Any]) -> set[str]:
    return {part.strip() for part in str(row.get("candidate_origin") or "").split(",") if part.strip()}


def _origin_sort_key(row: dict[str, Any]) -> int:
    origins = _origin_set(row)
    if "target" in origins:
        return 0
    if "rms_excess" in origins and ("intrinsic_variability" in origins or "low_amplitude_intrinsic" in origins):
        return 1
    if "rms_excess" in origins:
        return 2
    if "intrinsic_variability" in origins:
        return 3
    if "low_amplitude_intrinsic" in origins:
        return 4
    return 5


def _flag_set(value: Any) -> set[str]:
    if not value:
        return set()
    if isinstance(value, dict):
        return {str(k).upper() for k, v in value.items() if v}
    return {part.strip().upper() for part in str(value).split(",") if part.strip()}


def _num(value: Any, *, default: float = 0.0) -> float:
    try:
        f = float(value)
    except Exception:
        return float(default)
    if not math.isfinite(f):
        return float(default)
    return f


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def _safe_token(value: Any) -> str:
    text = str(value)
    text = re.sub(r"[^0-9A-Za-zА-Яа-я_+-]+", "_", text.strip())
    text = re.sub(r"_+", "_", text).strip("_")
    return text or "unknown"


def _finite_or_none(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _is_finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except Exception:
        return False


def _mean(values: Iterable[float]) -> float | None:
    vals = [float(v) for v in values]
    return sum(vals) / len(vals) if vals else None


def _median(values: Iterable[float]) -> float | None:
    vals = sorted(float(v) for v in values)
    n = len(vals)
    if n == 0:
        return None
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def _mad(values: Iterable[float]) -> float | None:
    vals = [float(v) for v in values]
    med = _median(vals)
    if med is None:
        return None
    return _median([abs(v - med) for v in vals])


def _sample_rms(values: Iterable[float]) -> float | None:
    vals = [float(v) for v in values]
    if len(vals) < 2:
        return None
    mean = sum(vals) / len(vals)
    return math.sqrt(sum((v - mean) ** 2 for v in vals) / (len(vals) - 1))


def _robust_peak_to_peak(values: Iterable[float]) -> float | None:
    vals = sorted(float(v) for v in values)
    if not vals:
        return None
    return _percentile(vals, 90.0) - _percentile(vals, 10.0)


def _percentile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return float("nan")
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = (len(sorted_values) - 1) * (float(q) / 100.0)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_values[lo]
    weight = pos - lo
    return sorted_values[lo] * (1.0 - weight) + sorted_values[hi] * weight



def _relative_link(value: Any, base: Path) -> str | None:
    if value in (None, ""):
        return None
    p = Path(str(value))
    try:
        return p.relative_to(base).as_posix()
    except Exception:
        if p.is_absolute():
            try:
                return p.relative_to(Path.cwd()).as_posix()
            except Exception:
                return p.as_posix()
        return p.as_posix()


def _h(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _fmt(value: Any, ndigits: int = 4) -> str:
    if not _is_finite(value):
        return ""
    return f"{float(value):.{int(ndigits)}f}"


def _write_json(payload: dict[str, Any], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_json_safe(payload), ensure_ascii=False, indent=2), encoding="utf-8")


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _csv_safe_value(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(_json_safe(value), ensure_ascii=False)
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    return value
