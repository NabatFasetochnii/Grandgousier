from __future__ import annotations

import csv
import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from grandgousier.db.models import Frame
from grandgousier.photometry.calibration_view import load_calibration_photometry_view


CANDIDATE_AXIS_CORRELATION_ALGORITHM = "candidate_axis_bin_spearman_pearson_v2"
DEFAULT_AXIS_BINS = 16
DEFAULT_MIN_COMMON_GOOD_EPOCHS = 8
DEFAULT_MIN_ABS_SPEARMAN_RHO = 0.8
DEFAULT_MIN_ABS_PEARSON_R = 0.8

CANDIDATE_AXIS_CORRELATION_COLUMNS: tuple[str, ...] = (
    "candidate_axis_correlation_status",
    "candidate_axis_correlation_flags",
    "candidate_axis_correlation_mode",
    "candidate_detector_x_median",
    "candidate_detector_y_median",
    "candidate_x_bin",
    "candidate_y_bin",
    "candidate_x_bin_shifted",
    "candidate_y_bin_shifted",
    "candidate_x_bin_candidate_count",
    "candidate_y_bin_candidate_count",
    "candidate_x_axis_correlation_mode",
    "candidate_y_axis_correlation_mode",
    "candidate_max_x_bin_spearman_rho",
    "candidate_max_y_bin_spearman_rho",
    "candidate_max_x_bin_spearman_object_id",
    "candidate_max_y_bin_spearman_object_id",
    "candidate_max_x_bin_spearman_source_id",
    "candidate_max_y_bin_spearman_source_id",
    "candidate_max_x_bin_spearman_common_good_epochs",
    "candidate_max_y_bin_spearman_common_good_epochs",
    "candidate_max_x_bin_pearson_r",
    "candidate_max_y_bin_pearson_r",
    "candidate_max_x_bin_pearson_object_id",
    "candidate_max_y_bin_pearson_object_id",
    "candidate_max_x_bin_pearson_source_id",
    "candidate_max_y_bin_pearson_source_id",
    "candidate_max_x_bin_pearson_common_good_epochs",
    "candidate_max_y_bin_pearson_common_good_epochs",
    # Backward-compatible generic strongest correlated partner fields.
    "candidate_max_x_bin_correlated_object_id",
    "candidate_max_y_bin_correlated_object_id",
    "candidate_max_x_bin_correlated_source_id",
    "candidate_max_y_bin_correlated_source_id",
    "candidate_max_x_bin_common_good_epochs",
    "candidate_max_y_bin_common_good_epochs",
    "candidate_max_x_bin_correlation_metric",
    "candidate_max_y_bin_correlation_metric",
    "candidate_max_x_bin_correlation_strength",
    "candidate_max_y_bin_correlation_strength",
    "candidate_n_x_correlated_candidates",
    "candidate_n_y_correlated_candidates",
    "candidate_n_x_spearman_correlated_candidates",
    "candidate_n_y_spearman_correlated_candidates",
    "candidate_n_x_pearson_correlated_candidates",
    "candidate_n_y_pearson_correlated_candidates",
)


@dataclass(slots=True)
class CandidateAxisCorrelationResult:
    candidate_rows: list[dict[str, Any]]
    pair_rows: list[dict[str, Any]]
    summary: dict[str, Any]


