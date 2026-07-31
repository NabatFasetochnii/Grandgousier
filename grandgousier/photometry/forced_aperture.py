from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
from astropy.io import fits
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tqdm import tqdm

from grandgousier.db.bulk import DEFAULT_BULK_INSERT_BATCH_SIZE
from grandgousier.db.measurement_io import upsert_measurement_rows
from grandgousier.db.models import (
    CalibrationVersion,
    CatalogSource,
    Frame,
    FrameCatalogLink,
    FrameStatus,
    InstrumentUnit,
    Measurement,
    Object,
)


@dataclass
class ForcedPhotometryResult:
    selected_frames: int = 0
    checked_frames: int = 0
    selected_links: int = 0
    measured: int = 0
    inserted_or_updated: int = 0
    insert_batches: int = 0
    insert_batch_size: int = DEFAULT_BULK_INSERT_BATCH_SIZE
    reused_frames: int = 0
    reused_measurements: int = 0
    skipped_missing_file: int = 0
    failed_frames: int = 0
    failed_measurements: int = 0
    calibration_version_id: int | None = None
    calibration_name: str | None = None
    dry_run: bool = False


@dataclass(frozen=True)
class ApertureMeasurement:
    flux: float | None
    flux_err: float | None
    mag_inst: float | None
    mag_err: float | None
    local_background: float | None
    aperture_radius_pix: float
    flags: dict


