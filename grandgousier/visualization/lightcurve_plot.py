from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence
from statistics import median

from grandgousier.calibration.preliminary import LightcurveRow


@dataclass
class LightcurvePlotResult:
    output_path: str
    n_rows: int
    n_points: int
    x_key: str
    y_key: str
    inverted_y: bool
    x_offset: float | None = None
    time_warning: str | None = None
    time_axis_mode: str | None = None
    n_time_breaks: int = 0
    plot_style: str | None = None


@dataclass(frozen=True)
class ObservatoryLocation:
    name: str
    lat_deg: float
    lon_deg: float
    height_m: float


# Default values are for Kourovka Astronomical Observatory.
# They are only used for on-the-fly BJD_TDB plotting/export diagnostics.
KNOWN_OBSERVATORIES: dict[str, ObservatoryLocation] = {
    "KAO": ObservatoryLocation(name="KAO", lat_deg=57.0363888889, lon_deg=59.5416666667, height_m=290.0),
    "KOUROVKA": ObservatoryLocation(name="KAO", lat_deg=57.0363888889, lon_deg=59.5416666667, height_m=290.0),
}


def export_lightcurve_plot(
    rows: Iterable[LightcurveRow],
    path: str | Path,
    *,
    x_key: str = "mjd_zero",
    y_key: str = "mag_cal",
    title: str | None = None,
    dpi: int = 150,
    figsize: tuple[float, float] = (8.0, 4.8),
    invert_y: bool | None = None,
    show_errors: bool = True,
    show_stats: bool = False,
    observatory_name: str | None = "KAO",
    observatory_lat_deg: float | None = None,
    observatory_lon_deg: float | None = None,
    observatory_height_m: float | None = None,
) -> LightcurvePlotResult:
    """Export a light-curve diagnostic plot as PNG/PDF/SVG/etc.

    Supported x-axis keys:
      mjd, mjd_zero, mjd_frac, bmjd_tdb, bmjd_tdb_zero, bmjd_tdb_frac,
      bjd_tdb, bjd_tdb_zero, bjd_tdb_frac, frame_id, measurement_id.

    BJD_TDB values are computed on the fly from UTC MJD, object coordinates, and
    observatory location. They are not written back to the database by this helper.
    """

    rows_list = list(rows)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    x_values_raw, time_warning, x_offset = _build_x_values(
        rows_list,
        x_key,
        observatory_name=observatory_name,
        observatory_lat_deg=observatory_lat_deg,
        observatory_lon_deg=observatory_lon_deg,
        observatory_height_m=observatory_height_m,
    )

    y_values_raw: list[float | None] = []
    y_errors_raw: list[float | None] = []
    for row in rows_list:
        y = _row_value(row, y_key)
        y_values_raw.append(float(y) if _is_finite(y) else None)
        y_errors_raw.append(_error_for_y(row, y_key) if show_errors else None)

    x_values: list[float] = []
    y_values: list[float] = []
    y_errors: list[float | None] = []
    for x, y, err in zip(x_values_raw, y_values_raw, y_errors_raw, strict=False):
        if not _is_finite(x) or not _is_finite(y):
            continue
        x_values.append(float(x))
        y_values.append(float(y))
        y_errors.append(err)

    if not x_values:
        raise ValueError(f"No finite points for x={x_key!r}, y={y_key!r}")

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:  # pragma: no cover - depends on optional dependency
        raise RuntimeError(
            "matplotlib is required for export-lightcurve-plot. "
            "Install it with: python -m pip install matplotlib"
        ) from exc

    fig, ax = plt.subplots(figsize=figsize)

    finite_errors = [e for e in y_errors if e is not None and _is_finite(e) and float(e) > 0]
    if show_errors and finite_errors and len(finite_errors) == len(y_values):
        ax.errorbar(x_values, y_values, yerr=[float(e) for e in y_errors], marker="o", linestyle="-", capsize=2)
    else:
        ax.plot(x_values, y_values, marker="o", linestyle="-")

    x_label = _axis_label_for_x(x_key, x_offset)
    ax.set_xlabel(x_label)
    ax.set_ylabel(_axis_label(y_key))
    ax.grid(True, alpha=0.3)

    if title:
        ax.set_title(title)
    else:
        ax.set_title(_default_title(rows_list))

    if show_stats:
        ax.text(
            0.02,
            0.98,
            _stats_text(y_values, y_key),
            transform=ax.transAxes,
            va="top",
            ha="left",
            fontsize=9,
            bbox={"boxstyle": "round", "facecolor": "white", "alpha": 0.75, "edgecolor": "0.7"},
        )

    if time_warning:
        ax.text(
            0.98,
            0.02,
            time_warning,
            transform=ax.transAxes,
            va="bottom",
            ha="right",
            fontsize=8,
            alpha=0.7,
        )

    if invert_y is None:
        invert_y = y_key in {"mag_cal", "mag_inst", "mag_err"}
    if invert_y:
        ax.invert_yaxis()

    fig.tight_layout()
    fig.savefig(path, dpi=dpi)
    plt.close(fig)

    return LightcurvePlotResult(
        output_path=str(path),
        n_rows=len(rows_list),
        n_points=len(x_values),
        x_key=x_key,
        y_key=y_key,
        inverted_y=bool(invert_y),
        x_offset=x_offset,
        time_warning=time_warning,
    )


