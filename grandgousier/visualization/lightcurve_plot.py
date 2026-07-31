from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

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