def run_forced_aperture_photometry(
    session: Session,
    *,
    catalog_name: str | None = None,
    frame_ids: list[int] | None = None,
    instrument: str | None = None,
    instrument_unit_id: int | None = None,
    only_wcs_ok: bool = True,
    aperture_radius_pix: float = 3.0,
    annulus_inner_pix: float = 8.0,
    annulus_outer_pix: float = 12.0,
    zero_point: float = 0.0,
    saturation_level_adu: float | None = None,
    calibration_name: str | None = None,
    limit_frames: int | None = None,
    limit_links: int | None = None,
    update_processing_status: bool = False,
    reuse_existing: bool = True,
    force_recompute: bool = False,
    dry_run: bool = False,
    insert_batch_size: int = DEFAULT_BULK_INSERT_BATCH_SIZE,
) -> ForcedPhotometryResult:
    """Run minimal forced circular-aperture photometry for catalog-frame links.

    This MVP stage intentionally avoids photutils. It uses only NumPy + Astropy FITS:
    circular aperture sum, local annulus median background, and instrumental magnitude.
    Coordinates are taken from frame_catalog_links.x/y and are interpreted as 0-based
    FITS array pixel coordinates produced by astropy.wcs.world_to_pixel_values().
    """

    if aperture_radius_pix <= 0:
        raise ValueError("aperture_radius_pix must be positive")
    if annulus_inner_pix <= aperture_radius_pix:
        raise ValueError("annulus_inner_pix must be greater than aperture_radius_pix")
    if annulus_outer_pix <= annulus_inner_pix:
        raise ValueError("annulus_outer_pix must be greater than annulus_inner_pix")

    result = ForcedPhotometryResult(dry_run=dry_run, insert_batch_size=int(insert_batch_size))
    if calibration_name is None:
        calibration_name = (
            f"raw_aperture_r{_fmt_float(aperture_radius_pix)}_"
            f"ann{_fmt_float(annulus_inner_pix)}_{_fmt_float(annulus_outer_pix)}_v1"
        )
    result.calibration_name = calibration_name

    frames = _select_frames(
        session,
        frame_ids=frame_ids,
        instrument=instrument,
        instrument_unit_id=instrument_unit_id,
        only_wcs_ok=only_wcs_ok,
        limit=limit_frames,
    )
    result.selected_frames = len(frames)

    if not dry_run:
        calibration_version = _get_or_create_calibration_version(
            session,
            name=calibration_name,
            instrument_unit_id=instrument_unit_id,
            parameters={
                "aperture_radius_pix": aperture_radius_pix,
                "annulus_inner_pix": annulus_inner_pix,
                "annulus_outer_pix": annulus_outer_pix,
                "zero_point": zero_point,
                "saturation_level_adu": saturation_level_adu,
                "catalog_name": catalog_name,
                "reuse_existing": reuse_existing,
                "force_recompute": force_recompute,
            },
        )
        session.flush()
        result.calibration_version_id = calibration_version.calibration_version_id
    else:
        result.calibration_version_id = None

    for frame in tqdm(frames, desc="Forced aperture photometry", unit="frame"):
        result.checked_frames += 1
        path = Path(frame.raw_path)
        if not path.exists():
            result.skipped_missing_file += 1
            continue

        links = _select_frame_links(
            session,
            frame_id=frame.frame_id,
            catalog_name=catalog_name,
            limit=limit_links,
        )
        if not links:
            continue
        result.selected_links += len(links)

        if (not dry_run) and reuse_existing and not force_recompute and result.calibration_version_id is not None:
            existing_n = _existing_measurement_count(
                session,
                frame_id=int(frame.frame_id),
                calibration_version_id=int(result.calibration_version_id),
            )
            if existing_n >= len(links):
                result.reused_frames += 1
                result.reused_measurements += int(existing_n)
                if update_processing_status:
                    frame.processing_status = FrameStatus.PHOTOMETRY_DONE
                continue

        try:
            with fits.open(path, memmap=False) as hdul:
                data = np.asarray(hdul[0].data, dtype=float)
                header = hdul[0].header
            if data.ndim != 2:
                result.failed_frames += 1
                continue
        except Exception:
            result.failed_frames += 1
            continue

        frame_saturation = _resolve_saturation_level(
            explicit=saturation_level_adu,
            frame=frame,
            header=header,
        )
        exptime = _safe_positive_float(frame.exptime)

        rows = []
        object_cache: dict[int, int] = {}
        for link, source in links:
            try:
                object_id = object_cache.get(source.catalog_source_id)
                if object_id is None:
                    object_id = _get_or_create_object_id(session, source, dry_run=dry_run)
                    if object_id is not None:
                        object_cache[source.catalog_source_id] = object_id
                measurement = measure_one_source(
                    data,
                    x=float(link.x),
                    y=float(link.y),
                    aperture_radius_pix=aperture_radius_pix,
                    annulus_inner_pix=annulus_inner_pix,
                    annulus_outer_pix=annulus_outer_pix,
                    exptime=exptime,
                    zero_point=zero_point,
                    saturation_level_adu=frame_saturation,
                )
                result.measured += 1
                if dry_run or object_id is None:
                    continue
                rows.append(
                    {
                        "object_id": object_id,
                        "frame_id": frame.frame_id,
                        "calibration_version_id": result.calibration_version_id,
                        "x": float(link.x),
                        "y": float(link.y),
                        "flux": measurement.flux,
                        "flux_err": measurement.flux_err,
                        "mag_inst": measurement.mag_inst,
                        "mag_cal": None,
                        "mag_err": measurement.mag_err,
                        "aperture_radius_pix": measurement.aperture_radius_pix,
                        "local_background": measurement.local_background,
                        "local_zp_correction": None,
                        "centroid_dx_pix": None,
                        "centroid_dy_pix": None,
                        "flags": {
                            **measurement.flags,
                            "catalog_name": source.catalog_name,
                            "catalog_source_id": source.catalog_source_id,
                            "source_id": source.source_id,
                            "annulus_inner_pix": annulus_inner_pix,
                            "annulus_outer_pix": annulus_outer_pix,
                            "zero_point": zero_point,
                            "photometry_stage": "forced_aperture_mvp",
                        },
                    }
                )
            except Exception as exc:
                result.failed_measurements += 1
                # Store nothing in this MVP path; detailed per-source failures can be added later.
                continue

        if rows:
            result.insert_batches += upsert_measurement_rows(session, rows, insert_batch_size=insert_batch_size)
            result.inserted_or_updated += len(rows)

        if update_processing_status and not dry_run:
            frame.processing_status = FrameStatus.PHOTOMETRY_DONE

    if not dry_run:
        session.commit()
    return result