def export_global_lightcurve_plot(
    rows: Sequence[dict],
    path: str | Path,
    *,
    title: str | None = None,
    dpi: int = 160,
    figsize: tuple[float, float] = (9.2, 5.2),
    reduction_mode: str = "raw",
    time_axis_mode: str = "auto",
) -> LightcurvePlotResult:
    """Plot an M5 globally calibrated light curve.

    Rows are expected to be exact M5 points or plot-only time bins with
    ``mjd``, ``mag_global`` and ``series_id``. Error bars intentionally use
    only the random/bin component. ``mag_err_series_zp`` remains a correlated
    series-level uncertainty and is not drawn as if it were independent noise.

    ``time_axis_mode=auto`` preserves time inside observing blocks but shortens
    large empty gaps between non-overlapping series. Break marks make that
    non-linear time axis explicit; the scientific CSV and plot-data CSV retain
    the original MJD values.
    """

    rows_list = [dict(row) for row in rows]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    finite_rows = [
        row for row in rows_list
        if _is_finite(row.get("mjd")) and _is_finite(row.get("mag_global"))
    ]
    if not finite_rows:
        raise ValueError("No finite M5 plot points")

    time_axis_mode = str(time_axis_mode).lower()
    if time_axis_mode not in {"auto", "continuous", "compressed"}:
        raise ValueError("time_axis_mode must be one of: auto, continuous, compressed")
    axis = _build_global_time_axis(finite_rows, requested_mode=time_axis_mode)

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "matplotlib is required for M5 global light-curve plots. "
            "Install it with: python -m pip install matplotlib"
        ) from exc

    fig, ax = plt.subplots(figsize=figsize)
    grouped: dict[int, list[tuple[dict, float]]] = {}
    for row, plot_x in zip(finite_rows, axis["x_values"], strict=False):
        grouped.setdefault(int(row.get("series_id") or -1), []).append((row, float(plot_x)))
    archive_overview = len(grouped) > 12

    for series_id, group in sorted(grouped.items()):
        group.sort(key=lambda item: float(item[0]["mjd"]))
        xs = [plot_x for _row, plot_x in group]
        ys = [float(row["mag_global"]) for row, _plot_x in group]
        errors = [
            float(row["mag_err_random"]) if _is_finite(row.get("mag_err_random")) else None
            for row, _plot_x in group
        ]
        label = f"series {series_id}" if series_id >= 0 else "series unknown"
        style_kwargs = {"color": "C0", "alpha": 0.82} if archive_overview else {}
        if all(error is not None and error >= 0 for error in errors):
            ax.errorbar(
                xs,
                ys,
                yerr=[float(error) for error in errors],
                marker="o" if reduction_mode == "bin" else ".",
                linestyle="-" if reduction_mode == "bin" else "none",
                capsize=2 if reduction_mode == "bin" else 0,
                label="_nolegend_" if archive_overview else label,
                **style_kwargs,
            )
        else:
            ax.plot(
                xs,
                ys,
                marker="o" if reduction_mode == "bin" else ".",
                linestyle="-" if reduction_mode == "bin" else "none",
                label="_nolegend_" if archive_overview else label,
                **style_kwargs,
            )

        if reduction_mode == "bin":
            for x_value, (row, _plot_x) in zip(xs, group, strict=False):
                lo = row.get("mag_min")
                hi = row.get("mag_max")
                if _is_finite(lo) and _is_finite(hi) and float(hi) > float(lo):
                    vline_kwargs = {"color": "C0"} if archive_overview else {}
                    ax.vlines(
                        x_value, float(lo), float(hi), alpha=0.25, linewidth=0.8, **vline_kwargs
                    )

    if axis["mode"] == "compressed":
        break_positions = list(axis["break_positions"])
        dense_breaks = len(break_positions) > 8
        ax.set_xlabel(
            "MJD (large gaps compressed; dotted lines mark time breaks)"
            if dense_breaks
            else "MJD (large gaps compressed; // marks time breaks)"
        )
        if axis["tick_positions"]:
            ax.set_xticks(axis["tick_positions"])
            ax.set_xticklabels(axis["tick_labels"], fontsize=8)
        slash_positions = break_positions
        if dense_breaks:
            n_marks = min(6, len(break_positions))
            indexes = sorted({round(i * (len(break_positions) - 1) / max(1, n_marks - 1)) for i in range(n_marks)})
            slash_positions = [break_positions[index] for index in indexes]
        for break_x in break_positions:
            ax.axvline(float(break_x), linestyle=":", linewidth=0.8, alpha=0.18)
        for break_x in slash_positions:
            ax.text(
                float(break_x),
                0.006,
                "//",
                transform=ax.get_xaxis_transform(),
                ha="center",
                va="bottom",
                fontsize=10,
                clip_on=False,
            )
    else:
        ax.set_xlabel(f"MJD - {axis['x_offset']:.0f}")

    ax.set_ylabel("Global calibrated magnitude")
    ax.grid(True, alpha=0.3)
    ax.invert_yaxis()
    ax.set_title(title or "M5 global object light curve")
    if not archive_overview:
        ax.legend(fontsize=8, ncol=2 if len(grouped) > 6 else 1)
    note_x, note_y, note_va, note_ha = (0.99, 0.99, "top", "right") if axis["mode"] == "compressed" else (0.01, 0.01, "bottom", "left")
    ax.text(
        note_x,
        note_y,
        "Error bars: random/bin component only; M4 series ZP uncertainty is correlated.",
        transform=ax.transAxes,
        va=note_va,
        ha=note_ha,
        fontsize=8,
        alpha=0.75,
    )
    fig.tight_layout()
    fig.savefig(path, dpi=dpi)
    plt.close(fig)

    return LightcurvePlotResult(
        output_path=str(path),
        n_rows=len(rows_list),
        n_points=len(finite_rows),
        x_key="mjd_compressed" if axis["mode"] == "compressed" else "mjd_zero",
        y_key="mag_global",
        inverted_y=True,
        x_offset=float(axis["x_offset"]),
        time_warning=(
            "Large empty time gaps are compressed for display only."
            if axis["mode"] == "compressed" else None
        ),
        time_axis_mode=str(axis["mode"]),
        n_time_breaks=len(axis["break_positions"]),
        plot_style="archive_overview" if archive_overview else "series_detailed",
    )


