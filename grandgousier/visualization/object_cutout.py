from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from grandgousier.db.models import (
    CalibrationVersion,
    CatalogSource,
    DetectionCatalogMatch,
    Frame,
    InstrumentUnit,
    Measurement,
    Object,
    SourceDetection,
)


@dataclass
class ObjectFrameRow:
    measurement_id: int
    frame_id: int
    object_id: int
    source_id: str | None
    catalog_name: str | None
    calibration_name: str | None
    instrument_unit_id: int | None
    instrument: str | None
    filter_name: str | None
    date_obs: object | None
    mjd: float | None
    x: float | None
    y: float | None
    flux: float | None
    flux_err: float | None
    mag_inst: float | None
    mag_cal: float | None
    mag_err: float | None
    local_zp_correction: float | None
    raw_path: str | None
    det_sep_arcsec: float | None = None
    det_snr: float | None = None
    det_x: float | None = None
    det_y: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "measurement_id": self.measurement_id,
            "frame_id": self.frame_id,
            "object_id": self.object_id,
            "source_id": self.source_id,
            "catalog_name": self.catalog_name,
            "calibration_name": self.calibration_name,
            "instrument_unit_id": self.instrument_unit_id,
            "instrument": self.instrument,
            "filter_name": self.filter_name,
            "date_obs": self.date_obs.isoformat() if hasattr(self.date_obs, "isoformat") else self.date_obs,
            "mjd": self.mjd,
            "x": self.x,
            "y": self.y,
            "flux": self.flux,
            "flux_err": self.flux_err,
            "mag_inst": self.mag_inst,
            "mag_cal": self.mag_cal,
            "mag_err": self.mag_err,
            "local_zp_correction": self.local_zp_correction,
            "det_sep_arcsec": self.det_sep_arcsec,
            "det_snr": self.det_snr,
            "det_x": self.det_x,
            "det_y": self.det_y,
            "raw_path": self.raw_path,
        }


def find_object_frames(
    session: Session,
    *,
    calibration_name: str,
    catalog_name: str,
    source_id: str | None = None,
    object_id: int | None = None,
    instrument_unit_id: int | None = None,
    sort: str = "time",
    limit: int | None = None,
) -> list[ObjectFrameRow]:
    rows = _load_object_frame_rows(
        session,
        calibration_name=calibration_name,
        catalog_name=catalog_name,
        source_id=source_id,
        object_id=object_id,
        instrument_unit_id=instrument_unit_id,
    )

    if sort == "time":
        rows.sort(key=lambda r: (r.mjd is None, r.mjd or 0.0, r.frame_id))
    elif sort == "brightest":
        rows.sort(key=lambda r: (r.mag_cal is None, r.mag_cal if r.mag_cal is not None else 999.0))
    elif sort == "faintest":
        rows.sort(key=lambda r: (r.mag_cal is None, -(r.mag_cal if r.mag_cal is not None else -999.0)))
    elif sort == "frame_id":
        rows.sort(key=lambda r: r.frame_id)
    else:
        raise ValueError(f"Unknown sort mode: {sort}")

    if limit is not None:
        rows = rows[: int(limit)]
    return rows


