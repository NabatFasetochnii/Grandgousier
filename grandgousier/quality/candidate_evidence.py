from __future__ import annotations

import csv
import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


CANDIDATE_EVIDENCE_COLUMNS: tuple[str, ...] = (
    "evidence_rank",
    "evidence_status",
    "source_id",
    "object_ids",
    "catalog_names",
    "series_count",
    "accepted_series_count",
    "auto_accepted_series_count",
    "forced_target_series_count",
    "borderline_series_count",
    "diagnostic_series_count",
    "effective_error_pass_series_count",
    "filters",
    "cameras",
    "series_ids",
    "best_pair_series_a",
    "best_pair_series_b",
    "best_pair_filter_a",
    "best_pair_filter_b",
    "best_pair_n_pairs",
    "best_pair_pearson_r",
    "best_pair_pearson_p",
    "best_pair_pearson_fdr_q",
    "best_pair_spearman_rho",
    "best_pair_spearman_p",
    "best_pair_spearman_fdr_q",
    "best_pair_significance_status",
    "best_pair_max_time_separation_sec",
    "coherent_pair_count",
    "significant_positive_pair_count",
    "significant_negative_pair_count",
    "repeated_accepted",
    "multi_filter_coherent",
    "multi_filter_significant_positive",
    "multi_filter_significant_negative",
    "is_target",
    "same_filter_comparable_filter_count",
    "same_filter_max_median_delta",
    "same_filter_median_range_by_filter",
    "all_filter_median_range",
    "median_mag_range",
    "median_mag_range_semantics",
)

PAIR_EVIDENCE_COLUMNS: tuple[str, ...] = (
    "source_id",
    "series_a",
    "series_b",
    "series_id_a",
    "series_id_b",
    "filter_a",
    "filter_b",
    "camera_a",
    "camera_b",
    "n_pairs",
    "pearson_r",
    "pearson_p",
    "pearson_fdr_q",
    "spearman_rho",
    "spearman_p",
    "spearman_fdr_q",
    "max_time_separation_sec",
    "median_time_separation_sec",
    "coherent",
    "different_filter",
    "significant_positive",
    "significant_negative",
    "significance_status",
)


@dataclass(frozen=True)
class CandidateSeries:
    root: Path
    candidates_dir: Path
    label: str
    series_id: int | None
    field_name: str | None
    filter_name: str | None
    camera: str | None
    rows_by_source: dict[str, dict[str, Any]]
    light_curves_by_source: dict[str, list[dict[str, Any]]]

    def metadata(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "candidates_dir": str(self.candidates_dir),
            "label": self.label,
            "series_id": self.series_id,
            "field_name": self.field_name,
            "filter_name": self.filter_name,
            "camera": self.camera,
            "candidate_rows": len(self.rows_by_source),
            "light_curves": len(self.light_curves_by_source),
        }


@dataclass(frozen=True)
class CandidateEvidenceReport:
    series: list[dict[str, Any]]
    rows: list[dict[str, Any]]
    pair_rows: list[dict[str, Any]]
    parameters: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "algorithm": "candidate_cross_series_evidence_v2",
            "parameters": dict(self.parameters),
            "series": list(self.series),
            "summary": {
                "series_count": len(self.series),
                "candidate_sources": len(self.rows),
                "pair_rows": len(self.pair_rows),
                "multi_filter_coherent_sources": sum(
                    1 for row in self.rows if row.get("multi_filter_coherent")
                ),
                "repeated_accepted_sources": sum(
                    1 for row in self.rows if row.get("repeated_accepted")
                ),
                "significant_positive_sources": sum(
                    1 for row in self.rows if row.get("multi_filter_significant_positive")
                ),
                "significant_negative_sources": sum(
                    1 for row in self.rows if row.get("multi_filter_significant_negative")
                ),
                "target_sources": sum(
                    1 for row in self.rows if row.get("is_target")
                ),
            },
            "candidates": list(self.rows),
            "pairs": list(self.pair_rows),
        }