def _build_global_time_axis(rows: Sequence[dict], *, requested_mode: str = "auto") -> dict[str, object]:
    """Return a display-only MJD transform with optional large-gap compression.

    Time differences *inside* each contiguous observing block are unchanged.
    Only empty gaps between non-overlapping series intervals can be shortened.
    The return value is intentionally simple so it can be regression-tested
    without Matplotlib.
    """

    if not rows:
        raise ValueError("No rows for M5 time-axis mapping")
    requested_mode = str(requested_mode).lower()
    if requested_mode not in {"auto", "continuous", "compressed"}:
        raise ValueError("requested_mode must be one of: auto, continuous, compressed")

    mjds = [float(row["mjd"]) for row in rows if _is_finite(row.get("mjd"))]
    if not mjds:
        raise ValueError("No finite MJD values for M5 time-axis mapping")
    x_offset = float(math.floor(min(mjds)))
    if requested_mode == "continuous":
        return {
            "mode": "continuous",
            "x_values": [float(row["mjd"]) - x_offset for row in rows],
            "x_offset": x_offset,
            "break_positions": [],
            "tick_positions": [],
            "tick_labels": [],
            "compressed_gap_minutes": None,
        }

    by_series: dict[int, list[float]] = {}
    for row in rows:
        if not _is_finite(row.get("mjd")):
            continue
        by_series.setdefault(int(row.get("series_id") or -1), []).append(float(row["mjd"]))
    intervals = [
        (min(values), max(values), series_id)
        for series_id, values in by_series.items()
        if values
    ]
    intervals.sort(key=lambda item: (item[0], item[1], item[2]))
    if len(intervals) <= 1:
        return {
            "mode": "continuous",
            "x_values": [float(row["mjd"]) - x_offset for row in rows],
            "x_offset": x_offset,
            "break_positions": [],
            "tick_positions": [],
            "tick_labels": [],
            "compressed_gap_minutes": None,
        }

    positive_spans = [end - start for start, end, _sid in intervals if end > start]
    typical_span = median(positive_spans) if positive_spans else 10.0 / 1440.0
    target_gap = min(max(float(typical_span) * 0.12, 2.0 / 1440.0), 30.0 / 1440.0)
    auto_threshold = max(target_gap * 1.5, 10.0 / 1440.0)

    breaks: list[dict[str, float]] = []
    cumulative_removed = 0.0
    current_end = float(intervals[0][1])
    for start, end, _series_id in intervals[1:]:
        start = float(start)
        end = float(end)
        gap = start - current_end
        threshold = target_gap if requested_mode == "compressed" else auto_threshold
        if gap > threshold:
            display_gap = min(gap, target_gap)
            removed = gap - display_gap
            left_display = current_end - cumulative_removed - x_offset
            cumulative_removed += removed
            right_display = start - cumulative_removed - x_offset
            breaks.append({
                "actual_right_start": start,
                "removed": removed,
                "cumulative_removed": cumulative_removed,
                "display_mid": (left_display + right_display) / 2.0,
            })
        current_end = max(current_end, end)

    if not breaks:
        return {
            "mode": "continuous",
            "x_values": [float(row["mjd"]) - x_offset for row in rows],
            "x_offset": x_offset,
            "break_positions": [],
            "tick_positions": [],
            "tick_labels": [],
            "compressed_gap_minutes": target_gap * 1440.0,
        }

    def map_mjd(value: float) -> float:
        removed = 0.0
        for item in breaks:
            if value >= float(item["actual_right_start"]):
                removed = float(item["cumulative_removed"])
            else:
                break
        return value - removed - x_offset

    x_values = [map_mjd(float(row["mjd"])) for row in rows]

    # One label per series is useful for small/medium overviews. For very many
    # series keep at most ten evenly distributed labels to avoid a black axis.
    selected = intervals
    max_ticks = 6 if len(intervals) > 12 else 10
    if len(selected) > max_ticks:
        idxs = sorted({
            round(i * (len(selected) - 1) / max(1, max_ticks - 1))
            for i in range(max_ticks)
        })
        selected = [selected[index] for index in idxs]
    show_series = len(intervals) <= 12
    tick_positions: list[float] = []
    tick_labels: list[str] = []
    for start, end, series_id in selected:
        midpoint = (float(start) + float(end)) / 2.0
        tick_positions.append(map_mjd(midpoint))
        label = f"{midpoint:.2f}"
        if show_series:
            label += f"\ns{series_id}"
        tick_labels.append(label)

    return {
        "mode": "compressed",
        "x_values": x_values,
        "x_offset": x_offset,
        "break_positions": [float(item["display_mid"]) for item in breaks],
        "tick_positions": tick_positions,
        "tick_labels": tick_labels,
        "compressed_gap_minutes": target_gap * 1440.0,
    }


