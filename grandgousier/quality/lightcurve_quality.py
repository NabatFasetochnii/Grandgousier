from __future__ import annotations

import csv
import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Any, Iterable, Mapping

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from grandgousier.db.models import (
    CalibrationFrameTerm,
    CalibrationIteration,
    CalibrationIterationObject,
    CalibrationVersion,
    Frame,
    InstrumentUnit,
    Measurement,
    Object,
)
from grandgousier.photometry.calibration_view import load_calibration_photometry_view


@dataclass
class ObjectScatterRow:
    object_id: int
    calibration_name: str | None
    instrument_unit_id: int | None
    instrument: str | None
    filter_name: str | None
    catalog_name: str | None
    source_id: str | None
    ra_deg: float | None
    dec_deg: float | None
    n_measurements: int
    mag_mean: float | None
    mag_median: float | None
    mag_rms: float | None
    mag_mad: float | None
    mag_err_median: float | None
    flux_median: float | None
    ref_mag: float | None
    color: float | None
    mjd_min: float | None
    mjd_max: float | None
    reference_catalog_name: str | None = None
    reference_band: str | None = None
    mag_err_systematic_median: float | None = None
    mag_p10: float | None = None
    mag_p90: float | None = None
    robust_peak_to_peak: float | None = None

    def to_dict(self) -> dict:
        return {
            "object_id": self.object_id,
            "calibration_name": self.calibration_name,
            "instrument_unit_id": self.instrument_unit_id,
            "instrument": self.instrument,
            "filter_name": self.filter_name,
            "catalog_name": self.catalog_name,
            "source_id": self.source_id,
            "ra_deg": self.ra_deg,
            "dec_deg": self.dec_deg,
            "n_measurements": self.n_measurements,
            "mag_mean": self.mag_mean,
            "mag_median": self.mag_median,
            "mag_rms": self.mag_rms,
            "mag_mad": self.mag_mad,
            "mag_err_median": self.mag_err_median,
            "mag_p10": self.mag_p10,
            "mag_p90": self.mag_p90,
            "robust_peak_to_peak": self.robust_peak_to_peak,
            "flux_median": self.flux_median,
            "ref_mag": self.ref_mag,
            "color": self.color,
            "reference_catalog_name": self.reference_catalog_name,
            "reference_band": self.reference_band,
            "mag_err_systematic_median": self.mag_err_systematic_median,
            "mjd_min": self.mjd_min,
            "mjd_max": self.mjd_max,
        }



@dataclass(frozen=True, slots=True)
class CalibrationMeasurementRow:
    object_id: int
    frame_id: int
    analysis_mag: float | None
    mag_cal: float | None
    mag_err_random: float | None
    mag_err: float | None
    mag_err_systematic: float | None
    flux: float | None
    x: float | None
    y: float | None
    aperture_radius_pix: float | None
    flags: dict[str, Any]
    # Transient source identity/intermediate state retained so later pipeline
    # stages can reuse the runner snapshot without re-reading BASE photometry.
    source_measurement_id: int | None = None
    mag_inst: float | None = None


@dataclass(frozen=True, slots=True)
class CalibrationFrameRow:
    frame_id: int
    instrument_unit_id: int | None
    mjd: float | None
    airmass: float | None
    header_json: dict[str, Any]
    quality_flags: dict[str, Any]
    instrument: str | None
    filter_name: str | None
    pixel_scale_arcsec: float | None
    date_obs: Any | None = None


@dataclass(frozen=True, slots=True)
class CalibrationObjectRow:
    object_id: int
    ra_deg: float | None
    dec_deg: float | None
    ref_mags: dict[str, Any]
    colors: dict[str, Any]


@dataclass
class CalibrationAnalysisSnapshot:
    calibration_version_id: int
    calibration_name: str
    catalog_name: str | None
    instrument_unit_id: int | None
    frame_ids: tuple[int, ...] | None
    min_measurements: int
    mag_key: str
    color_key: str
    y_key: str
    fwhm_header_unit: str
    measurement_rows: tuple[CalibrationMeasurementRow, ...]
    frames_by_id: dict[int, CalibrationFrameRow]
    objects_by_id: dict[int, CalibrationObjectRow]
    object_rows: list[ObjectScatterRow]
    measurement_geometry: dict[int, dict[str, Any]]
    materialization_source: str = "MEASUREMENTS"
    algorithm: str = "calibration_analysis_snapshot_v2_storage_independent"
    timings_sec: dict[str, float] = field(default_factory=dict)

    def matches(
        self,
        *,
        calibration_name: str,
        catalog_name: str | None,
        instrument_unit_id: int | None,
        frame_ids: list[int] | tuple[int, ...] | None,
        min_measurements: int,
        mag_key: str = "G",
        color_key: str = "BP_RP",
        y_key: str = "mag_cal",
        fwhm_header_unit: str = "arcsec",
    ) -> bool:
        return (
            self.calibration_name == str(calibration_name)
            and self.catalog_name == catalog_name
            and self.instrument_unit_id == instrument_unit_id
            and self.frame_ids == _normalize_frame_ids(frame_ids)
            and self.min_measurements == int(min_measurements)
            and self.mag_key == str(mag_key)
            and self.color_key == str(color_key)
            and self.y_key == str(y_key)
            and self.fwhm_header_unit == str(fwhm_header_unit or "arcsec")
        )


_COMPACT_ANALYSIS_FLAG_KEYS = (
    "bad_xy",
    "outside_image",
    "empty_aperture",
    "non_positive_flux",
    "saturated",
    "near_saturated",
    "saturated_or_near_saturated",
)