def export_object_cutout(
    session: Session,
    *,
    calibration_name: str,
    catalog_name: str,
    source_id: str | None = None,
    object_id: int | None = None,
    frame_id: int,
    out: str | Path,
    size_pix: int = 80,
    show_detections: bool = True,
    aperture: bool = True,
    vmin_percentile: float = 1.0,
    vmax_percentile: float = 99.5,
    cmap: str = "gray",
    dpi: int = 140,
) -> dict[str, Any]:
    """Export a FITS cutout around an object's forced-photometry position."""

    row = _select_frame_row(
        session,
        calibration_name=calibration_name,
        catalog_name=catalog_name,
        source_id=source_id,
        object_id=object_id,
        frame_id=frame_id,
    )
    if row.raw_path is None:
        raise ValueError(f"Frame {frame_id} has no raw_path")
    raw_path = Path(row.raw_path)
    if not raw_path.exists():
        raise FileNotFoundError(f"FITS file not found: {raw_path}")
    if row.x is None or row.y is None:
        raise ValueError(f"Object has no measurement x/y on frame {frame_id}")

    from astropy.io import fits
    import matplotlib.pyplot as plt
    from matplotlib.patches import Circle
    import numpy as np

    data = fits.getdata(raw_path)
    data = np.asarray(data, dtype=float)
    if data.ndim > 2:
        data = np.squeeze(data)
    if data.ndim != 2:
        raise ValueError(f"Expected 2D FITS image, got shape={data.shape}")

    ny, nx = data.shape
    half = max(2, int(size_pix) // 2)
    cx = float(row.x)
    cy = float(row.y)
    x0 = max(0, int(round(cx)) - half)
    x1 = min(nx, int(round(cx)) + half + 1)
    y0 = max(0, int(round(cy)) - half)
    y1 = min(ny, int(round(cy)) + half + 1)
    cut = data[y0:y1, x0:x1]

    finite = cut[np.isfinite(cut)]
    if finite.size:
        vmin = float(np.nanpercentile(finite, float(vmin_percentile)))
        vmax = float(np.nanpercentile(finite, float(vmax_percentile)))
        if not np.isfinite(vmin) or not np.isfinite(vmax) or vmax <= vmin:
            vmin = None
            vmax = None
    else:
        vmin = None
        vmax = None

    detections = []
    matched_detection_id = None
    if show_detections:
        detections = _load_frame_detections(session, frame_id=frame_id, x0=x0, x1=x1, y0=y0, y1=y1)
        matched_detection_id = _load_matched_detection_id(
            session,
            catalog_name=catalog_name,
            source_id=source_id,
            frame_id=frame_id,
        )

    fig, ax = plt.subplots(figsize=(6.5, 6.0), dpi=dpi)
    ax.imshow(cut, origin="lower", cmap=cmap, vmin=vmin, vmax=vmax, interpolation="nearest")

    tx = cx - x0
    ty = cy - y0
    ax.plot([tx], [ty], marker="+", markersize=14, markeredgewidth=2, linestyle="None", label="forced aperture / catalog position")
    if aperture:
        radius = _load_aperture_radius(session, measurement_id=row.measurement_id)
        if radius is not None:
            ax.add_patch(Circle((tx, ty), radius=radius, fill=False, linewidth=1.5, label=f"aperture r={radius:.1f}px"))

    if show_detections and detections:
        xs = [d.x - x0 for d in detections]
        ys = [d.y - y0 for d in detections]
        ax.scatter(xs, ys, s=24, facecolors="none", linewidths=1.0, label="detections")
        if matched_detection_id is not None:
            for d in detections:
                if d.source_detection_id == matched_detection_id:
                    ax.plot([d.x - x0], [d.y - y0], marker="x", markersize=11, markeredgewidth=2, linestyle="None", label="matched detection")
                    break

    title_parts = [
        f"frame {row.frame_id}",
        f"object {row.object_id}",
    ]
    if row.source_id:
        title_parts.append(str(row.source_id))
    if row.mag_cal is not None:
        title_parts.append(f"mag={row.mag_cal:.3f}")
    ax.set_title(" / ".join(title_parts), fontsize=10)
    ax.set_xlabel(f"x pixel, cutout x0={x0}")
    ax.set_ylabel(f"y pixel, cutout y0={y0}")
    ax.legend(loc="upper right", fontsize=7, framealpha=0.85)
    fig.tight_layout()

    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path)
    plt.close(fig)

    return {
        "path": str(out_path),
        "frame_id": row.frame_id,
        "measurement_id": row.measurement_id,
        "object_id": row.object_id,
        "source_id": row.source_id,
        "raw_path": row.raw_path,
        "x": row.x,
        "y": row.y,
        "x0": x0,
        "x1": x1,
        "y0": y0,
        "y1": y1,
        "detections_plotted": len(detections),
        "matched_detection_id": matched_detection_id,
    }


def _load_object_frame_rows(
    session: Session,
    *,
    calibration_name: str,
    catalog_name: str,
    source_id: str | None,
    object_id: int | None,
    instrument_unit_id: int | None,
) -> list[ObjectFrameRow]:
    stmt = (
        select(Measurement, Frame, Object, CalibrationVersion, InstrumentUnit)
        .join(Frame, Frame.frame_id == Measurement.frame_id)
        .join(Object, Object.object_id == Measurement.object_id)
        .join(CalibrationVersion, CalibrationVersion.calibration_version_id == Measurement.calibration_version_id)
        .outerjoin(InstrumentUnit, InstrumentUnit.instrument_unit_id == Frame.instrument_unit_id)
        .where(CalibrationVersion.name == calibration_name)
        .order_by(Frame.mjd, Frame.frame_id)
    )
    if object_id is not None:
        stmt = stmt.where(Measurement.object_id == object_id)
    if instrument_unit_id is not None:
        stmt = stmt.where(Frame.instrument_unit_id == instrument_unit_id)

    det_by_frame = _load_detection_match_map(session, catalog_name=catalog_name, source_id=source_id)

    rows: list[ObjectFrameRow] = []
    for meas, frame, obj, cv, iu in session.execute(stmt).all():
        flags = meas.flags or {}
        if flags.get("catalog_name") != catalog_name:
            continue
        if source_id is not None and str(flags.get("source_id")) != str(source_id):
            continue
        det_info = det_by_frame.get(frame.frame_id, {})
        rows.append(
            ObjectFrameRow(
                measurement_id=meas.measurement_id,
                frame_id=frame.frame_id,
                object_id=obj.object_id,
                source_id=str(flags.get("source_id")) if flags.get("source_id") is not None else None,
                catalog_name=flags.get("catalog_name"),
                calibration_name=cv.name,
                instrument_unit_id=getattr(iu, "instrument_unit_id", None),
                instrument=getattr(iu, "instrument", None),
                filter_name=getattr(iu, "filter_name", None),
                date_obs=frame.date_obs,
                mjd=frame.mjd,
                x=meas.x,
                y=meas.y,
                flux=meas.flux,
                flux_err=meas.flux_err,
                mag_inst=meas.mag_inst,
                mag_cal=meas.mag_cal,
                mag_err=meas.mag_err,
                local_zp_correction=meas.local_zp_correction,
                raw_path=frame.raw_path,
                det_sep_arcsec=det_info.get("det_sep_arcsec"),
                det_snr=det_info.get("det_snr"),
                det_x=det_info.get("det_x"),
                det_y=det_info.get("det_y"),
            )
        )
    return rows