def _build_x_values(
    rows: Sequence[LightcurveRow],
    x_key: str,
    *,
    observatory_name: str | None,
    observatory_lat_deg: float | None,
    observatory_lon_deg: float | None,
    observatory_height_m: float | None,
) -> tuple[list[float | None], str | None, float | None]:
    if x_key == "mjd":
        return [_row_value(row, "mjd") for row in rows], None, None
    if x_key == "mjd_zero":
        mjds = [_row_value(row, "mjd") for row in rows]
        values, offset = _zeroed_values(mjds)
        return values, None, offset
    if x_key == "mjd_frac":
        return [_fractional_value(_row_value(row, "mjd")) for row in rows], None, None
    if x_key in {"bjd_tdb", "bjd_tdb_zero", "bjd_tdb_frac", "bmjd_tdb", "bmjd_tdb_zero", "bmjd_tdb_frac"}:
        use_mjd = x_key.startswith("bmjd_")
        times = _compute_bjd_tdb_values(
            rows,
            as_mjd=use_mjd,
            observatory_name=observatory_name,
            observatory_lat_deg=observatory_lat_deg,
            observatory_lon_deg=observatory_lon_deg,
            observatory_height_m=observatory_height_m,
        )
        if x_key in {"bjd_tdb_zero", "bmjd_tdb_zero"}:
            values, offset = _zeroed_values(times)
            return values, None, offset
        if x_key in {"bjd_tdb_frac", "bmjd_tdb_frac"}:
            return [_fractional_value(value) for value in times], None, None
        return times, None, None
    if x_key in {"frame_id", "measurement_id"}:
        return [_row_value(row, x_key) for row in rows], None, None
    raise ValueError(f"Unsupported light-curve plot x key: {x_key}")