def _fuse_compact_normalized_ensemble_rows(
    base_rows: Iterable[Mapping[str, Any]],
    *,
    aperture_terms: Mapping[int, CalibrationFrameTerm],
    aperture_zp_terms: Mapping[int, CalibrationFrameTerm],
    ensemble_terms: Mapping[int, CalibrationFrameTerm],
    ensemble_object_terms: Mapping[int, CalibrationIterationObject],
    aperture_zp_catalog_name: str | None,
    ensemble_catalog_name: str | None,
) -> tuple[CalibrationMeasurementRow, ...]:
    """Fuse normalized APERTURE -> ZP -> ENSEMBLE state in one compact pass.

    The generic reconstruction engine deliberately preserves the complete
    measurement state and provenance dictionary after every stage.  Object-QC
    needs a much smaller contract: final ensemble magnitude/random error, base
    geometry/flux, selected rejection/error flags, and ensemble reference
    membership.  Reconstructing only those fields avoids three 50k-row dict
    copies and avoids carrying the full BASE JSONB payload through Python.

    This helper mirrors the scalar formulas in ``calibration.reconstruction``;
    eligibility is checked by the PostgreSQL loader below before it is used by
    production code.
    """

    from grandgousier.calibration.reconstruction import (
        _aperture_differential_error,
        _aperture_prediction,
    )

    out: list[CalibrationMeasurementRow] = []
    for base in base_rows:
        object_id = int(base["object_id"])
        frame_id = int(base["frame_id"])
        base_catalog_name = base.get("catalog_name")

        aperture_term = aperture_terms.get(frame_id)
        correction = 0.0
        differential_error = 0.0
        if aperture_term is not None and bool(aperture_term.accepted):
            correction = _aperture_prediction(aperture_term, base.get("x"), base.get("y"))
            differential_error = _aperture_differential_error(
                aperture_term, base.get("x"), base.get("y")
            )

        flux = base.get("flux")
        if flux is not None:
            try:
                flux_value = float(flux)
            except (TypeError, ValueError):
                flux_value = math.nan
            if math.isfinite(flux_value):
                flux = flux_value * (10.0 ** (-0.4 * float(correction)))

        mag_inst = _to_float(base.get("mag_inst"))
        if mag_inst is not None:
            mag_inst += float(correction)

        random_err = _first_finite_value(base.get("mag_err_random"), base.get("mag_err"))
        if random_err is not None:
            random_err = math.hypot(float(random_err), float(differential_error))
        else:
            # Generic reconstruction uses _quadrature(None, 0.0) -> 0.0.
            random_err = abs(float(differential_error))

        if aperture_zp_catalog_name and str(base_catalog_name) != str(aperture_zp_catalog_name):
            continue
        zp_term = aperture_zp_terms.get(frame_id)
        if zp_term is None or not bool(zp_term.accepted):
            continue
        zp = _to_float(zp_term.offset_mag)
        if zp is None or mag_inst is None:
            continue
        mag_cal = float(mag_inst) + float(zp)

        effective_catalog_name = aperture_zp_catalog_name or base_catalog_name
        if ensemble_catalog_name and str(effective_catalog_name) != str(ensemble_catalog_name):
            continue
        ensemble_term = ensemble_terms.get(frame_id)
        if ensemble_term is None or not bool(ensemble_term.accepted):
            continue
        ensemble_offset = _to_float(ensemble_term.offset_mag)
        if ensemble_offset is None:
            continue
        mag_cal = float(mag_cal) - float(ensemble_offset)

        flags: dict[str, Any] = {}
        if effective_catalog_name is not None:
            flags["catalog_name"] = effective_catalog_name
        if base.get("source_id") is not None:
            flags["source_id"] = base.get("source_id")
        for key in _COMPACT_ANALYSIS_FLAG_KEYS:
            value = base.get(key)
            if value is not None:
                flags[key] = bool(value)
        for key in ("random_error_physical_mag_err", "random_error_legacy_mag_err"):
            value = _to_float(base.get(key))
            if value is not None:
                flags[key] = value
        for key in (
            "reference_mag",
            "reference_color_value",
            "reference_catalog_name",
            "reference_band",
        ):
            value = base.get(key)
            if value is not None:
                flags[key] = value

        object_term = ensemble_object_terms.get(object_id)
        is_reference = bool(object_term.is_reference) if object_term is not None else False
        requested_exclusion = (
            bool(object_term.requested_exclusion) if object_term is not None else False
        )
        effective_exclusion = (
            bool(object_term.effective_exclusion) if object_term is not None else False
        )
        flags.update(
            {
                "ensemble_object_is_reference": is_reference,
                "ensemble_object_requested_reference_exclusion": requested_exclusion,
                "ensemble_object_excluded_from_reference": effective_exclusion,
                "ensemble_reference_exclusion_reason": (
                    "candidate_or_target_object" if requested_exclusion else None
                ),
            }
        )

        out.append(
            CalibrationMeasurementRow(
                object_id=object_id,
                frame_id=frame_id,
                analysis_mag=mag_cal,
                mag_cal=mag_cal,
                mag_err_random=random_err,
                mag_err=random_err,
                mag_err_systematic=_to_float(base.get("mag_err_systematic")),
                flux=_to_float(flux),
                x=_to_float(base.get("x")),
                y=_to_float(base.get("y")),
                aperture_radius_pix=_to_float(base.get("aperture_radius_pix")),
                flags=flags,
                source_measurement_id=(
                    int(base["measurement_id"])
                    if base.get("measurement_id") is not None
                    else None
                ),
                mag_inst=mag_inst,
            )
        )
    return tuple(out)