def annotate_candidate_axis_correlations(
    session: Session,
    *,
    candidate_rows: list[dict[str, Any]],
    calibration_name: str,
    good_frame_ids: Iterable[int],
    axis_bins: int = DEFAULT_AXIS_BINS,
    min_common_good_epochs: int = DEFAULT_MIN_COMMON_GOOD_EPOCHS,
    min_abs_spearman_rho: float = DEFAULT_MIN_ABS_SPEARMAN_RHO,
    min_abs_pearson_r: float = DEFAULT_MIN_ABS_PEARSON_R,
) -> CandidateAxisCorrelationResult:
    """Annotate confirmed candidates with same-axis-bin light-curve correlations.

    This is a diagnostic-only candidate layer. It never changes candidate status,
    photometry, errors, frame QC, or candidate-reference exclusion. Candidate
    changes are median-centred and correlated only on common photometric-frame-QC
    GOOD epochs. X and Y bins are evaluated independently.

    Spearman correlation captures persistent rank-coherent variation while Pearson
    correlation intentionally retains sensitivity to synchronous high-amplitude
    excursions. A pair is flagged when either absolute coefficient reaches its
    configured threshold; both signed coefficients remain in provenance.
    """

    _validate_policy(
        axis_bins,
        min_common_good_epochs,
        min_abs_spearman_rho,
        min_abs_pearson_r,
    )
    rows = [dict(row) for row in candidate_rows]
    good_ids = sorted({int(value) for value in good_frame_ids})
    eligible_indices: list[int] = []
    object_ids: list[int] = []
    for index, row in enumerate(rows):
        _initialize_candidate_fields(row)
        if not _eligible_candidate(row):
            continue
        object_id = _int_or_none(row.get("object_id"))
        if object_id is None:
            row["candidate_axis_correlation_status"] = "MISSING_OBJECT_ID"
            continue
        eligible_indices.append(index)
        object_ids.append(object_id)

    if len(eligible_indices) < 2 or not good_ids:
        status = "INSUFFICIENT_CANDIDATES" if len(eligible_indices) < 2 else "NO_GOOD_FRAMES"
        for index in eligible_indices:
            rows[index]["candidate_axis_correlation_status"] = status
        return CandidateAxisCorrelationResult(
            candidate_rows=rows,
            pair_rows=[],
            summary=_summary(
                rows,
                pair_rows=[],
                axis_bins=axis_bins,
                min_common_good_epochs=min_common_good_epochs,
                min_abs_spearman_rho=min_abs_spearman_rho,
                min_abs_pearson_r=min_abs_pearson_r,
                detector_width=None,
                detector_height=None,
            ),
        )

    view = load_calibration_photometry_view(
        session,
        calibration_name=calibration_name,
        frame_ids=good_ids,
        object_ids=sorted(set(object_ids)),
    )
    by_object: dict[int, dict[int, tuple[float, float | None, float | None]]] = {}
    x_values: list[float] = []
    y_values: list[float] = []
    for measurement in view.rows:
        mag = _finite_or_none(measurement.mag_cal)
        if mag is None:
            continue
        object_id = int(measurement.object_id)
        frame_id = int(measurement.frame_id)
        x = _finite_or_none(measurement.x)
        y = _finite_or_none(measurement.y)
        by_object.setdefault(object_id, {})[frame_id] = (mag, x, y)
        if x is not None:
            x_values.append(x)
        if y is not None:
            y_values.append(y)

    detector_width, detector_height = _detector_extent(
        session,
        good_ids,
        x_values=x_values,
        y_values=y_values,
    )

    row_index_by_object: dict[int, int] = {}
    source_by_object: dict[int, Any] = {}
    light_curves: dict[int, dict[int, float]] = {}
    bin_members_x: dict[int, list[int]] = {}
    bin_members_y: dict[int, list[int]] = {}
    bin_members_x_shifted: dict[int, list[int]] = {}
    bin_members_y_shifted: dict[int, list[int]] = {}

    for index in eligible_indices:
        row = rows[index]
        object_id = int(row["object_id"])
        measurements = by_object.get(object_id, {})
        finite_mags = [value[0] for value in measurements.values() if _finite_or_none(value[0]) is not None]
        xs = [value[1] for value in measurements.values() if value[1] is not None]
        ys = [value[2] for value in measurements.values() if value[2] is not None]
        if not finite_mags or not xs or not ys:
            row["candidate_axis_correlation_status"] = "INSUFFICIENT_PHOTOMETRY_OR_POSITION"
            continue
        median_mag = float(statistics.median(finite_mags))
        x_median = float(statistics.median(xs))
        y_median = float(statistics.median(ys))
        x_bin = _axis_bin(x_median, detector_width, axis_bins)
        y_bin = _axis_bin(y_median, detector_height, axis_bins)
        x_bin_shifted = _axis_bin(x_median, detector_width, axis_bins, half_bin_shift=True)
        y_bin_shifted = _axis_bin(y_median, detector_height, axis_bins, half_bin_shift=True)
        row["candidate_detector_x_median"] = x_median
        row["candidate_detector_y_median"] = y_median
        row["candidate_x_bin"] = x_bin
        row["candidate_y_bin"] = y_bin
        row["candidate_x_bin_shifted"] = x_bin_shifted
        row["candidate_y_bin_shifted"] = y_bin_shifted
        row["candidate_axis_correlation_status"] = "EVALUATED"
        row_index_by_object[object_id] = index
        source_by_object[object_id] = row.get("source_id")
        light_curves[object_id] = {
            frame_id: float(value[0]) - median_mag
            for frame_id, value in measurements.items()
            if _finite_or_none(value[0]) is not None
        }
        bin_members_x.setdefault(x_bin, []).append(object_id)
        bin_members_y.setdefault(y_bin, []).append(object_id)
        bin_members_x_shifted.setdefault(x_bin_shifted, []).append(object_id)
        bin_members_y_shifted.setdefault(y_bin_shifted, []).append(object_id)

    for object_id, index in row_index_by_object.items():
        row = rows[index]
        x_neighbors = set(bin_members_x.get(int(row["candidate_x_bin"]), []))
        x_neighbors.update(bin_members_x_shifted.get(int(row["candidate_x_bin_shifted"]), []))
        y_neighbors = set(bin_members_y.get(int(row["candidate_y_bin"]), []))
        y_neighbors.update(bin_members_y_shifted.get(int(row["candidate_y_bin_shifted"]), []))
        row["candidate_x_bin_candidate_count"] = len(x_neighbors)
        row["candidate_y_bin_candidate_count"] = len(y_neighbors)

    pair_rows: list[dict[str, Any]] = []
    pair_context: dict[tuple[str, int, int], list[tuple[str, int]]] = {}
    for axis, scheme, groups in (
        ("X", "PRIMARY", bin_members_x),
        ("X", "HALF_SHIFTED", bin_members_x_shifted),
        ("Y", "PRIMARY", bin_members_y),
        ("Y", "HALF_SHIFTED", bin_members_y_shifted),
    ):
        for bin_index, members in sorted(groups.items()):
            ordered = sorted(set(members))
            for left_pos, object_a in enumerate(ordered[:-1]):
                for object_b in ordered[left_pos + 1 :]:
                    pair_context.setdefault((axis, object_a, object_b), []).append((scheme, int(bin_index)))

    for (axis, object_a, object_b), contexts in sorted(pair_context.items()):
        common = sorted(set(light_curves[object_a]) & set(light_curves[object_b]))
        spearman_rho = None
        pearson_r = None
        if len(common) >= int(min_common_good_epochs):
            values_a = [light_curves[object_a][frame_id] for frame_id in common]
            values_b = [light_curves[object_b][frame_id] for frame_id in common]
            spearman_rho = _spearman(values_a, values_b)
            pearson_r = _pearson(values_a, values_b)
        spearman_correlated = _passes_abs_threshold(spearman_rho, min_abs_spearman_rho)
        pearson_correlated = _passes_abs_threshold(pearson_r, min_abs_pearson_r)
        correlation_mode = _correlation_mode(spearman_correlated, pearson_correlated)
        correlated = correlation_mode != "NONE"
        pair_row = {
            "axis": axis,
            "bin_index": int(contexts[0][1]),
            "bin_schemes": ",".join(scheme for scheme, _ in contexts),
            "bin_contexts": ",".join(
                f"{scheme}:{bin_index}" for scheme, bin_index in contexts
            ),
            "object_id_a": int(object_a),
            "object_id_b": int(object_b),
            "source_id_a": source_by_object.get(object_a),
            "source_id_b": source_by_object.get(object_b),
            "n_common_good_epochs": len(common),
            "spearman_rho": spearman_rho,
            "abs_spearman_rho": abs(float(spearman_rho)) if spearman_rho is not None else None,
            "pearson_r": pearson_r,
            "abs_pearson_r": abs(float(pearson_r)) if pearson_r is not None else None,
            "spearman_correlated": spearman_correlated,
            "pearson_correlated": pearson_correlated,
            "correlation_mode": correlation_mode,
            "correlated": correlated,
        }
        pair_rows.append(pair_row)
        _update_candidate_pair(
            rows[row_index_by_object[object_a]],
            axis,
            object_b,
            source_by_object.get(object_b),
            len(common),
            spearman_rho=spearman_rho,
            pearson_r=pearson_r,
            spearman_correlated=spearman_correlated,
            pearson_correlated=pearson_correlated,
            correlation_mode=correlation_mode,
        )
        _update_candidate_pair(
            rows[row_index_by_object[object_b]],
            axis,
            object_a,
            source_by_object.get(object_a),
            len(common),
            spearman_rho=spearman_rho,
            pearson_r=pearson_r,
            spearman_correlated=spearman_correlated,
            pearson_correlated=pearson_correlated,
            correlation_mode=correlation_mode,
        )

    for object_id, index in row_index_by_object.items():
        row = rows[index]
        x_mode = _mode_from_candidate_counts(row, "x")
        y_mode = _mode_from_candidate_counts(row, "y")
        combined_mode = _correlation_mode(
            x_mode in {"SPEARMAN", "BOTH"} or y_mode in {"SPEARMAN", "BOTH"},
            x_mode in {"PEARSON", "BOTH"} or y_mode in {"PEARSON", "BOTH"},
        )
        row["candidate_x_axis_correlation_mode"] = x_mode
        row["candidate_y_axis_correlation_mode"] = y_mode
        row["candidate_axis_correlation_mode"] = combined_mode

        flags: list[str] = []
        if int(row.get("candidate_n_x_correlated_candidates") or 0) > 0:
            flags.append("CANDIDATE_CORRELATED_X")
        if int(row.get("candidate_n_y_correlated_candidates") or 0) > 0:
            flags.append("CANDIDATE_CORRELATED_Y")
        row["candidate_axis_correlation_flags"] = ",".join(flags)
        if len(flags) == 2:
            row["candidate_axis_correlation_status"] = "CORRELATED_XY"
        elif flags:
            row["candidate_axis_correlation_status"] = "CORRELATED_X" if flags[0].endswith("_X") else "CORRELATED_Y"
        else:
            row["candidate_axis_correlation_status"] = "NO_STRONG_AXIS_BIN_CORRELATION"

    return CandidateAxisCorrelationResult(
        candidate_rows=rows,
        pair_rows=pair_rows,
        summary=_summary(
            rows,
            pair_rows=pair_rows,
            axis_bins=axis_bins,
            min_common_good_epochs=min_common_good_epochs,
            min_abs_spearman_rho=min_abs_spearman_rho,
            min_abs_pearson_r=min_abs_pearson_r,
            detector_width=detector_width,
            detector_height=detector_height,
        ),
    )