def _select_frame_row(
    session: Session,
    *,
    calibration_name: str,
    catalog_name: str,
    source_id: str | None,
    object_id: int | None,
    frame_id: int,
) -> ObjectFrameRow:
    rows = _load_object_frame_rows(
        session,
        calibration_name=calibration_name,
        catalog_name=catalog_name,
        source_id=source_id,
        object_id=object_id,
        instrument_unit_id=None,
    )
    for row in rows:
        if int(row.frame_id) == int(frame_id):
            return row
    raise ValueError(f"Object not found on frame_id={frame_id}")


def _load_detection_match_map(
    session: Session,
    *,
    catalog_name: str,
    source_id: str | None,
) -> dict[int, dict[str, float]]:
    if source_id is None:
        return {}
    catalog_source = session.scalar(
        select(CatalogSource).where(
            CatalogSource.catalog_name == catalog_name,
            CatalogSource.source_id == str(source_id),
        )
    )
    if catalog_source is None:
        return {}

    stmt = (
        select(DetectionCatalogMatch, SourceDetection)
        .join(SourceDetection, SourceDetection.source_detection_id == DetectionCatalogMatch.source_detection_id)
        .where(
            DetectionCatalogMatch.catalog_source_id == catalog_source.catalog_source_id,
            DetectionCatalogMatch.catalog_name == catalog_name,
            DetectionCatalogMatch.match_status == "MATCHED",
        )
    )
    out: dict[int, dict[str, float]] = {}
    for match, det in session.execute(stmt).all():
        out[int(match.frame_id)] = {
            "det_sep_arcsec": match.sep_arcsec,
            "det_snr": det.snr,
            "det_x": det.x,
            "det_y": det.y,
        }
    return out


def _load_frame_detections(
    session: Session,
    *,
    frame_id: int,
    x0: int,
    x1: int,
    y0: int,
    y1: int,
) -> list[SourceDetection]:
    stmt = (
        select(SourceDetection)
        .where(
            SourceDetection.frame_id == frame_id,
            SourceDetection.x >= float(x0),
            SourceDetection.x <= float(x1),
            SourceDetection.y >= float(y0),
            SourceDetection.y <= float(y1),
        )
        .order_by(SourceDetection.snr.desc().nullslast())
    )
    return list(session.scalars(stmt).all())


def _load_matched_detection_id(
    session: Session,
    *,
    catalog_name: str,
    source_id: str | None,
    frame_id: int,
) -> int | None:
    if source_id is None:
        return None
    catalog_source = session.scalar(
        select(CatalogSource).where(
            CatalogSource.catalog_name == catalog_name,
            CatalogSource.source_id == str(source_id),
        )
    )
    if catalog_source is None:
        return None
    match = session.scalar(
        select(DetectionCatalogMatch).where(
            DetectionCatalogMatch.catalog_source_id == catalog_source.catalog_source_id,
            DetectionCatalogMatch.catalog_name == catalog_name,
            DetectionCatalogMatch.frame_id == frame_id,
            DetectionCatalogMatch.match_status == "MATCHED",
        )
    )
    return int(match.source_detection_id) if match is not None else None


def _load_aperture_radius(session: Session, *, measurement_id: int) -> float | None:
    measurement = session.scalar(select(Measurement).where(Measurement.measurement_id == measurement_id))
    if measurement is None or measurement.aperture_radius_pix is None:
        return None
    return float(measurement.aperture_radius_pix)