def _load_fused_compact_normalized_ensemble_rows(
    session: Session,
    *,
    calibration_version: CalibrationVersion,
    frame_ids: tuple[int, ...] | None,
    instrument_unit_id: int | None,
    y_key: str,
    timings: dict[str, float],
    preloaded_base_rows: Iterable[Mapping[str, Any]] | None = None,
) -> tuple[CalibrationMeasurementRow, ...] | None:
    """PostgreSQL BASE scalar read + one-pass Python normalized reconstruction.

    This is intentionally narrower than the failed 0.5.40.29 server-side QC
    reconstruction.  PostgreSQL only extracts the small BASE scalar/flag subset;
    the inexpensive 66-frame normalized terms are then applied in one Python
    pass.  Unsupported storage/chains fall back to the authoritative generic
    photometry view.
    """

    bind = session.get_bind()
    if bind is None or str(bind.dialect.name).lower() != "postgresql":
        return None
    if str(calibration_version.product_kind or "").upper() != "ENSEMBLE":
        return None
    if str(calibration_version.materialization_kind or "").upper() != "NORMALIZED_TERMS":
        return None
    if str(y_key) != "mag_cal":
        return None
    # Preserve calibration_view's compatibility precedence: if a supposedly
    # compact ENSEMBLE generation actually owns legacy Measurement rows, those
    # rows remain authoritative and the generic reader must win.
    legacy_count = int(
        session.scalar(
            select(func.count(Measurement.measurement_id)).where(
                Measurement.calibration_version_id
                == int(calibration_version.calibration_version_id)
            )
        )
        or 0
    )
    if legacy_count > 0:
        return None

    from grandgousier.calibration.reconstruction import _chain_from_base, _latest_iteration

    phase_started = perf_counter()
    chain = _chain_from_base(session, calibration_version)
    kinds = [str(cv.product_kind or "").upper() for cv in chain]
    if kinds != ["BASE_PHOTOMETRY", "APERTURE_CORRECTION", "APERTURE_ZP", "ENSEMBLE"]:
        return None
    base_cv, aperture_cv, aperture_zp_cv, ensemble_cv = chain
    if str(base_cv.materialization_kind or "").upper() != "FULL_MEASUREMENTS":
        return None

    aperture_iteration = _latest_iteration(session, int(aperture_cv.calibration_version_id))
    aperture_zp_iteration = _latest_iteration(session, int(aperture_zp_cv.calibration_version_id))
    ensemble_iteration = _latest_iteration(session, int(ensemble_cv.calibration_version_id))
    if aperture_iteration is None or aperture_zp_iteration is None or ensemble_iteration is None:
        return None
    ensemble_parameters = dict(ensemble_iteration.parameters or {})
    if not bool(ensemble_parameters.get("prefer_mag_cal", True)):
        return None
    timings["compact_normalized_plan"] = perf_counter() - phase_started

    phase_started = perf_counter()
    aperture_terms = {
        int(term.frame_id): term
        for term in session.scalars(
            select(CalibrationFrameTerm).where(
                CalibrationFrameTerm.calibration_iteration_id
                == int(aperture_iteration.calibration_iteration_id)
            )
        ).all()
    }
    aperture_zp_terms = {
        int(term.frame_id): term
        for term in session.scalars(
            select(CalibrationFrameTerm).where(
                CalibrationFrameTerm.calibration_iteration_id
                == int(aperture_zp_iteration.calibration_iteration_id)
            )
        ).all()
        if bool(term.accepted) and _to_float(term.offset_mag) is not None
    }
    ensemble_terms = {
        int(term.frame_id): term
        for term in session.scalars(
            select(CalibrationFrameTerm).where(
                CalibrationFrameTerm.calibration_iteration_id
                == int(ensemble_iteration.calibration_iteration_id)
            )
        ).all()
        if bool(term.accepted) and _to_float(term.offset_mag) is not None
    }
    ensemble_object_terms = {
        int(term.object_id): term
        for term in session.scalars(
            select(CalibrationIterationObject).where(
                CalibrationIterationObject.calibration_iteration_id
                == int(ensemble_iteration.calibration_iteration_id)
            )
        ).all()
    }
    timings["compact_normalized_term_load"] = perf_counter() - phase_started

    phase_started = perf_counter()
    if preloaded_base_rows is None:
        columns = [
            Measurement.measurement_id.label("measurement_id"),
            Measurement.object_id.label("object_id"),
            Measurement.frame_id.label("frame_id"),
            Measurement.x.label("x"),
            Measurement.y.label("y"),
            Measurement.flux.label("flux"),
            Measurement.mag_inst.label("mag_inst"),
            Measurement.mag_err.label("mag_err"),
            Measurement.mag_err_random.label("mag_err_random"),
            Measurement.mag_err_systematic.label("mag_err_systematic"),
            Measurement.aperture_radius_pix.label("aperture_radius_pix"),
            Measurement.flags["catalog_name"].as_string().label("catalog_name"),
            Measurement.flags["source_id"].as_string().label("source_id"),
            Measurement.flags["bad_xy"].as_boolean().label("bad_xy"),
            Measurement.flags["outside_image"].as_boolean().label("outside_image"),
            Measurement.flags["empty_aperture"].as_boolean().label("empty_aperture"),
            Measurement.flags["non_positive_flux"].as_boolean().label("non_positive_flux"),
            Measurement.flags["saturated"].as_boolean().label("saturated"),
            Measurement.flags["near_saturated"].as_boolean().label("near_saturated"),
            Measurement.flags["saturated_or_near_saturated"].as_boolean().label(
                "saturated_or_near_saturated"
            ),
            Measurement.flags["random_error_physical_mag_err"].as_float().label(
                "random_error_physical_mag_err"
            ),
            Measurement.flags["random_error_legacy_mag_err"].as_float().label(
                "random_error_legacy_mag_err"
            ),
            Measurement.flags["reference_mag"].as_float().label("reference_mag"),
            Measurement.flags["reference_color_value"].as_float().label(
                "reference_color_value"
            ),
            Measurement.flags["reference_catalog_name"].as_string().label(
                "reference_catalog_name"
            ),
            Measurement.flags["reference_band"].as_string().label("reference_band"),
        ]
        stmt = select(*columns).where(
            Measurement.calibration_version_id == int(base_cv.calibration_version_id)
        )
        if frame_ids:
            stmt = stmt.where(Measurement.frame_id.in_(frame_ids))
        if instrument_unit_id is not None:
            stmt = stmt.join(Frame, Frame.frame_id == Measurement.frame_id).where(
                Frame.instrument_unit_id == int(instrument_unit_id)
            )
        raw_rows = session.execute(
            stmt.order_by(Measurement.object_id, Measurement.frame_id)
        ).mappings().all()
        timings["compact_normalized_base_query"] = perf_counter() - phase_started
        timings["compact_normalized_seed_reuse"] = 0.0
    else:
        raw_rows = tuple(preloaded_base_rows)
        timings["compact_normalized_base_query"] = 0.0
        timings["compact_normalized_seed_reuse"] = perf_counter() - phase_started
        timings["compact_normalized_seed_rows"] = float(len(raw_rows))

    phase_started = perf_counter()
    rows = _fuse_compact_normalized_ensemble_rows(
        raw_rows,
        aperture_terms=aperture_terms,
        aperture_zp_terms=aperture_zp_terms,
        ensemble_terms=ensemble_terms,
        ensemble_object_terms=ensemble_object_terms,
        aperture_zp_catalog_name=dict(aperture_zp_iteration.parameters or {}).get(
            "catalog_name"
        ),
        ensemble_catalog_name=ensemble_parameters.get("catalog_name"),
    )
    timings["compact_normalized_fused_reconstruction"] = perf_counter() - phase_started
    return rows