def export_candidate_axis_correlation(
    result: CandidateAxisCorrelationResult,
    out_dir: str | Path,
) -> dict[str, str]:
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    pairs_csv = out_path / "candidate_axis_pairs.csv"
    summary_json = out_path / "summary.json"
    pair_columns = [
        "axis",
        "bin_index",
        "bin_schemes",
        "bin_contexts",
        "object_id_a",
        "object_id_b",
        "source_id_a",
        "source_id_b",
        "n_common_good_epochs",
        "spearman_rho",
        "abs_spearman_rho",
        "pearson_r",
        "abs_pearson_r",
        "spearman_correlated",
        "pearson_correlated",
        "correlation_mode",
        "correlated",
    ]
    with pairs_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=pair_columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(result.pair_rows)
    summary_json.write_text(
        json.dumps(
            {"summary": result.summary, "candidates": result.candidate_rows},
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    return {"pairs_csv": str(pairs_csv), "summary_json": str(summary_json)}


def _eligible_candidate(row: dict[str, Any]) -> bool:
    if str(row.get("candidate_status") or "").upper() != "ACCEPTED":
        return False
    confirmation = str(row.get("good_frame_confirmation_status") or "").upper()
    return confirmation in {"GOOD_FRAME_CONFIRMED", "TARGET_PRESERVED"}


def _initialize_candidate_fields(row: dict[str, Any]) -> None:
    row.setdefault("candidate_axis_correlation_status", "NOT_EVALUATED")
    row.setdefault("candidate_axis_correlation_flags", "")
    for field in CANDIDATE_AXIS_CORRELATION_COLUMNS[2:]:
        row.setdefault(field, None)
    row["candidate_axis_correlation_mode"] = "NONE"
    row["candidate_x_axis_correlation_mode"] = "NONE"
    row["candidate_y_axis_correlation_mode"] = "NONE"
    for axis in ("x", "y"):
        row[f"candidate_n_{axis}_correlated_candidates"] = 0
        row[f"candidate_n_{axis}_spearman_correlated_candidates"] = 0
        row[f"candidate_n_{axis}_pearson_correlated_candidates"] = 0


def _update_candidate_pair(
    row: dict[str, Any],
    axis: str,
    other_object_id: int,
    other_source_id: Any,
    n_common: int,
    *,
    spearman_rho: float | None,
    pearson_r: float | None,
    spearman_correlated: bool,
    pearson_correlated: bool,
    correlation_mode: str,
) -> None:
    axis_lower = axis.lower()
    _update_metric_best(
        row,
        axis_lower,
        metric="spearman",
        value=spearman_rho,
        other_object_id=other_object_id,
        other_source_id=other_source_id,
        n_common=n_common,
    )
    _update_metric_best(
        row,
        axis_lower,
        metric="pearson",
        value=pearson_r,
        other_object_id=other_object_id,
        other_source_id=other_source_id,
        n_common=n_common,
    )

    if correlation_mode == "NONE":
        return

    row[f"candidate_n_{axis_lower}_correlated_candidates"] = (
        int(row.get(f"candidate_n_{axis_lower}_correlated_candidates") or 0) + 1
    )
    if spearman_correlated:
        row[f"candidate_n_{axis_lower}_spearman_correlated_candidates"] = (
            int(row.get(f"candidate_n_{axis_lower}_spearman_correlated_candidates") or 0) + 1
        )
    if pearson_correlated:
        row[f"candidate_n_{axis_lower}_pearson_correlated_candidates"] = (
            int(row.get(f"candidate_n_{axis_lower}_pearson_correlated_candidates") or 0) + 1
        )

    strength_candidates = [
        abs(float(value))
        for value, passed in (
            (spearman_rho, spearman_correlated),
            (pearson_r, pearson_correlated),
        )
        if passed and value is not None and math.isfinite(float(value))
    ]
    strength = max(strength_candidates) if strength_candidates else None
    strength_key = f"candidate_max_{axis_lower}_bin_correlation_strength"
    current_strength = _finite_or_none(row.get(strength_key))
    if strength is not None and (current_strength is None or strength > current_strength):
        row[strength_key] = float(strength)
        row[f"candidate_max_{axis_lower}_bin_correlation_metric"] = correlation_mode
        row[f"candidate_max_{axis_lower}_bin_correlated_object_id"] = int(other_object_id)
        row[f"candidate_max_{axis_lower}_bin_correlated_source_id"] = other_source_id
        row[f"candidate_max_{axis_lower}_bin_common_good_epochs"] = int(n_common)


def _update_metric_best(
    row: dict[str, Any],
    axis_lower: str,
    *,
    metric: str,
    value: float | None,
    other_object_id: int,
    other_source_id: Any,
    n_common: int,
) -> None:
    finite_value = _finite_or_none(value)
    if finite_value is None:
        return
    suffix = "spearman_rho" if metric == "spearman" else "pearson_r"
    value_key = f"candidate_max_{axis_lower}_bin_{suffix}"
    current = _finite_or_none(row.get(value_key))
    if current is not None and abs(float(finite_value)) <= abs(float(current)):
        return
    row[value_key] = float(finite_value)
    row[f"candidate_max_{axis_lower}_bin_{metric}_object_id"] = int(other_object_id)
    row[f"candidate_max_{axis_lower}_bin_{metric}_source_id"] = other_source_id
    row[f"candidate_max_{axis_lower}_bin_{metric}_common_good_epochs"] = int(n_common)


def _summary(
    rows: list[dict[str, Any]],
    *,
    pair_rows: list[dict[str, Any]],
    axis_bins: int,
    min_common_good_epochs: int,
    min_abs_spearman_rho: float,
    min_abs_pearson_r: float,
    detector_width: float | None,
    detector_height: float | None,
) -> dict[str, Any]:
    eligible = [row for row in rows if _eligible_candidate(row)]
    counts: dict[str, int] = {}
    mode_counts: dict[str, int] = {}
    for row in eligible:
        status = str(row.get("candidate_axis_correlation_status") or "UNSET")
        counts[status] = counts.get(status, 0) + 1
        mode = str(row.get("candidate_axis_correlation_mode") or "NONE")
        mode_counts[mode] = mode_counts.get(mode, 0) + 1
    return {
        "enabled": True,
        "algorithm": CANDIDATE_AXIS_CORRELATION_ALGORITHM,
        "diagnostic_only": True,
        "axis_bins": int(axis_bins),
        "binning_schemes": ["PRIMARY", "HALF_SHIFTED"],
        "min_common_good_epochs": int(min_common_good_epochs),
        "min_abs_spearman_rho": float(min_abs_spearman_rho),
        "min_abs_pearson_r": float(min_abs_pearson_r),
        "pair_flag_policy": "ABS_SPEARMAN_OR_ABS_PEARSON",
        "detector_width": detector_width,
        "detector_height": detector_height,
        "eligible_candidates": len(eligible),
        "evaluated_pairs": len(pair_rows),
        "correlated_pairs": sum(1 for row in pair_rows if row.get("correlated")),
        "spearman_correlated_pairs": sum(1 for row in pair_rows if row.get("spearman_correlated")),
        "pearson_correlated_pairs": sum(1 for row in pair_rows if row.get("pearson_correlated")),
        "both_correlated_pairs": sum(1 for row in pair_rows if row.get("correlation_mode") == "BOTH"),
        "candidates_correlated_x": sum(int(row.get("candidate_n_x_correlated_candidates") or 0) > 0 for row in eligible),
        "candidates_correlated_y": sum(int(row.get("candidate_n_y_correlated_candidates") or 0) > 0 for row in eligible),
        "candidates_correlated_any": sum(
            (int(row.get("candidate_n_x_correlated_candidates") or 0) > 0)
            or (int(row.get("candidate_n_y_correlated_candidates") or 0) > 0)
            for row in eligible
        ),
        "candidates_correlated_spearman": sum(
            (int(row.get("candidate_n_x_spearman_correlated_candidates") or 0) > 0)
            or (int(row.get("candidate_n_y_spearman_correlated_candidates") or 0) > 0)
            for row in eligible
        ),
        "candidates_correlated_pearson": sum(
            (int(row.get("candidate_n_x_pearson_correlated_candidates") or 0) > 0)
            or (int(row.get("candidate_n_y_pearson_correlated_candidates") or 0) > 0)
            for row in eligible
        ),
        "candidate_mode_counts": mode_counts,
        "status_counts": counts,
    }


def _detector_extent(
    session: Session,
    frame_ids: list[int],
    *,
    x_values: list[float],
    y_values: list[float],
) -> tuple[float, float]:
    widths: list[float] = []
    heights: list[float] = []
    if frame_ids:
        frames = session.execute(select(Frame).where(Frame.frame_id.in_(frame_ids))).scalars().all()
        for frame in frames:
            header = frame.header_json or {}
            width = _finite_or_none(header.get("NAXIS1"))
            height = _finite_or_none(header.get("NAXIS2"))
            if width is not None and width > 0:
                widths.append(width)
            if height is not None and height > 0:
                heights.append(height)
    width = float(statistics.median(widths)) if widths else (max(x_values) + 1.0 if x_values else 1.0)
    height = float(statistics.median(heights)) if heights else (max(y_values) + 1.0 if y_values else 1.0)
    return max(width, 1.0), max(height, 1.0)


def _axis_bin(
    value: float,
    extent: float,
    n_bins: int,
    *,
    half_bin_shift: bool = False,
) -> int:
    fraction = float(value) / max(float(extent), 1.0)
    coordinate = fraction * int(n_bins) + (0.5 if half_bin_shift else 0.0)
    return max(0, min(int(n_bins) - 1, int(math.floor(coordinate))))


def _spearman(values_a: list[float], values_b: list[float]) -> float | None:
    if len(values_a) != len(values_b) or len(values_a) < 3:
        return None
    return _pearson(_average_ranks(values_a), _average_ranks(values_b))


def _pearson(values_a: list[float], values_b: list[float]) -> float | None:
    if len(values_a) != len(values_b) or len(values_a) < 3:
        return None
    mean_a = sum(values_a) / len(values_a)
    mean_b = sum(values_b) / len(values_b)
    numerator = sum((a - mean_a) * (b - mean_b) for a, b in zip(values_a, values_b))
    denom_a = sum((a - mean_a) ** 2 for a in values_a)
    denom_b = sum((b - mean_b) ** 2 for b in values_b)
    denominator = math.sqrt(denom_a * denom_b)
    if denominator <= 0:
        return None
    return float(numerator / denominator)


def _average_ranks(values: list[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    pos = 0
    while pos < len(ordered):
        end = pos + 1
        while end < len(ordered) and ordered[end][1] == ordered[pos][1]:
            end += 1
        average_rank = (pos + 1 + end) / 2.0
        for idx in range(pos, end):
            ranks[ordered[idx][0]] = average_rank
        pos = end
    return ranks


def _passes_abs_threshold(value: float | None, threshold: float) -> bool:
    return bool(
        value is not None
        and math.isfinite(float(value))
        and abs(float(value)) >= float(threshold)
    )


def _correlation_mode(spearman_correlated: bool, pearson_correlated: bool) -> str:
    if spearman_correlated and pearson_correlated:
        return "BOTH"
    if spearman_correlated:
        return "SPEARMAN"
    if pearson_correlated:
        return "PEARSON"
    return "NONE"


def _mode_from_candidate_counts(row: dict[str, Any], axis_lower: str) -> str:
    return _correlation_mode(
        int(row.get(f"candidate_n_{axis_lower}_spearman_correlated_candidates") or 0) > 0,
        int(row.get(f"candidate_n_{axis_lower}_pearson_correlated_candidates") or 0) > 0,
    )


def _validate_policy(
    axis_bins: int,
    min_common_good_epochs: int,
    min_abs_spearman_rho: float,
    min_abs_pearson_r: float,
) -> None:
    if int(axis_bins) < 2:
        raise ValueError("axis_bins must be >= 2")
    if int(min_common_good_epochs) < 3:
        raise ValueError("min_common_good_epochs must be >= 3")
    if not math.isfinite(float(min_abs_spearman_rho)) or not 0.0 <= float(min_abs_spearman_rho) <= 1.0:
        raise ValueError("min_abs_spearman_rho must be between 0 and 1")
    if not math.isfinite(float(min_abs_pearson_r)) or not 0.0 <= float(min_abs_pearson_r) <= 1.0:
        raise ValueError("min_abs_pearson_r must be between 0 and 1")


def _finite_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