def build_candidate_evidence_report(
    series_dirs: Iterable[str | Path],
    *,
    max_time_separation_sec: float = 120.0,
    min_pairs: int = 10,
    min_correlation: float = 0.25,
    max_fdr_q: float = 0.05,
    min_repeat_series: int = 2,
    target_source_ids: Iterable[str] | None = None,
) -> CandidateEvidenceReport:
    series_paths = [Path(path).expanduser().resolve() for path in series_dirs]
    _validate_candidate_evidence_parameters(
        series_paths=series_paths,
        max_time_separation_sec=max_time_separation_sec,
        min_pairs=min_pairs,
        min_correlation=min_correlation,
        max_fdr_q=max_fdr_q,
        min_repeat_series=min_repeat_series,
    )
    loaded = [load_candidate_series(path) for path in series_paths]
    explicit_target_source_ids = {
        str(value).strip() for value in (target_source_ids or []) if str(value).strip()
    }
    source_ids = sorted({source_id for series in loaded for source_id in series.rows_by_source})
    pair_rows: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []

    for source_id in source_ids:
        appearances = [series for series in loaded if source_id in series.rows_by_source]
        accepted = [
            series
            for series in appearances
            if _status(series.rows_by_source[source_id]) == "ACCEPTED"
        ]
        auto_accepted = [
            series
            for series in appearances
            if _is_auto_accepted(series.rows_by_source[source_id])
        ]
        forced_targets = [
            series
            for series in appearances
            if _is_forced_target(series.rows_by_source[source_id])
        ]
        borderline = [
            series
            for series in appearances
            if _status(series.rows_by_source[source_id]) == "BORDERLINE"
        ]
        diagnostic = [
            series
            for series in appearances
            if _status(series.rows_by_source[source_id]) == "DIAGNOSTIC"
        ]
        effective_pass = [
            series
            for series in appearances
            if str(series.rows_by_source[source_id].get("residual_floor_ab_status") or "").upper()
            == "PASS"
        ]
        source_pairs: list[dict[str, Any]] = []
        for idx, series_a in enumerate(appearances):
            curve_a = series_a.light_curves_by_source.get(source_id)
            if not curve_a:
                continue
            for series_b in appearances[idx + 1 :]:
                curve_b = series_b.light_curves_by_source.get(source_id)
                if not curve_b:
                    continue
                pair = compare_light_curves(
                    curve_a,
                    curve_b,
                    max_time_separation_sec=max_time_separation_sec,
                )
                pair_row = {
                    "source_id": source_id,
                    "series_a": series_a.label,
                    "series_b": series_b.label,
                    "series_id_a": series_a.series_id,
                    "series_id_b": series_b.series_id,
                    "filter_a": series_a.filter_name,
                    "filter_b": series_b.filter_name,
                    "camera_a": series_a.camera,
                    "camera_b": series_b.camera,
                    **pair,
                }
                different_filter = bool(
                    series_a.filter_name
                    and series_b.filter_name
                    and series_a.filter_name != series_b.filter_name
                )
                coherent = bool(
                    int(pair_row.get("n_pairs") or 0) >= int(min_pairs)
                    and _at_least(pair_row.get("pearson_r"), min_correlation)
                    and _at_least(pair_row.get("spearman_rho"), min_correlation)
                )
                pair_row["coherent"] = coherent
                pair_row["different_filter"] = different_filter
                source_pairs.append(pair_row)
                pair_rows.append(pair_row)

        coherent_pairs = [pair for pair in source_pairs if pair.get("coherent")]
        multi_filter_pairs = [
            pair for pair in coherent_pairs if pair.get("different_filter")
        ]
        best_pair = max(
            source_pairs,
            key=lambda pair: (
                _correlation_sort_value(pair.get("pearson_r")),
                _correlation_sort_value(pair.get("spearman_rho")),
                int(pair.get("n_pairs") or 0),
            ),
            default=None,
        )
        repeated_accepted = len(auto_accepted) >= max(2, int(min_repeat_series))
        multi_filter_coherent = bool(multi_filter_pairs)
        is_target = source_id in explicit_target_source_ids or any(
            _truthy(series.rows_by_source[source_id].get("is_target")) for series in appearances
        )
        if multi_filter_coherent:
            evidence_status = "COHERENT_MULTI_FILTER"
        elif repeated_accepted:
            evidence_status = "REPEATED_ACCEPTED"
        elif effective_pass:
            evidence_status = "EFFECTIVE_ERROR_PASS"
        elif auto_accepted:
            evidence_status = "WEAK_SINGLE_SERIES_ACCEPTED"
        elif diagnostic or borderline:
            evidence_status = "DIAGNOSTIC_ONLY"
        else:
            evidence_status = "NO_SUPPORT"
        if is_target:
            evidence_status = f"TARGET_{evidence_status}"

        magnitude_ranges = _summarize_median_magnitude_ranges(
            appearances,
            source_id=source_id,
        )
        rows.append(
            {
                "evidence_status": evidence_status,
                "source_id": source_id,
                "object_ids": _join_unique(
                    series.rows_by_source[source_id].get("object_id")
                    for series in appearances
                ),
                "catalog_names": _join_unique(
                    series.rows_by_source[source_id].get("catalog_name")
                    for series in appearances
                ),
                "series_count": len(appearances),
                "accepted_series_count": len(accepted),
                "auto_accepted_series_count": len(auto_accepted),
                "forced_target_series_count": len(forced_targets),
                "borderline_series_count": len(borderline),
                "diagnostic_series_count": len(diagnostic),
                "effective_error_pass_series_count": len(effective_pass),
                "filters": _join_unique(series.filter_name for series in appearances),
                "cameras": _join_unique(series.camera for series in appearances),
                "series_ids": _join_unique(series.series_id for series in appearances),
                "best_pair_series_a": best_pair.get("series_a") if best_pair else None,
                "best_pair_series_b": best_pair.get("series_b") if best_pair else None,
                "best_pair_filter_a": best_pair.get("filter_a") if best_pair else None,
                "best_pair_filter_b": best_pair.get("filter_b") if best_pair else None,
                "best_pair_n_pairs": best_pair.get("n_pairs") if best_pair else 0,
                "best_pair_pearson_r": best_pair.get("pearson_r") if best_pair else None,
                "best_pair_pearson_p": best_pair.get("pearson_p") if best_pair else None,
                "best_pair_pearson_fdr_q": None,
                "best_pair_spearman_rho": best_pair.get("spearman_rho") if best_pair else None,
                "best_pair_spearman_p": best_pair.get("spearman_p") if best_pair else None,
                "best_pair_spearman_fdr_q": None,
                "best_pair_significance_status": None,
                "best_pair_max_time_separation_sec": (
                    best_pair.get("max_time_separation_sec") if best_pair else None
                ),
                "coherent_pair_count": len(coherent_pairs),
                "significant_positive_pair_count": 0,
                "significant_negative_pair_count": 0,
                "repeated_accepted": repeated_accepted,
                "multi_filter_coherent": multi_filter_coherent,
                "multi_filter_significant_positive": False,
                "multi_filter_significant_negative": False,
                "is_target": is_target,
                **magnitude_ranges,
            }
        )

    _apply_pair_fdr_and_significance(
        pair_rows,
        min_pairs=min_pairs,
        min_correlation=min_correlation,
        max_fdr_q=max_fdr_q,
    )
    _refresh_candidate_pair_significance(rows, pair_rows)
    rows.sort(key=_candidate_evidence_sort_key)
    for index, row in enumerate(rows, start=1):
        row["evidence_rank"] = index
    pair_rows.sort(
        key=lambda row: (
            str(row.get("source_id") or ""),
            str(row.get("series_a") or ""),
            str(row.get("series_b") or ""),
        )
    )
    return CandidateEvidenceReport(
        series=[series.metadata() for series in loaded],
        rows=rows,
        pair_rows=pair_rows,
        parameters={
            "max_time_separation_sec": float(max_time_separation_sec),
            "min_pairs": int(min_pairs),
            "min_correlation": float(min_correlation),
            "max_fdr_q": float(max_fdr_q),
            "min_repeat_series": int(min_repeat_series),
            "target_source_ids": sorted(explicit_target_source_ids),
        },
    )