def load_calibration_analysis_snapshot(
    session: Session,
    *,
    calibration_name: str,
    calibration_version_id: int | None = None,
    catalog_name: str | None = None,
    instrument_unit_id: int | None = None,
    frame_ids: list[int] | tuple[int, ...] | None = None,
    min_measurements: int = 5,
    mag_key: str = "G",
    color_key: str = "BP_RP",
    y_key: str = "mag_cal",
    fwhm_header_unit: str = "arcsec",
    _preloaded_compact_base_rows: Iterable[Mapping[str, Any]] | None = None,
) -> CalibrationAnalysisSnapshot:
    """Load one compact scalar snapshot for repeated calibration QC analysis.

    The snapshot replaces repeated hydration of full ``Measurement``, ``Frame``,
    ``Object`` and ``InstrumentUnit`` ORM entities. Measurement rows are loaded
    once; frame JSON is decoded once per frame rather than once per measurement.
    The same in-memory data feeds object scatter, object geometry, candidate
    triage, field reports and random-error validation.
    """

    total_started = perf_counter()
    timings: dict[str, float] = {}
    normalized_frame_ids = _normalize_frame_ids(frame_ids)

    phase_started = perf_counter()
    cv_id = int(calibration_version_id) if calibration_version_id is not None else None
    if cv_id is None:
        cv_id = session.scalar(
            select(CalibrationVersion.calibration_version_id).where(
                CalibrationVersion.name == calibration_name
            )
        )
    if cv_id is None:
        raise ValueError(f"Calibration version not found: {calibration_name}")
    timings["calibration_lookup"] = perf_counter() - phase_started

    supported_analysis_columns = {
        "mag_inst",
        "mag_cal",
        "mag_err",
        "mag_err_random",
        "mag_err_systematic",
        "flux",
        "flux_err",
        "x",
        "y",
        "aperture_radius_pix",
        "local_background",
        "local_zp_correction",
        "centroid_dx_pix",
        "centroid_dy_pix",
    }
    if y_key not in supported_analysis_columns:
        raise ValueError(f"Calibration photometry column not found: {y_key}")

    phase_started = perf_counter()
    materialization_source = None
    cv = session.get(CalibrationVersion, int(cv_id))
    measurement_rows = None
    if cv is not None:
        measurement_rows = _load_fused_compact_normalized_ensemble_rows(
            session,
            calibration_version=cv,
            frame_ids=normalized_frame_ids,
            instrument_unit_id=instrument_unit_id,
            y_key=y_key,
            timings=timings,
            preloaded_base_rows=_preloaded_compact_base_rows,
        )
        if measurement_rows is not None:
            materialization_source = (
                "RECONSTRUCTED_NORMALIZED_PYTHON_FUSED_PRELOADED_BASE_V2"
                if _preloaded_compact_base_rows is not None
                else "RECONSTRUCTED_NORMALIZED_PYTHON_FUSED_COMPACT_V1"
            )

    if measurement_rows is None:
        photometry_view = load_calibration_photometry_view(
            session,
            calibration_version_id=cv_id,
            frame_ids=normalized_frame_ids,
            prefer_science=True,
        )
        raw_measurement_rows = list(photometry_view.rows)

        # Frame metadata is loaded below in one batch.  When a caller requests one
        # instrument explicitly, filter by frame identity before constructing the
        # snapshot so the semantics match the former Measurement/Frame SQL query.
        if instrument_unit_id is not None and raw_measurement_rows:
            candidate_frame_ids = sorted({row.frame_id for row in raw_measurement_rows})
            allowed_frame_ids = set(
                int(value)
                for value in session.scalars(
                    select(Frame.frame_id).where(
                        Frame.frame_id.in_(candidate_frame_ids),
                        Frame.instrument_unit_id == int(instrument_unit_id),
                    )
                ).all()
            )
            raw_measurement_rows = [
                row for row in raw_measurement_rows if int(row.frame_id) in allowed_frame_ids
            ]

        measurement_rows = tuple(
            CalibrationMeasurementRow(
                object_id=int(row.object_id),
                frame_id=int(row.frame_id),
                analysis_mag=getattr(row, y_key),
                mag_cal=row.mag_cal,
                mag_err_random=row.mag_err_random,
                mag_err=row.mag_err,
                mag_err_systematic=row.mag_err_systematic,
                flux=row.flux,
                x=row.x,
                y=row.y,
                aperture_radius_pix=row.aperture_radius_pix,
                flags=row.flags if isinstance(row.flags, dict) else {},
                source_measurement_id=(
                    int(row.measurement_id)
                    if getattr(row, "measurement_id", None) is not None
                    else None
                ),
                mag_inst=getattr(row, "mag_inst", None),
            )
            for row in raw_measurement_rows
        )
        materialization_source = photometry_view.source
    timings["load_measurement_rows"] = perf_counter() - phase_started

    object_ids = sorted({row.object_id for row in measurement_rows})
    frame_id_values = sorted({row.frame_id for row in measurement_rows})

    phase_started = perf_counter()
    objects_by_id: dict[int, CalibrationObjectRow] = {}
    if object_ids:
        object_stmt = select(
            Object.object_id.label("object_id"),
            Object.ra_deg.label("ra_deg"),
            Object.dec_deg.label("dec_deg"),
            Object.ref_mags.label("ref_mags"),
            Object.colors.label("colors"),
        ).where(Object.object_id.in_(object_ids))
        for row in session.execute(object_stmt).mappings():
            object_id = int(row["object_id"])
            objects_by_id[object_id] = CalibrationObjectRow(
                object_id=object_id,
                ra_deg=row["ra_deg"],
                dec_deg=row["dec_deg"],
                ref_mags=row["ref_mags"] if isinstance(row["ref_mags"], dict) else {},
                colors=row["colors"] if isinstance(row["colors"], dict) else {},
            )
    timings["load_object_rows"] = perf_counter() - phase_started

    phase_started = perf_counter()
    frames_by_id: dict[int, CalibrationFrameRow] = {}
    if frame_id_values:
        frame_stmt = (
            select(
                Frame.frame_id.label("frame_id"),
                Frame.instrument_unit_id.label("instrument_unit_id"),
                Frame.mjd.label("mjd"),
                Frame.date_obs.label("date_obs"),
                Frame.airmass.label("airmass"),
                Frame.header_json.label("header_json"),
                Frame.quality_flags.label("quality_flags"),
                InstrumentUnit.instrument.label("instrument"),
                InstrumentUnit.filter_name.label("filter_name"),
                InstrumentUnit.pixel_scale_arcsec.label("pixel_scale_arcsec"),
            )
            .outerjoin(
                InstrumentUnit,
                InstrumentUnit.instrument_unit_id == Frame.instrument_unit_id,
            )
            .where(Frame.frame_id.in_(frame_id_values))
        )
        for row in session.execute(frame_stmt).mappings():
            frame_id = int(row["frame_id"])
            frames_by_id[frame_id] = CalibrationFrameRow(
                frame_id=frame_id,
                instrument_unit_id=(
                    int(row["instrument_unit_id"])
                    if row["instrument_unit_id"] is not None
                    else None
                ),
                mjd=row["mjd"],
                airmass=row["airmass"],
                header_json=(
                    row["header_json"] if isinstance(row["header_json"], dict) else {}
                ),
                quality_flags=(
                    row["quality_flags"]
                    if isinstance(row["quality_flags"], dict)
                    else {}
                ),
                instrument=row["instrument"],
                filter_name=row["filter_name"],
                pixel_scale_arcsec=row["pixel_scale_arcsec"],
                date_obs=row["date_obs"],
            )
    timings["load_frame_rows"] = perf_counter() - phase_started

    phase_started = perf_counter()
    object_rows = _build_object_scatter_rows_from_snapshot_rows(
        calibration_name=calibration_name,
        catalog_name=catalog_name,
        min_measurements=int(min_measurements),
        mag_key=mag_key,
        color_key=color_key,
        measurement_rows=measurement_rows,
        frames_by_id=frames_by_id,
        objects_by_id=objects_by_id,
    )
    timings["build_object_scatter_rows"] = perf_counter() - phase_started

    phase_started = perf_counter()
    measurement_geometry = _build_measurement_geometry_from_snapshot_rows(
        catalog_name=catalog_name,
        measurement_rows=measurement_rows,
        frames_by_id=frames_by_id,
        fwhm_header_unit=fwhm_header_unit,
    )
    timings["build_measurement_geometry"] = perf_counter() - phase_started
    timings["total"] = perf_counter() - total_started
    timings["_runner_cache_hit"] = 0.0

    return CalibrationAnalysisSnapshot(
        calibration_version_id=cv_id,
        calibration_name=str(calibration_name),
        catalog_name=catalog_name,
        instrument_unit_id=instrument_unit_id,
        frame_ids=normalized_frame_ids,
        min_measurements=int(min_measurements),
        mag_key=str(mag_key),
        color_key=str(color_key),
        y_key=str(y_key),
        fwhm_header_unit=str(fwhm_header_unit or "arcsec"),
        measurement_rows=measurement_rows,
        frames_by_id=frames_by_id,
        objects_by_id=objects_by_id,
        object_rows=object_rows,
        measurement_geometry=measurement_geometry,
        materialization_source=str(materialization_source or "UNKNOWN"),
        timings_sec=timings,
    )