def _compute_bjd_tdb_values(
    rows: Sequence[LightcurveRow],
    *,
    as_mjd: bool,
    observatory_name: str | None,
    observatory_lat_deg: float | None,
    observatory_lon_deg: float | None,
    observatory_height_m: float | None,
) -> list[float | None]:
    try:
        from astropy import units as u
        from astropy.coordinates import EarthLocation, SkyCoord
        from astropy.time import Time
    except Exception as exc:  # pragma: no cover - depends on optional dependency
        raise RuntimeError("astropy is required to compute BJD_TDB") from exc

    location = _resolve_observatory_location(
        observatory_name=observatory_name,
        observatory_lat_deg=observatory_lat_deg,
        observatory_lon_deg=observatory_lon_deg,
        observatory_height_m=observatory_height_m,
    )
    earth_location = EarthLocation.from_geodetic(
        lon=location.lon_deg * u.deg,
        lat=location.lat_deg * u.deg,
        height=location.height_m * u.m,
    )

    out: list[float | None] = []
    for row in rows:
        if not (_is_finite(row.mjd) and _is_finite(row.ra_deg) and _is_finite(row.dec_deg)):
            out.append(None)
            continue
        t_utc = Time(float(row.mjd), format="mjd", scale="utc", location=earth_location)
        coord = SkyCoord(float(row.ra_deg) * u.deg, float(row.dec_deg) * u.deg, frame="icrs")
        ltt = t_utc.light_travel_time(coord, kind="barycentric")
        t_bary = t_utc.tdb + ltt
        out.append(float(t_bary.mjd if as_mjd else t_bary.jd))
    return out


def _resolve_observatory_location(
    *,
    observatory_name: str | None,
    observatory_lat_deg: float | None,
    observatory_lon_deg: float | None,
    observatory_height_m: float | None,
) -> ObservatoryLocation:
    if observatory_lat_deg is not None or observatory_lon_deg is not None or observatory_height_m is not None:
        if observatory_lat_deg is None or observatory_lon_deg is None:
            raise ValueError("Both --observatory-lat-deg and --observatory-lon-deg are required for custom BJD_TDB")
        return ObservatoryLocation(
            name="custom",
            lat_deg=float(observatory_lat_deg),
            lon_deg=float(observatory_lon_deg),
            height_m=0.0 if observatory_height_m is None else float(observatory_height_m),
        )
    key = (observatory_name or "KAO").strip().upper()
    if key in KNOWN_OBSERVATORIES:
        return KNOWN_OBSERVATORIES[key]
    known = ", ".join(sorted(KNOWN_OBSERVATORIES))
    raise ValueError(
        f"Unknown observatory {observatory_name!r}; use one of {known} or pass custom lat/lon/height"
    )


def _zeroed_values(values: Sequence[float | None]) -> tuple[list[float | None], float | None]:
    finite = [float(v) for v in values if _is_finite(v)]
    if not finite:
        return [None for _ in values], None
    offset = math.floor(min(finite))
    return [(float(v) - offset) if _is_finite(v) else None for v in values], float(offset)


def _fractional_value(value) -> float | None:
    if not _is_finite(value):
        return None
    v = float(value)
    return v - math.floor(v)