def load_candidate_series(path: str | Path) -> CandidateSeries:
    root = Path(path).expanduser().resolve()
    candidates_dir = root if root.name.lower() == "candidates" else root / "candidates"
    summary_path = candidates_dir / "candidates_summary.csv"
    if not summary_path.exists():
        raise FileNotFoundError(f"candidate summary not found: {summary_path}")
    with summary_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    rows_by_source = {
        str(row.get("source_id") or "").strip(): row
        for row in rows
        if str(row.get("source_id") or "").strip()
    }
    light_curves_by_source: dict[str, list[dict[str, Any]]] = {}
    series_id: int | None = None
    field_name: str | None = None
    filter_name: str | None = None
    camera: str | None = None
    for source_id in rows_by_source:
        matches = sorted(candidates_dir.glob(f"object_*_src_{source_id}_light_curve.csv"))
        if not matches:
            continue
        curve = _read_light_curve(matches[0])
        if not curve:
            continue
        light_curves_by_source[source_id] = curve
        first = curve[0]
        series_id = series_id if series_id is not None else _int_or_none(first.get("series_id"))
        field_name = field_name or _none_if_blank(first.get("field_name"))
        filter_name = filter_name or _none_if_blank(first.get("filter_name"))
        camera = camera or _none_if_blank(first.get("camera"))
    label = root.name if root.name.lower() != "candidates" else root.parent.name
    return CandidateSeries(
        root=root,
        candidates_dir=candidates_dir,
        label=label,
        series_id=series_id,
        field_name=field_name,
        filter_name=filter_name,
        camera=camera,
        rows_by_source=rows_by_source,
        light_curves_by_source=light_curves_by_source,
    )