def derive_color_calibration_analysis_snapshot(
    session: Session,
    *,
    source_snapshot: CalibrationAnalysisSnapshot,
    target_calibration_name: str,
) -> CalibrationAnalysisSnapshot | None:
    """Derive a color-calibrated QC snapshot from its cached ensemble parent.

    Color calibration changes only magnitude/error/reference-system state per
    object. Frame metadata, object metadata and measurement geometry are
    invariant. Rebuilding the target snapshot from normalized color terms avoids
    re-reading/reconstructing tens of thousands of photometry rows solely for
    the second Object-QC/field-report pass.

    ``None`` means the target is not a supported direct color child of the
    supplied snapshot; callers should fall back to the normal loader.
    """

    total_started = perf_counter()
    target_cv = session.scalar(
        select(CalibrationVersion).where(
            CalibrationVersion.name == str(target_calibration_name)
        )
    )
    if target_cv is None:
        return None
    if str(target_cv.product_kind or "").upper() != "COLOR_CALIBRATION":
        return None
    if target_cv.source_calibration_version_id is None:
        return None
    if int(target_cv.source_calibration_version_id) != int(
        source_snapshot.calibration_version_id
    ):
        return None
    if source_snapshot.y_key != "mag_cal":
        return None

    phase_started = perf_counter()
    iteration = session.scalar(
        select(CalibrationIteration)
        .where(
            CalibrationIteration.calibration_version_id
            == int(target_cv.calibration_version_id)
        )
        .order_by(
            CalibrationIteration.iteration_no.desc(),
            CalibrationIteration.calibration_iteration_id.desc(),
        )
        .limit(1)
    )
    if iteration is None:
        return None
    metrics = dict(iteration.metrics or {})
    parameters = dict(iteration.parameters or {})
    offset = _to_float(metrics.get("transform_offset"))
    color_term = _to_float(metrics.get("color_term"))
    color0 = _to_float(metrics.get("color0"))
    if offset is None or color_term is None or color0 is None:
        return None

    object_terms: dict[int, dict[str, Any]] = {}
    term_stmt = select(
        CalibrationIterationObject.object_id.label("object_id"),
        CalibrationIterationObject.details.label("details"),
    ).where(
        CalibrationIterationObject.calibration_iteration_id
        == int(iteration.calibration_iteration_id)
    )
    for row in session.execute(term_stmt).mappings():
        object_terms[int(row["object_id"])] = (
            dict(row["details"]) if isinstance(row["details"], dict) else {}
        )
    terms_load_sec = perf_counter() - phase_started

    identity_catalog_name = parameters.get("identity_catalog_name")
    target_rows: list[CalibrationMeasurementRow] = []
    missing_object_ids: set[int] = set()
    phase_started = perf_counter()
    for source in source_snapshot.measurement_rows:
        source_flags = source.flags if isinstance(source.flags, dict) else {}
        if (
            identity_catalog_name
            and source_flags.get("catalog_name") != identity_catalog_name
        ):
            continue
        details = object_terms.get(int(source.object_id))
        if details is None:
            missing_object_ids.add(int(source.object_id))
            continue
        applied_color = _to_float(details.get("applied_color_value"))
        color_missing = bool(details.get("color_missing"))
        if applied_color is None:
            if color_missing:
                applied_color = float(color0)
            else:
                missing_object_ids.add(int(source.object_id))
                continue
        correction = float(offset) + float(color_term) * (
            float(applied_color) - float(color0)
        )
        mag_cal = (
            float(source.mag_cal) + correction
            if source.mag_cal is not None and _is_finite(source.mag_cal)
            else None
        )
        random_err = (
            float(source.mag_err_random)
            if source.mag_err_random is not None
            and _is_finite(source.mag_err_random)
            else (
                float(source.mag_err)
                if source.mag_err is not None and _is_finite(source.mag_err)
                else None
            )
        )
        systematic_error = _to_float(details.get("systematic_error"))
        flags = dict(source_flags)
        flags.update(
            {
                "calibration_stage": "reference_color_calibration",
                "calibration_method": "ensemble_preserving_reference_color_transform",
                "color_calibration_algorithm": "ensemble_preserving_reference_transform_v3",
                "reference_transform_model": parameters.get("selected_transform_model"),
                "reference_transform_model_selection_reason": parameters.get(
                    "model_selection_reason"
                ),
                "source_calibration_version_id": int(
                    target_cv.source_calibration_version_id
                ),
                "source_calibration_name": source_snapshot.calibration_name,
                "identity_catalog_name": parameters.get("identity_catalog_name"),
                "reference_catalog_name": parameters.get("reference_catalog_name"),
                "reference_catalog_source_id": details.get(
                    "reference_catalog_source_id"
                ),
                "reference_band": parameters.get("reference_band"),
                "reference_mag": details.get("reference_mag"),
                "reference_color_index": parameters.get("color_index"),
                "reference_color_value": details.get("reference_color_value"),
                "reference_color_applied_value": applied_color,
                "reference_color_missing": color_missing,
                "reference_color_assumed_pivot": color_missing,
                "reference_color0": color0,
                "reference_color_term": color_term,
                "reference_fitted_color_term": metrics.get("fitted_color_term"),
                "reference_fitted_color_term_error": metrics.get(
                    "fitted_color_term_err"
                ),
                "reference_transform_offset": offset,
                "reference_transform_offset_error": metrics.get(
                    "transform_offset_err"
                ),
                "reference_color_term_error": metrics.get("color_term_err"),
                "reference_transform_systematic_floor": metrics.get(
                    "transform_residual_robust_sigma"
                ),
                "reference_color_correction": correction,
                "reference_color_outside_fit_range": bool(
                    details.get("color_outside_fit_range")
                ),
                "reference_color_calibration_applied": bool(mag_cal is not None),
                "mag_err_systematic": systematic_error,
                "reference_color_error": details.get("color_error"),
            }
        )
        target_rows.append(
            CalibrationMeasurementRow(
                object_id=int(source.object_id),
                frame_id=int(source.frame_id),
                analysis_mag=mag_cal,
                mag_cal=mag_cal,
                mag_err_random=random_err,
                mag_err=random_err,
                mag_err_systematic=systematic_error,
                flux=source.flux,
                x=source.x,
                y=source.y,
                aperture_radius_pix=source.aperture_radius_pix,
                flags=flags,
                source_measurement_id=source.source_measurement_id,
                mag_inst=source.mag_inst,
            )
        )
    derive_rows_sec = perf_counter() - phase_started
    if missing_object_ids:
        return None

    measurement_rows = tuple(target_rows)
    phase_started = perf_counter()
    object_rows = _build_object_scatter_rows_from_snapshot_rows(
        calibration_name=str(target_calibration_name),
        catalog_name=source_snapshot.catalog_name,
        min_measurements=int(source_snapshot.min_measurements),
        mag_key=source_snapshot.mag_key,
        color_key=source_snapshot.color_key,
        measurement_rows=measurement_rows,
        frames_by_id=source_snapshot.frames_by_id,
        objects_by_id=source_snapshot.objects_by_id,
    )
    scatter_sec = perf_counter() - phase_started

    timings = {
        "calibration_lookup": 0.0,
        "load_measurement_rows": 0.0,
        "load_object_rows": 0.0,
        "load_frame_rows": 0.0,
        "derive_color_terms_load": terms_load_sec,
        "derive_color_measurement_rows": derive_rows_sec,
        "build_object_scatter_rows": scatter_sec,
        "build_measurement_geometry": 0.0,
        "total": perf_counter() - total_started,
        "_runner_cache_hit": 0.0,
        "_runner_derived": 1.0,
    }
    return CalibrationAnalysisSnapshot(
        calibration_version_id=int(target_cv.calibration_version_id),
        calibration_name=str(target_calibration_name),
        catalog_name=source_snapshot.catalog_name,
        instrument_unit_id=source_snapshot.instrument_unit_id,
        frame_ids=source_snapshot.frame_ids,
        min_measurements=int(source_snapshot.min_measurements),
        mag_key=source_snapshot.mag_key,
        color_key=source_snapshot.color_key,
        y_key=source_snapshot.y_key,
        fwhm_header_unit=source_snapshot.fwhm_header_unit,
        measurement_rows=measurement_rows,
        frames_by_id=source_snapshot.frames_by_id,
        objects_by_id=source_snapshot.objects_by_id,
        object_rows=object_rows,
        measurement_geometry=source_snapshot.measurement_geometry,
        materialization_source="DERIVED_COLOR_FROM_CACHED_PARENT",
        algorithm="calibration_analysis_snapshot_v3_incremental_color",
        timings_sec=timings,
    )