def measure_one_source(
    data: np.ndarray,
    *,
    x: float,
    y: float,
    aperture_radius_pix: float,
    annulus_inner_pix: float,
    annulus_outer_pix: float,
    exptime: float | None,
    zero_point: float,
    saturation_level_adu: float | None,
) -> ApertureMeasurement:
    flags: dict[str, object] = {}
    ny, nx = data.shape
    if not math.isfinite(x) or not math.isfinite(y):
        flags["bad_xy"] = True
        return _empty_measurement(aperture_radius_pix, flags)

    # Use a cutout covering the outer annulus. This keeps the MVP fast enough even
    # for many sources per frame and avoids allocating full-frame distance arrays.
    pad = int(math.ceil(annulus_outer_pix + 1.0))
    x0 = max(0, int(math.floor(x)) - pad)
    x1 = min(nx, int(math.floor(x)) + pad + 1)
    y0 = max(0, int(math.floor(y)) - pad)
    y1 = min(ny, int(math.floor(y)) + pad + 1)

    if x0 >= x1 or y0 >= y1:
        flags["outside_image"] = True
        return _empty_measurement(aperture_radius_pix, flags)

    if x - aperture_radius_pix < 0 or x + aperture_radius_pix > nx - 1:
        flags["aperture_touches_x_edge"] = True
    if y - aperture_radius_pix < 0 or y + aperture_radius_pix > ny - 1:
        flags["aperture_touches_y_edge"] = True
    if x - annulus_outer_pix < 0 or x + annulus_outer_pix > nx - 1:
        flags["annulus_touches_x_edge"] = True
    if y - annulus_outer_pix < 0 or y + annulus_outer_pix > ny - 1:
        flags["annulus_touches_y_edge"] = True

    cutout = data[y0:y1, x0:x1]
    yy, xx = np.indices(cutout.shape, dtype=float)
    xx = xx + x0
    yy = yy + y0
    rr2 = (xx - x) ** 2 + (yy - y) ** 2

    aperture_mask = rr2 <= aperture_radius_pix**2
    annulus_mask = (rr2 >= annulus_inner_pix**2) & (rr2 <= annulus_outer_pix**2)

    aperture_values = cutout[aperture_mask]
    annulus_values = cutout[annulus_mask]
    aperture_values = aperture_values[np.isfinite(aperture_values)]
    annulus_values = annulus_values[np.isfinite(annulus_values)]

    if aperture_values.size == 0:
        flags["empty_aperture"] = True
        return _empty_measurement(aperture_radius_pix, flags)
    if annulus_values.size < 5:
        flags["too_few_annulus_pixels"] = True
        local_background = float(np.nanmedian(cutout)) if np.isfinite(cutout).any() else 0.0
        annulus_std = float(np.nanstd(cutout)) if np.isfinite(cutout).any() else 0.0
    else:
        local_background = float(np.nanmedian(annulus_values))
        annulus_std = float(1.4826 * np.nanmedian(np.abs(annulus_values - local_background)))
        if not math.isfinite(annulus_std) or annulus_std <= 0:
            annulus_std = float(np.nanstd(annulus_values))

    aperture_sum = float(np.nansum(aperture_values))
    n_ap = int(aperture_values.size)
    flux = aperture_sum - local_background * n_ap

    if saturation_level_adu is not None and np.nanmax(aperture_values) >= saturation_level_adu:
        flags["saturated_or_near_saturated"] = True
        flags["aperture_max_adu"] = float(np.nanmax(aperture_values))

    # Error is deliberately conservative for MVP: background scatter + Poisson-like
    # term for positive net flux. A later stage should use gain/read-noise from frame metadata.
    variance = max(0.0, flux) + n_ap * max(0.0, annulus_std) ** 2
    flux_err = float(math.sqrt(variance)) if variance > 0 else None

    mag_inst = None
    mag_err = None
    if flux > 0:
        rate = flux / exptime if exptime and exptime > 0 else flux
        if rate > 0:
            mag_inst = float(-2.5 * math.log10(rate) + zero_point)
            if flux_err is not None and flux_err > 0:
                mag_err = float(1.0857362047581296 * flux_err / flux)
    else:
        flags["non_positive_flux"] = True

    return ApertureMeasurement(
        flux=float(flux),
        flux_err=flux_err,
        mag_inst=mag_inst,
        mag_err=mag_err,
        local_background=local_background,
        aperture_radius_pix=float(aperture_radius_pix),
        flags=flags,
    )


def _select_frames(
    session: Session,
    *,
    frame_ids: list[int] | None,
    instrument: str | None,
    instrument_unit_id: int | None,
    only_wcs_ok: bool,
    limit: int | None,
) -> list[Frame]:
    stmt = select(Frame).order_by(Frame.frame_id)
    if frame_ids:
        stmt = stmt.where(Frame.frame_id.in_(frame_ids))
    if instrument:
        stmt = stmt.where(Frame.instrument == instrument)
    if instrument_unit_id is not None:
        stmt = stmt.where(Frame.instrument_unit_id == instrument_unit_id)
    if only_wcs_ok:
        stmt = stmt.where(Frame.wcs_status == "HEADER_WCS_OK")
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt).all())