def compare_light_curves(
    curve_a: list[dict[str, Any]],
    curve_b: list[dict[str, Any]],
    *,
    max_time_separation_sec: float,
) -> dict[str, Any]:
    points_a = _usable_curve_points(curve_a)
    points_b = _usable_curve_points(curve_b)
    pairs = _nearest_time_pairs(
        points_a,
        points_b,
        max_time_separation_days=float(max_time_separation_sec) / 86400.0,
    )
    values_a = [pair[0] for pair in pairs]
    values_b = [pair[1] for pair in pairs]
    separations = [pair[2] * 86400.0 for pair in pairs]
    pearson_r = _pearson(values_a, values_b)
    spearman_rho = _spearman(values_a, values_b)
    return {
        "n_pairs": len(pairs),
        "pearson_r": pearson_r,
        "pearson_p": _correlation_p_value(pearson_r, len(pairs)),
        "spearman_rho": spearman_rho,
        "spearman_p": _correlation_p_value(spearman_rho, len(pairs)),
        "max_time_separation_sec": max(separations) if separations else None,
        "median_time_separation_sec": statistics.median(separations) if separations else None,
    }


def export_candidate_evidence_report(
    report: CandidateEvidenceReport,
    *,
    out_prefix: str | Path,
) -> dict[str, str]:
    prefix = Path(out_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = prefix.with_suffix(".json")
    candidates_csv_path = prefix.with_name(prefix.name + "_candidates.csv")
    pairs_csv_path = prefix.with_name(prefix.name + "_pairs.csv")
    json_path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _write_csv(candidates_csv_path, report.rows, CANDIDATE_EVIDENCE_COLUMNS)
    _write_csv(pairs_csv_path, report.pair_rows, PAIR_EVIDENCE_COLUMNS)
    return {
        "json": str(json_path),
        "candidates_csv": str(candidates_csv_path),
        "pairs_csv": str(pairs_csv_path),
    }


def _read_light_curve(path: Path) -> list[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _usable_curve_points(rows: list[dict[str, Any]]) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for row in rows:
        if _truthy(row.get("is_rejected")):
            continue
        mjd = _float_or_none(row.get("mjd"))
        mag = _float_or_none(row.get("mag_cal"))
        if mjd is None or mag is None:
            continue
        points.append((mjd, mag))
    if not points:
        return []
    median_mag = statistics.median(value for _, value in points)
    return sorted((mjd, mag - median_mag) for mjd, mag in points)


def _nearest_time_pairs(
    points_a: list[tuple[float, float]],
    points_b: list[tuple[float, float]],
    *,
    max_time_separation_days: float,
) -> list[tuple[float, float, float]]:
    candidates: list[tuple[float, int, int]] = []
    for index_a, (mjd_a, _value_a) in enumerate(points_a):
        for index_b, (mjd_b, _value_b) in enumerate(points_b):
            delta = abs(mjd_a - mjd_b)
            if delta <= max_time_separation_days:
                candidates.append((delta, index_a, index_b))
    candidates.sort()
    used_a: set[int] = set()
    used_b: set[int] = set()
    pairs: list[tuple[float, float, float]] = []
    for delta, index_a, index_b in candidates:
        if index_a in used_a or index_b in used_b:
            continue
        used_a.add(index_a)
        used_b.add(index_b)
        pairs.append((points_a[index_a][1], points_b[index_b][1], delta))
    return pairs


def _pearson(values_a: list[float], values_b: list[float]) -> float | None:
    if len(values_a) < 3 or len(values_a) != len(values_b):
        return None
    mean_a = statistics.fmean(values_a)
    mean_b = statistics.fmean(values_b)
    da = [value - mean_a for value in values_a]
    db = [value - mean_b for value in values_b]
    denominator = math.sqrt(sum(value * value for value in da) * sum(value * value for value in db))
    if denominator <= 0:
        return None
    return sum(a * b for a, b in zip(da, db)) / denominator


def _spearman(values_a: list[float], values_b: list[float]) -> float | None:
    if len(values_a) < 3 or len(values_a) != len(values_b):
        return None
    return _pearson(_ranks(values_a), _ranks(values_b))


def _ranks(values: list[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        rank = (index + 1 + end) / 2.0
        for original_index, _value in ordered[index:end]:
            ranks[original_index] = rank
        index = end
    return ranks


def _write_csv(path: Path, rows: list[dict[str, Any]], columns: Iterable[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(_csv_safe_row(row) for row in rows)


def _summarize_median_magnitude_ranges(
    appearances: list[CandidateSeries],
    *,
    source_id: str,
) -> dict[str, Any]:
    by_filter: dict[str, list[float]] = {}
    all_values: list[float] = []
    for series in appearances:
        value = _float_or_none(series.rows_by_source[source_id].get("mag_median"))
        if value is None:
            continue
        all_values.append(value)
        filter_name = _none_if_blank(series.filter_name)
        if filter_name is not None:
            by_filter.setdefault(filter_name, []).append(value)

    ranges_by_filter = {
        filter_name: max(values) - min(values)
        for filter_name, values in sorted(by_filter.items())
        if len(values) >= 2
    }
    same_filter_max = max(ranges_by_filter.values()) if ranges_by_filter else None
    all_filter_range = max(all_values) - min(all_values) if len(all_values) >= 2 else None
    return {
        "same_filter_comparable_filter_count": len(ranges_by_filter),
        "same_filter_max_median_delta": same_filter_max,
        "same_filter_median_range_by_filter": ranges_by_filter,
        "all_filter_median_range": all_filter_range,
        # Backwards-compatible column name from v1. Its semantics are corrected
        # in v2: it never mixes filters and aliases same_filter_max_median_delta.
        "median_mag_range": same_filter_max,
        "median_mag_range_semantics": "same_filter_max_median_delta_v2",
    }


def _candidate_origin_parts(value: Any) -> set[str]:
    return {
        part.strip().lower()
        for part in str(value or "").split(",")
        if part.strip()
    }


def _is_forced_target(row: dict[str, Any]) -> bool:
    if _status(row) != "ACCEPTED" or not _truthy(row.get("is_target")):
        return False
    origins = _candidate_origin_parts(row.get("candidate_origin"))
    return not origins or origins == {"target"}


def _is_auto_accepted(row: dict[str, Any]) -> bool:
    if _status(row) != "ACCEPTED":
        return False
    if not _truthy(row.get("is_target")):
        return True
    return bool(_candidate_origin_parts(row.get("candidate_origin")) - {"target"})


def _correlation_p_value(correlation: float | None, n_pairs: int) -> float | None:
    """Two-sided Fisher-z normal approximation, dependency-free and diagnostic."""

    value = _float_or_none(correlation)
    if value is None or int(n_pairs) < 4:
        return None
    absolute = min(abs(value), 1.0)
    if absolute >= 1.0:
        return 0.0
    z_score = math.atanh(absolute) * math.sqrt(max(1.0, float(n_pairs) - 3.0))
    return math.erfc(z_score / math.sqrt(2.0))


def _benjamini_hochberg(values: list[tuple[int, float]]) -> dict[int, float]:
    if not values:
        return {}
    ordered = sorted(values, key=lambda item: item[1])
    total = len(ordered)
    adjusted: dict[int, float] = {}
    running = 1.0
    for rank_from_end, (row_index, p_value) in enumerate(reversed(ordered), start=1):
        rank = total - rank_from_end + 1
        candidate = min(1.0, float(p_value) * total / rank)
        running = min(running, candidate)
        adjusted[row_index] = running
    return adjusted


def _apply_pair_fdr_and_significance(
    pair_rows: list[dict[str, Any]],
    *,
    min_pairs: int,
    min_correlation: float,
    max_fdr_q: float,
) -> None:
    eligible = [
        index
        for index, row in enumerate(pair_rows)
        if int(row.get("n_pairs") or 0) >= int(min_pairs)
    ]
    pearson_q = _benjamini_hochberg(
        [
            (index, p_value)
            for index in eligible
            if (p_value := _float_or_none(pair_rows[index].get("pearson_p"))) is not None
        ]
    )
    spearman_q = _benjamini_hochberg(
        [
            (index, p_value)
            for index in eligible
            if (p_value := _float_or_none(pair_rows[index].get("spearman_p"))) is not None
        ]
    )
    for index, row in enumerate(pair_rows):
        row["pearson_fdr_q"] = pearson_q.get(index)
        row["spearman_fdr_q"] = spearman_q.get(index)
        enough_pairs = int(row.get("n_pairs") or 0) >= int(min_pairs)
        pearson = _float_or_none(row.get("pearson_r"))
        spearman = _float_or_none(row.get("spearman_rho"))
        pearson_q_value = _float_or_none(row.get("pearson_fdr_q"))
        spearman_q_value = _float_or_none(row.get("spearman_fdr_q"))
        fdr_pass = bool(
            pearson_q_value is not None
            and spearman_q_value is not None
            and pearson_q_value <= float(max_fdr_q)
            and spearman_q_value <= float(max_fdr_q)
        )
        positive = bool(
            enough_pairs
            and fdr_pass
            and pearson is not None
            and spearman is not None
            and pearson >= float(min_correlation)
            and spearman >= float(min_correlation)
        )
        negative = bool(
            enough_pairs
            and fdr_pass
            and pearson is not None
            and spearman is not None
            and pearson <= -float(min_correlation)
            and spearman <= -float(min_correlation)
        )
        row["significant_positive"] = positive
        row["significant_negative"] = negative
        if not enough_pairs:
            status = "INSUFFICIENT_PAIRS"
        elif positive:
            status = "SIGNIFICANT_POSITIVE"
        elif negative:
            status = "SIGNIFICANT_NEGATIVE"
        else:
            status = "NOT_SIGNIFICANT"
        row["significance_status"] = status


def _refresh_candidate_pair_significance(
    rows: list[dict[str, Any]],
    pair_rows: list[dict[str, Any]],
) -> None:
    pairs_by_source: dict[str, list[dict[str, Any]]] = {}
    for pair in pair_rows:
        pairs_by_source.setdefault(str(pair.get("source_id") or ""), []).append(pair)
    for row in rows:
        source_pairs = pairs_by_source.get(str(row.get("source_id") or ""), [])
        best_pair = max(
            source_pairs,
            key=lambda pair: (
                _correlation_sort_value(pair.get("pearson_r")),
                _correlation_sort_value(pair.get("spearman_rho")),
                int(pair.get("n_pairs") or 0),
            ),
            default=None,
        )
        if best_pair is not None:
            row["best_pair_pearson_fdr_q"] = best_pair.get("pearson_fdr_q")
            row["best_pair_spearman_fdr_q"] = best_pair.get("spearman_fdr_q")
            row["best_pair_significance_status"] = best_pair.get("significance_status")
        positive_pairs = [pair for pair in source_pairs if pair.get("significant_positive")]
        negative_pairs = [pair for pair in source_pairs if pair.get("significant_negative")]
        row["significant_positive_pair_count"] = len(positive_pairs)
        row["significant_negative_pair_count"] = len(negative_pairs)
        row["multi_filter_significant_positive"] = any(
            pair.get("different_filter") for pair in positive_pairs
        )
        row["multi_filter_significant_negative"] = any(
            pair.get("different_filter") for pair in negative_pairs
        )


def _csv_safe_row(row: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, (dict, list, tuple)):
            result[key] = json.dumps(value, ensure_ascii=False, sort_keys=True)
        else:
            result[key] = value
    return result



def _validate_candidate_evidence_parameters(
    *,
    series_paths: list[Path],
    max_time_separation_sec: float,
    min_pairs: int,
    min_correlation: float,
    max_fdr_q: float,
    min_repeat_series: int,
) -> None:
    if len(series_paths) < 2:
        raise ValueError("candidate evidence requires at least two series directories")
    duplicate_paths = sorted(
        {str(path) for path in series_paths if series_paths.count(path) > 1}
    )
    if duplicate_paths:
        raise ValueError(
            "candidate evidence series directories must be unique: "
            + ", ".join(duplicate_paths)
        )
    if not math.isfinite(float(max_time_separation_sec)) or float(max_time_separation_sec) < 0:
        raise ValueError("max_time_separation_sec must be a finite non-negative value")
    if int(min_pairs) < 3:
        raise ValueError("min_pairs must be at least 3 for correlation evidence")
    if not math.isfinite(float(min_correlation)) or not -1.0 <= float(min_correlation) <= 1.0:
        raise ValueError("min_correlation must be between -1 and 1")
    if not math.isfinite(float(max_fdr_q)) or not 0.0 < float(max_fdr_q) <= 1.0:
        raise ValueError("max_fdr_q must be in the interval (0, 1]")
    if int(min_repeat_series) < 2:
        raise ValueError("min_repeat_series must be at least 2")

def _candidate_evidence_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    priority = {
        "TARGET_COHERENT_MULTI_FILTER": 0,
        "COHERENT_MULTI_FILTER": 1,
        "TARGET_REPEATED_ACCEPTED": 2,
        "REPEATED_ACCEPTED": 3,
        "TARGET_EFFECTIVE_ERROR_PASS": 4,
        "EFFECTIVE_ERROR_PASS": 5,
        "TARGET_WEAK_SINGLE_SERIES_ACCEPTED": 6,
        "WEAK_SINGLE_SERIES_ACCEPTED": 7,
        "TARGET_DIAGNOSTIC_ONLY": 8,
        "DIAGNOSTIC_ONLY": 9,
        "TARGET_NO_SUPPORT": 10,
        "NO_SUPPORT": 11,
    }
    return (
        priority.get(str(row.get("evidence_status") or ""), 99),
        -int(row.get("auto_accepted_series_count") or 0),
        -int(row.get("coherent_pair_count") or 0),
        -_correlation_sort_value(row.get("best_pair_pearson_r")),
        str(row.get("source_id") or ""),
    )



def _correlation_sort_value(value: Any) -> float:
    numeric = _float_or_none(value)
    return numeric if numeric is not None else -2.0

def _status(row: dict[str, Any]) -> str:
    return str(row.get("candidate_status") or "").upper()


def _at_least(value: Any, threshold: float) -> bool:
    numeric = _float_or_none(value)
    return numeric is not None and numeric >= float(threshold)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _float_or_none(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _none_if_blank(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _join_unique(values: Iterable[Any]) -> str:
    seen: list[str] = []
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text and text not in seen:
            seen.append(text)
    return ",".join(seen)