def load_object_scatter_rows(
    session: Session,
    *,
    calibration_name: str,
    catalog_name: str | None = None,
    instrument_unit_id: int | None = None,
    frame_ids: list[int] | None = None,
    min_measurements: int = 5,
    mag_key: str = "G",
    color_key: str = "BP_RP",
    y_key: str = "mag_cal",
) -> list[ObjectScatterRow]:
    """Return per-object scatter statistics for one calibration version."""

    snapshot = load_calibration_analysis_snapshot(
        session,
        calibration_name=calibration_name,
        catalog_name=catalog_name,
        instrument_unit_id=instrument_unit_id,
        frame_ids=frame_ids,
        min_measurements=min_measurements,
        mag_key=mag_key,
        color_key=color_key,
        y_key=y_key,
    )
    return snapshot.object_rows


def _build_object_scatter_rows_from_snapshot_rows(
    *,
    calibration_name: str,
    catalog_name: str | None,
    min_measurements: int,
    mag_key: str,
    color_key: str,
    measurement_rows: tuple[CalibrationMeasurementRow, ...],
    frames_by_id: dict[int, CalibrationFrameRow],
    objects_by_id: dict[int, CalibrationObjectRow],
) -> list[ObjectScatterRow]:
    grouped: dict[int, list[CalibrationMeasurementRow]] = defaultdict(list)
    for measurement in measurement_rows:
        flags = measurement.flags
        if catalog_name and flags.get("catalog_name") != catalog_name:
            continue
        grouped[measurement.object_id].append(measurement)

    rows: list[ObjectScatterRow] = []
    for object_id, values in grouped.items():
        if len(values) < int(min_measurements):
            continue
        obj = objects_by_id.get(object_id)
        if obj is None:
            continue
        first = values[0]
        frame0 = frames_by_id.get(first.frame_id)
        flags0 = first.flags

        mags: list[float] = []
        mag_errs: list[float] = []
        systematic_errs: list[float] = []
        fluxes: list[float] = []
        mjds: list[float] = []
        for measurement in values:
            if measurement.analysis_mag is not None and _is_finite(measurement.analysis_mag):
                mags.append(float(measurement.analysis_mag))
            random_err = (
                measurement.mag_err_random
                if measurement.mag_err_random is not None
                else measurement.mag_err
            )
            if random_err is not None and _is_finite(random_err):
                mag_errs.append(float(random_err))
            if (
                measurement.mag_err_systematic is not None
                and _is_finite(measurement.mag_err_systematic)
            ):
                systematic_errs.append(float(measurement.mag_err_systematic))
            if measurement.flux is not None and _is_finite(measurement.flux):
                fluxes.append(float(measurement.flux))
            frame = frames_by_id.get(measurement.frame_id)
            if frame is not None and frame.mjd is not None and _is_finite(frame.mjd):
                mjds.append(float(frame.mjd))

        if len(mags) < int(min_measurements):
            continue

        science_ref_mag, science_color, science_ref_catalog, science_ref_band = (
            _science_reference_from_snapshot_rows(values)
        )
        resolved_ref_mag, resolved_color = _resolve_object_reference(
            science_ref_mag=science_ref_mag,
            science_color=science_color,
            science_ref_catalog=science_ref_catalog,
            science_ref_band=science_ref_band,
            legacy_ref_mag=_to_float(obj.ref_mags.get(mag_key)),
            legacy_color=_to_float(obj.colors.get(color_key)),
        )
        rows.append(
            ObjectScatterRow(
                object_id=object_id,
                calibration_name=calibration_name,
                instrument_unit_id=(frame0.instrument_unit_id if frame0 is not None else None),
                instrument=(frame0.instrument if frame0 is not None else None),
                filter_name=(frame0.filter_name if frame0 is not None else None),
                catalog_name=flags0.get("catalog_name"),
                source_id=flags0.get("source_id"),
                ra_deg=obj.ra_deg,
                dec_deg=obj.dec_deg,
                n_measurements=len(mags),
                mag_mean=_mean(mags),
                mag_median=_median(mags),
                mag_rms=_sample_rms(mags),
                mag_mad=_mad(mags),
                mag_err_median=_median(mag_errs),
                mag_p10=_percentile(mags, 10.0),
                mag_p90=_percentile(mags, 90.0),
                robust_peak_to_peak=_robust_peak_to_peak(mags),
                flux_median=_median(fluxes),
                ref_mag=resolved_ref_mag,
                color=resolved_color,
                reference_catalog_name=science_ref_catalog,
                reference_band=science_ref_band,
                mag_err_systematic_median=_median(systematic_errs),
                mjd_min=min(mjds) if mjds else None,
                mjd_max=max(mjds) if mjds else None,
            )
        )
    rows.sort(
        key=lambda row: (
            float("inf") if row.mag_median is None else row.mag_median,
            row.object_id,
        )
    )
    return rows