def _row_value(row: LightcurveRow, key: str):
    if key == "frame_id":
        return row.frame_id
    if key == "measurement_id":
        return row.measurement_id
    if key == "mjd":
        return row.mjd
    if key == "flux":
        return row.flux
    if key == "flux_err":
        return row.flux_err
    if key == "mag_inst":
        return row.mag_inst
    if key == "mag_cal":
        return row.mag_cal
    if key == "mag_err":
        return row.mag_err
    if key == "local_zp_correction":
        return row.local_zp_correction
    if key == "x":
        return row.x
    if key == "y":
        return row.y
    if key == "ra_deg":
        return row.ra_deg
    if key == "dec_deg":
        return row.dec_deg
    raise ValueError(f"Unsupported light-curve plot key: {key}")


def _error_for_y(row: LightcurveRow, y_key: str) -> float | None:
    if y_key in {"mag_cal", "mag_inst"}:
        return row.mag_err
    if y_key == "flux":
        return row.flux_err
    return None


def _is_finite(value) -> bool:
    try:
        return math.isfinite(float(value))
    except Exception:
        return False


def _axis_label_for_x(key: str, offset: float | None) -> str:
    if key == "mjd_zero":
        return f"MJD - {offset:.0f}" if offset is not None else "MJD - offset"
    if key == "bjd_tdb_zero":
        return f"BJD_TDB - {offset:.0f}" if offset is not None else "BJD_TDB - offset"
    if key == "bmjd_tdb_zero":
        return f"BMJD_TDB - {offset:.0f}" if offset is not None else "BMJD_TDB - offset"
    labels = {
        "mjd": "MJD (UTC)",
        "mjd_frac": "MJD fractional part",
        "bjd_tdb": "BJD_TDB",
        "bjd_tdb_frac": "BJD_TDB fractional part",
        "bmjd_tdb": "BMJD_TDB",
        "bmjd_tdb_frac": "BMJD_TDB fractional part",
        "frame_id": "Frame ID",
        "measurement_id": "Measurement ID",
    }
    return labels.get(key, key)


def _axis_label(key: str) -> str:
    labels = {
        "mjd": "MJD (UTC)",
        "mjd_zero": "MJD - offset",
        "mjd_frac": "MJD fractional part",
        "bjd_tdb": "BJD_TDB",
        "bjd_tdb_zero": "BJD_TDB - offset",
        "bjd_tdb_frac": "BJD_TDB fractional part",
        "frame_id": "Frame ID",
        "measurement_id": "Measurement ID",
        "mag_cal": "Calibrated magnitude",
        "mag_inst": "Instrumental magnitude",
        "mag_err": "Magnitude error",
        "flux": "Flux",
        "flux_err": "Flux error",
        "local_zp_correction": "Frame zero point",
        "x": "X pixel",
        "y": "Y pixel",
    }
    return labels.get(key, key)


def _default_title(rows: Sequence[LightcurveRow]) -> str:
    source_id = _first_nonempty(getattr(r, "source_id", None) for r in rows)
    catalog_name = _first_nonempty(getattr(r, "catalog_name", None) for r in rows)
    calibration_name = _first_nonempty(getattr(r, "calibration_name", None) for r in rows)
    filter_name = _first_nonempty(getattr(r, "filter_name", None) for r in rows)
    object_id = _first_nonempty(str(getattr(r, "object_id", "")) for r in rows)
    label = source_id or (f"object_id={object_id}" if object_id else "light curve")
    parts = []
    if catalog_name:
        parts.append(catalog_name)
    parts.append(str(label))
    if filter_name:
        parts.append(f"filter={filter_name}")
    if calibration_name:
        parts.append(calibration_name)
    return " / ".join(parts)


def _stats_text(values: Sequence[float], y_key: str) -> str:
    if not values:
        return "N = 0"
    n = len(values)
    mean = sum(values) / n
    median = _median(values)
    rms = math.sqrt(sum((v - mean) ** 2 for v in values) / n) if n > 0 else float("nan")
    mad = _median([abs(v - median) for v in values]) if n > 0 else float("nan")
    return "\n".join(
        [
            f"N = {n}",
            f"mean = {mean:.5g}",
            f"median = {median:.5g}",
            f"rms = {rms:.4g}",
            f"MAD = {mad:.4g}",
        ]
    )


def _median(values: Sequence[float]) -> float:
    if not values:
        return float("nan")
    xs = sorted(float(v) for v in values)
    n = len(xs)
    mid = n // 2
    if n % 2:
        return xs[mid]
    return 0.5 * (xs[mid - 1] + xs[mid])


def _first_nonempty(values) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None