def _select_frame_links(
    session: Session,
    *,
    frame_id: int,
    catalog_name: str | None,
    limit: int | None,
) -> list[tuple[FrameCatalogLink, CatalogSource]]:
    stmt = (
        select(FrameCatalogLink, CatalogSource)
        .join(CatalogSource, CatalogSource.catalog_source_id == FrameCatalogLink.catalog_source_id)
        .where(FrameCatalogLink.frame_id == frame_id)
        .where(FrameCatalogLink.in_footprint.is_(True))
        .order_by(FrameCatalogLink.frame_catalog_link_id)
    )
    if catalog_name:
        stmt = stmt.where(CatalogSource.catalog_name == catalog_name)
    if limit is not None:
        stmt = stmt.limit(limit)
    return [(link, source) for link, source in session.execute(stmt).all()]


def _existing_measurement_count(session: Session, *, frame_id: int, calibration_version_id: int) -> int:
    value = session.scalar(
        select(func.count())
        .select_from(Measurement)
        .where(Measurement.frame_id == frame_id)
        .where(Measurement.calibration_version_id == calibration_version_id)
    )
    return int(value or 0)


def _get_or_create_calibration_version(
    session: Session,
    *,
    name: str,
    instrument_unit_id: int | None,
    parameters: dict,
) -> CalibrationVersion:
    existing = session.scalar(select(CalibrationVersion).where(CalibrationVersion.name == name))
    if existing is not None:
        return existing
    calibration_version = CalibrationVersion(
        name=name,
        model_name="forced_aperture_mvp",
        instrument_unit_id=instrument_unit_id,
        instrument_profile={},
        used_catalogs={"catalog_name": parameters.get("catalog_name")},
        parameters=parameters,
        notes="MVP forced circular aperture photometry: local annulus median background; no calibration.",
    )
    session.add(calibration_version)
    return calibration_version


def _get_or_create_object_id(session: Session, source: CatalogSource, *, dry_run: bool) -> int | None:
    object_uid = _object_uid_from_source(source)
    existing_id = session.scalar(select(Object.object_id).where(Object.object_uid == object_uid))
    if existing_id is not None:
        return int(existing_id)
    if dry_run:
        return None
    external_ids = {source.catalog_name: source.source_id}
    gaia_dr3_id = source.source_id if source.catalog_name.lower() in {"gaia", "gaia_dr3", "gaiadr3"} else None
    obj = Object(
        object_uid=object_uid,
        ra_deg=source.ra_deg,
        dec_deg=source.dec_deg,
        gaia_dr3_id=gaia_dr3_id,
        external_ids=external_ids,
        ref_mags=source.ref_mags or {},
        colors=source.colors or {},
        pmra_masyr=source.pmra_masyr,
        pmdec_masyr=source.pmdec_masyr,
        object_flags={"created_from_catalog_source_id": source.catalog_source_id, "catalog_name": source.catalog_name},
    )
    session.add(obj)
    session.flush()
    return int(obj.object_id)


def _object_uid_from_source(source: CatalogSource) -> str:
    text = f"{source.catalog_name}|{source.source_id}|{source.ra_deg:.10f}|{source.dec_deg:.10f}"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
    return f"obj_{digest}"


def _resolve_saturation_level(*, explicit: float | None, frame: Frame, header) -> float | None:
    if explicit is not None:
        return explicit
    if frame.instrument_unit is not None and frame.instrument_unit.saturation_level_adu is not None:
        return float(frame.instrument_unit.saturation_level_adu)
    for key in ("SATURATE", "SATLEVEL", "SATUR", "MAXADU"):
        value = header.get(key)
        try:
            if value is not None:
                return float(value)
        except Exception:
            pass
    return None


def _safe_positive_float(value) -> float | None:
    try:
        value = float(value)
    except Exception:
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def _empty_measurement(aperture_radius_pix: float, flags: dict) -> ApertureMeasurement:
    return ApertureMeasurement(
        flux=None,
        flux_err=None,
        mag_inst=None,
        mag_err=None,
        local_background=None,
        aperture_radius_pix=float(aperture_radius_pix),
        flags=flags,
    )


def _fmt_float(value: float) -> str:
    text = f"{value:g}"
    return text.replace(".", "p").replace("-", "m")