def _build_measurement_geometry_from_snapshot_rows(
    *,
    catalog_name: str | None,
    measurement_rows: tuple[CalibrationMeasurementRow, ...],
    frames_by_id: dict[int, CalibrationFrameRow],
    fwhm_header_unit: str,
) -> dict[int, dict[str, Any]]:
    grouped: dict[int, list[CalibrationMeasurementRow]] = defaultdict(list)
    for measurement in measurement_rows:
        if catalog_name and measurement.flags.get("catalog_name") != catalog_name:
            continue
        grouped[measurement.object_id].append(measurement)

    out: dict[int, dict[str, Any]] = {}
    for object_id, values in grouped.items():
        xs: list[float] = []
        ys: list[float] = []
        edges: list[float] = []
        saturated = False
        aperture_radii_pix: list[float] = []
        aperture_radii_arcsec: list[float] = []
        pixel_scales: list[float] = []
        fwhm_arcsec_values: list[float] = []
        for measurement in values:
            frame = frames_by_id.get(measurement.frame_id)
            if frame is None:
                continue
            if measurement.x is not None and _is_finite(measurement.x):
                xs.append(float(measurement.x))
            if measurement.y is not None and _is_finite(measurement.y):
                ys.append(float(measurement.y))
            pixel_scale = _snapshot_pixel_scale_arcsec(frame)
            if pixel_scale is not None:
                pixel_scales.append(pixel_scale)
            if (
                measurement.aperture_radius_pix is not None
                and _is_finite(measurement.aperture_radius_pix)
            ):
                radius_pix = float(measurement.aperture_radius_pix)
                aperture_radii_pix.append(radius_pix)
                if pixel_scale is not None:
                    aperture_radii_arcsec.append(radius_pix * pixel_scale)
            fwhm_arcsec = _snapshot_frame_fwhm_arcsec(
                frame,
                fwhm_header_unit=fwhm_header_unit,
            )
            if fwhm_arcsec is not None:
                fwhm_arcsec_values.append(fwhm_arcsec)
            if measurement.flags.get("saturated_or_near_saturated"):
                saturated = True
            nx = _to_float(frame.header_json.get("NAXIS1"))
            ny = _to_float(frame.header_json.get("NAXIS2"))
            if (
                measurement.x is not None
                and measurement.y is not None
                and nx is not None
                and ny is not None
            ):
                x = float(measurement.x)
                y = float(measurement.y)
                edges.append(min(x, y, nx - 1.0 - x, ny - 1.0 - y))
        out[object_id] = {
            "median_x": float(median(xs)) if xs else None,
            "median_y": float(median(ys)) if ys else None,
            "edge_distance_pix_min": min(edges) if edges else None,
            "saturated_like": saturated,
            "aperture_radius_pix_median": (
                float(median(aperture_radii_pix)) if aperture_radii_pix else None
            ),
            "aperture_radius_arcsec_median": (
                float(median(aperture_radii_arcsec))
                if aperture_radii_arcsec
                else None
            ),
            "pixel_scale_arcsec_median": (
                float(median(pixel_scales)) if pixel_scales else None
            ),
            "fwhm_arcsec_median": (
                float(median(fwhm_arcsec_values)) if fwhm_arcsec_values else None
            ),
        }
    return out


def _snapshot_pixel_scale_arcsec(frame: CalibrationFrameRow) -> float | None:
    value = _to_float(frame.pixel_scale_arcsec)
    if value is not None and value > 0:
        return value
    quality = frame.quality_flags
    wcs_check = quality.get("wcs_check")
    candidates = [quality.get("pixel_scale_arcsec_mean")]
    if isinstance(wcs_check, dict):
        candidates.append(wcs_check.get("pixel_scale_arcsec_mean"))
        details = wcs_check.get("details")
        if isinstance(details, dict):
            candidates.append(details.get("pixel_scale_arcsec_mean"))
    for candidate in candidates:
        value = _to_float(candidate)
        if value is not None and value > 0:
            return value
    return None


def _snapshot_frame_fwhm_arcsec(
    frame: CalibrationFrameRow,
    *,
    fwhm_header_unit: str,
) -> float | None:
    value = None
    for key in (
        "SEXFWHM",
        "SEX_FWHM",
        "FWHM",
        "FWHMARC",
        "FWHM_ARCSEC",
        "SEEING",
        "SEEING1",
        "PSF_FWHM",
    ):
        if key in frame.header_json:
            value = _to_float(frame.header_json.get(key))
            if value is not None:
                break
    if value is None:
        return None
    unit = str(fwhm_header_unit or "arcsec").lower()
    if unit == "arcsec":
        return value if value > 0 else None
    pixel_scale = _snapshot_pixel_scale_arcsec(frame)
    if unit == "pix":
        return value * pixel_scale if pixel_scale is not None and value > 0 else None
    if unit == "auto":
        if pixel_scale is not None and 1.0 <= value <= 8.0:
            pixel_value = value * pixel_scale
            if 0.3 <= pixel_value <= 20.0:
                return pixel_value
        return value if value > 0 else None
    return value if value > 0 else None


def _normalize_frame_ids(
    frame_ids: list[int] | tuple[int, ...] | None,
) -> tuple[int, ...] | None:
    if not frame_ids:
        return None
    return tuple(sorted({int(value) for value in frame_ids}))


def _science_reference_from_snapshot_rows(
    values: list[CalibrationMeasurementRow],
) -> tuple[float | None, float | None, str | None, str | None]:
    for measurement in values:
        flags = measurement.flags
        ref_mag = _to_float(flags.get("reference_mag"))
        ref_color = _to_float(flags.get("reference_color_value"))
        ref_catalog = flags.get("reference_catalog_name")
        ref_band = flags.get("reference_band")
        if ref_catalog or ref_band or ref_mag is not None:
            return (
                ref_mag,
                ref_color,
                str(ref_catalog) if ref_catalog else None,
                str(ref_band) if ref_band else None,
            )
    return None, None, None, None


def _science_reference_from_flags(values):
    """Return the reference system encoded by a derived science calibration."""
    for meas, _obj, _frame, _iu in values:
        flags = meas.flags or {}
        ref_mag = _to_float(flags.get("reference_mag"))
        ref_color = _to_float(flags.get("reference_color_value"))
        ref_catalog = flags.get("reference_catalog_name")
        ref_band = flags.get("reference_band")
        if ref_catalog or ref_band or ref_mag is not None:
            return ref_mag, ref_color, (str(ref_catalog) if ref_catalog else None), (str(ref_band) if ref_band else None)
    return None, None, None, None

def _resolve_object_reference(
    *,
    science_ref_mag: float | None,
    science_color: float | None,
    science_ref_catalog: str | None,
    science_ref_band: str | None,
    legacy_ref_mag: float | None,
    legacy_color: float | None,
) -> tuple[float | None, float | None]:
    """Do not fall back to a different band when science provenance is explicit.

    A color-calibrated measurement may intentionally have no individual reference
    magnitude/color (for example, pivot-color fallback). In that case Gaia values
    from the identity catalog must not be substituted because they belong to a
    different photometric system.
    """
    has_explicit_science_system = bool(science_ref_catalog or science_ref_band)
    if has_explicit_science_system:
        return science_ref_mag, science_color
    return legacy_ref_mag, legacy_color

def build_rms_mag_summary(
    object_rows: Iterable[ObjectScatterRow],
    *,
    bin_width: float = 0.5,
    min_objects_per_bin: int = 3,
) -> list[dict]:
    bins: dict[int, list[ObjectScatterRow]] = defaultdict(list)
    for row in object_rows:
        if row.mag_median is None or row.mag_rms is None:
            continue
        if not _is_finite(row.mag_median) or not _is_finite(row.mag_rms):
            continue
        idx = math.floor(float(row.mag_median) / float(bin_width))
        bins[idx].append(row)

    out: list[dict] = []
    for idx in sorted(bins):
        rows = bins[idx]
        if len(rows) < int(min_objects_per_bin):
            continue
        rms_values = [float(r.mag_rms) for r in rows if r.mag_rms is not None and _is_finite(r.mag_rms)]
        mad_values = [float(r.mag_mad) for r in rows if r.mag_mad is not None and _is_finite(r.mag_mad)]
        err_values = [float(r.mag_err_median) for r in rows if r.mag_err_median is not None and _is_finite(r.mag_err_median)]
        mag_values = [float(r.mag_median) for r in rows if r.mag_median is not None and _is_finite(r.mag_median)]
        if not rms_values:
            continue
        left = idx * float(bin_width)
        right = left + float(bin_width)
        out.append(
            {
                "mag_bin_left": left,
                "mag_bin_right": right,
                "mag_bin_center": (left + right) / 2.0,
                "n_objects": len(rows),
                "mag_median": _median(mag_values),
                "rms_median": _median(rms_values),
                "rms_p10": _percentile(rms_values, 10.0),
                "rms_p90": _percentile(rms_values, 90.0),
                "mad_median": _median(mad_values),
                "mag_err_median": _median(err_values),
            }
        )
    return out


def build_variable_candidates(
    object_rows: Iterable[ObjectScatterRow],
    *,
    bin_width: float = 0.5,
    min_objects_per_bin: int = 3,
    min_measurements: int = 5,
    min_rms: float | None = None,
    min_excess: float = 2.0,
    limit: int | None = 50,
) -> list[dict]:
    rows = [r for r in object_rows if r.n_measurements >= int(min_measurements)]
    summary = build_rms_mag_summary(rows, bin_width=bin_width, min_objects_per_bin=min_objects_per_bin)
    med_by_bin = {math.floor(float(s["mag_bin_center"]) / float(bin_width)): s.get("rms_median") for s in summary}

    out: list[dict] = []
    for row in rows:
        if row.mag_median is None or row.mag_rms is None:
            continue
        if not _is_finite(row.mag_median) or not _is_finite(row.mag_rms):
            continue
        if min_rms is not None and float(row.mag_rms) < float(min_rms):
            continue
        idx = math.floor(float(row.mag_median) / float(bin_width))
        baseline = med_by_bin.get(idx)
        if baseline is None or not _is_finite(baseline) or float(baseline) <= 0:
            continue
        excess = float(row.mag_rms) / float(baseline)
        if excess < float(min_excess):
            continue
        d = row.to_dict()
        d["baseline_rms"] = float(baseline)
        d["rms_excess"] = excess
        out.append(d)
    out.sort(key=lambda d: (-(d.get("rms_excess") or 0.0), -(d.get("mag_rms") or 0.0)))
    if limit is not None:
        out = out[: int(limit)]
    return out


def compare_calibration_scatter(
    session: Session,
    *,
    left_calibration_name: str,
    right_calibration_name: str,
    catalog_name: str | None = None,
    instrument_unit_id: int | None = None,
    frame_ids: list[int] | None = None,
    min_measurements: int = 5,
    limit: int | None = 50,
) -> tuple[list[dict], dict]:
    left = load_object_scatter_rows(
        session,
        calibration_name=left_calibration_name,
        catalog_name=catalog_name,
        instrument_unit_id=instrument_unit_id,
        min_measurements=min_measurements,
    )
    right = load_object_scatter_rows(
        session,
        calibration_name=right_calibration_name,
        catalog_name=catalog_name,
        instrument_unit_id=instrument_unit_id,
        min_measurements=min_measurements,
    )

    def key(row: ObjectScatterRow) -> tuple[str | None, str | None, int]:
        return (row.catalog_name, row.source_id, row.object_id)

    left_map = {key(row): row for row in left}
    right_map = {key(row): row for row in right}
    common_keys = sorted(set(left_map) & set(right_map), key=lambda k: (k[0] or "", k[1] or "", k[2]))

    rows: list[dict] = []
    ratios: list[float] = []
    left_rms_values: list[float] = []
    right_rms_values: list[float] = []
    for k in common_keys:
        l = left_map[k]
        r = right_map[k]
        if l.mag_rms is None or r.mag_rms is None:
            continue
        if not _is_finite(l.mag_rms) or not _is_finite(r.mag_rms):
            continue
        ratio = float(r.mag_rms) / float(l.mag_rms) if float(l.mag_rms) > 0 else None
        delta = float(r.mag_rms) - float(l.mag_rms)
        if ratio is not None and _is_finite(ratio):
            ratios.append(ratio)
        left_rms_values.append(float(l.mag_rms))
        right_rms_values.append(float(r.mag_rms))
        rows.append(
            {
                "object_id": r.object_id,
                "catalog_name": r.catalog_name,
                "source_id": r.source_id,
                "mag_median": r.mag_median,
                "n_left": l.n_measurements,
                "n_right": r.n_measurements,
                "left_rms": l.mag_rms,
                "right_rms": r.mag_rms,
                "rms_ratio": ratio,
                "rms_delta": delta,
                "left_mad": l.mag_mad,
                "right_mad": r.mag_mad,
            }
        )

    rows.sort(key=lambda d: (d.get("rms_ratio") if d.get("rms_ratio") is not None else float("inf")))
    limited_rows = rows[: int(limit)] if limit is not None else rows
    summary = {
        "left_calibration_name": left_calibration_name,
        "right_calibration_name": right_calibration_name,
        "n_left_objects": len(left),
        "n_right_objects": len(right),
        "n_common_objects": len(rows),
        "left_rms_median": _median(left_rms_values),
        "right_rms_median": _median(right_rms_values),
        "rms_ratio_median": _median(ratios),
        "improved_fraction": (sum(1 for x in ratios if x < 1.0) / len(ratios)) if ratios else None,
    }
    return limited_rows, summary


def export_object_scatter_csv(rows: Iterable[ObjectScatterRow], path: str | Path) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "object_id",
        "calibration_name",
        "instrument_unit_id",
        "instrument",
        "filter_name",
        "catalog_name",
        "source_id",
        "ra_deg",
        "dec_deg",
        "n_measurements",
        "mag_mean",
        "mag_median",
        "mag_rms",
        "mag_mad",
        "mag_err_median",
        "mag_p10",
        "mag_p90",
        "robust_peak_to_peak",
        "flux_median",
        "ref_mag",
        "color",
        "mjd_min",
        "mjd_max",
    ]
    n = 0
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.to_dict())
            n += 1
    return n


def _to_float(value) -> float | None:
    try:
        out = float(value)
    except Exception:
        return None
    return out if math.isfinite(out) else None


def _first_finite_value(*values: Any) -> float | None:
    for value in values:
        parsed = _to_float(value)
        if parsed is not None:
            return parsed
    return None


def _is_finite(value) -> bool:
    try:
        return math.isfinite(float(value))
    except Exception:
        return False


def _mean(values: list[float]) -> float | None:
    return float(sum(values) / len(values)) if values else None


def _median(values: list[float]) -> float | None:
    return float(median(values)) if values else None


def _mad(values: list[float]) -> float | None:
    if not values:
        return None
    med = median(values)
    return float(median([abs(v - med) for v in values]))


def _sample_rms(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    mu = sum(values) / len(values)
    return float(math.sqrt(sum((v - mu) ** 2 for v in values) / (len(values) - 1)))


def _robust_peak_to_peak(values: list[float]) -> float | None:
    p10 = _percentile(values, 10.0)
    p90 = _percentile(values, 90.0)
    if p10 is None or p90 is None:
        return None
    return float(p90 - p10)


def _percentile(values: list[float], q: float) -> float | None:
    clean = sorted(float(v) for v in values if _is_finite(v))
    if not clean:
        return None
    if len(clean) == 1:
        return clean[0]
    pos = (len(clean) - 1) * float(q) / 100.0
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return clean[int(pos)]
    frac = pos - lo
    return clean[lo] * (1.0 - frac) + clean[hi] * frac
