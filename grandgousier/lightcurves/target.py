from __future__ import annotations

import json
import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Any

from sqlalchemy import bindparam, or_, select, text
from sqlalchemy.orm import Session

from grandgousier.db.models import (
    CalibrationVersion,
    CatalogSource,
    Frame,
    Object,
    ObjectCatalogLink,
)
from grandgousier.lightcurves.product import (
    LightCurveSelectionError,
    _load_calibration_rows,
    export_science_light_curve_csv,
    load_science_light_curve_rows,
    preflight_science_light_curve_selection,
)
from grandgousier.photometry.calibration_view import load_calibration_photometry_view


@dataclass
class TargetResolution:
    resolved: bool
    object_id: int | None = None
    source_id: str | None = None
    catalog_source_id: int | None = None
    catalog_name: str | None = None
    ra_deg: float | None = None
    dec_deg: float | None = None
    target_ra_deg: float | None = None
    target_dec_deg: float | None = None
    coordinate_provenance: str | None = None
    sep_arcsec: float | None = None
    n_measurements: int = 0
    method: str = "unresolved"
    reason: str | None = None
    nearest_object_id: int | None = None
    nearest_source_id: str | None = None
    nearest_catalog_source_id: int | None = None
    nearest_ra_deg: float | None = None
    nearest_dec_deg: float | None = None
    nearest_sep_arcsec: float | None = None
    nearest_n_measurements: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "resolved": self.resolved,
            "object_id": self.object_id,
            "source_id": self.source_id,
            "catalog_source_id": self.catalog_source_id,
            "catalog_name": self.catalog_name,
            "ra_deg": self.ra_deg,
            "dec_deg": self.dec_deg,
            "target_ra_deg": self.target_ra_deg,
            "target_dec_deg": self.target_dec_deg,
            "coordinate_provenance": self.coordinate_provenance,
            "sep_arcsec": self.sep_arcsec,
            "n_measurements": self.n_measurements,
            "method": self.method,
            "reason": self.reason,
            "nearest_object_id": self.nearest_object_id,
            "nearest_source_id": self.nearest_source_id,
            "nearest_catalog_source_id": self.nearest_catalog_source_id,
            "nearest_ra_deg": self.nearest_ra_deg,
            "nearest_dec_deg": self.nearest_dec_deg,
            "nearest_sep_arcsec": self.nearest_sep_arcsec,
            "nearest_n_measurements": self.nearest_n_measurements,
        }


@dataclass
class TargetLightCurveProduct:
    target: TargetResolution
    rows_total: int = 0
    rows_accepted: int = 0
    rows_rejected: int = 0
    mag_median: float | None = None
    mag_mean: float | None = None
    mag_rms: float | None = None
    mag_mad: float | None = None
    mag_err_median: float | None = None
    mag_err_systematic_median: float | None = None
    mag_min: float | None = None
    mag_max: float | None = None
    peak_to_peak: float | None = None
    mjd_min: float | None = None
    mjd_max: float | None = None
    csv_path: str | None = None
    json_path: str | None = None
    interseries_generation_ids: list[int] = field(default_factory=list)
    interseries_generation_scopes: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target.to_dict(),
            "rows_total": self.rows_total,
            "rows_accepted": self.rows_accepted,
            "rows_rejected": self.rows_rejected,
            "mag_median": self.mag_median,
            "mag_mean": self.mag_mean,
            "mag_rms": self.mag_rms,
            "mag_mad": self.mag_mad,
            "mag_err_median": self.mag_err_median,
            "mag_err_systematic_median": self.mag_err_systematic_median,
            "mag_min": self.mag_min,
            "mag_max": self.mag_max,
            "peak_to_peak": self.peak_to_peak,
            "mjd_min": self.mjd_min,
            "mjd_max": self.mjd_max,
            "csv_path": self.csv_path,
            "json_path": self.json_path,
            "interseries_generation_ids": list(self.interseries_generation_ids),
            "interseries_generation_scopes": list(self.interseries_generation_scopes),
            "warnings": list(self.warnings),
        }


def resolve_target_object(
    session: Session,
    *,
    calibration_name: str,
    catalog_name: str | None = None,
    frame_ids: list[int] | None = None,
    target_source_id: str | None = None,
    target_object_id: int | None = None,
    target_ra_deg: float | None = None,
    target_dec_deg: float | None = None,
    max_sep_arcsec: float | None = 120.0,
) -> TargetResolution:
    """Resolve a measured Object; frame pointing is never target identity."""

    explicit_coords = validate_target_coordinates(target_ra_deg, target_dec_deg)
    target_coords = explicit_coords
    if frame_ids is not None and not frame_ids:
        return TargetResolution(resolved=False, method="frame_scope", reason="empty frame scope")

    if (
        target_object_id is None
        and not target_source_id
        and explicit_coords is None
        and max_sep_arcsec is not None
        and float(max_sep_arcsec) < 0
    ):
        return TargetResolution(
            resolved=False,
            catalog_name=catalog_name,
            method="disabled",
            reason="target resolution disabled by negative max_sep_arcsec",
        )

    if target_object_id is None and not target_source_id and explicit_coords is None:
        return TargetResolution(
            resolved=False,
            catalog_name=catalog_name,
            method="not_requested",
            reason="no target selector was supplied",
        )

    calibration_rows = _load_calibration_rows(
        session,
        calibration_names=[str(calibration_name)],
        calibration_prefixes=[],
        model_name=None,
    )
    if len(calibration_rows) > 1:
        raise LightCurveSelectionError(
            f"Calibration name {calibration_name!r} resolved to multiple calibration products"
        )
    cv = (
        session.get(CalibrationVersion, int(calibration_rows[0]["calibration_version_id"]))
        if calibration_rows
        else None
    )
    if cv is None:
        return TargetResolution(
            resolved=False,
            method="calibration_name",
            reason=f"calibration not found: {calibration_name}",
        )

    view = load_calibration_photometry_view(
        session,
        calibration_version_id=int(cv.calibration_version_id),
        frame_ids=frame_ids,
        prefer_science=True,
    )
    filtered = [
        row
        for row in view.rows
        if not catalog_name or (row.flags or {}).get("catalog_name") == catalog_name
    ]
    identities = _identity_rows_from_photometry_view(session, filtered)
    # Use the instrument of the selected, measured Frames, never a caller's profile
    # or the field/pointing coordinates. Check even unmeasured explicitly selected frames.
    selected_ids = {int(row.frame_id) for row in view.rows}
    if frame_ids is not None:
        selected_ids.update(int(frame_id) for frame_id in frame_ids)
    instrument = _measured_scope_instrument(session, selected_ids)
    if instrument is None:
        return TargetResolution(
            resolved=False,
            catalog_name=catalog_name,
            method="frame_scope",
            reason="selected frame instruments are missing, unknown or mixed",
        )

    target_method = "explicit_object_coordinates" if explicit_coords is not None else None
    target_coordinate_provenance = (
        "explicit_object_coordinates" if explicit_coords is not None else None
    )

    source_matches: list[tuple[int, int, float, float]] = []
    catalog_coords: tuple[float, float] | None = None
    object_coords: tuple[float, float] | None = None
    catalog_sep_arcsec: float | None = None
    if target_source_id:
        if not catalog_name:
            return TargetResolution(
                resolved=False, source_id=str(target_source_id), method="target_source_id",
                reason="catalog_name is required to scope target_source_id",
            )
        source_matches = session.execute(
            select(
                ObjectCatalogLink.object_id, CatalogSource.catalog_source_id,
                CatalogSource.ra_deg, CatalogSource.dec_deg,
            )
            .join(CatalogSource, ObjectCatalogLink.catalog_source_id == CatalogSource.catalog_source_id)
            .where(
                CatalogSource.catalog_name == catalog_name,
                CatalogSource.source_id == str(target_source_id),
            )
        ).all()
        if len(source_matches) != 1 or int(source_matches[0][0]) not in identities:
            return TargetResolution(
                resolved=False, source_id=str(target_source_id), catalog_name=catalog_name,
                method="target_source_id",
                reason="target source has no unique catalog link to a measured object in scope",
            )
        object_id, catalog_source_id, catalog_ra, catalog_dec = source_matches[0]
        object_row = identities[int(object_id)]
        if any(
            int(row.object_id) == int(object_id)
            and not _measured_source_agrees(
                row.flags, source_id=str(target_source_id),
                catalog_source_id=int(catalog_source_id),
            )
            for row in filtered
        ):
            return TargetResolution(
                resolved=False, source_id=str(target_source_id), catalog_name=catalog_name,
                method="unresolved", reason="measured source flags contradict the catalog link",
            )
        catalog_coords, catalog_position_error = _coordinate_pair_status(
            catalog_ra, catalog_dec
        )
        object_coords, object_position_error = _coordinate_pair_status(
            object_row.get("ra_deg"), object_row.get("dec_deg")
        )
        if catalog_position_error or object_position_error:
            return TargetResolution(
                resolved=False, source_id=str(target_source_id), catalog_name=catalog_name,
                method="unresolved",
                reason="catalog or object sky coordinates are partially populated or invalid",
            )
        if catalog_coords is not None and object_coords is not None:
            catalog_sep_arcsec = _sky_separation_arcsec(*catalog_coords, *object_coords)
            # No epoch propagation is justified here. Refuse distant links rather than
            # assuming distinct catalog releases or epochs describe one sky position.
            if catalog_sep_arcsec > 2.0:
                return TargetResolution(
                    resolved=False, source_id=str(target_source_id), catalog_name=catalog_name,
                    method="unresolved", reason="catalog and object positions differ by over 2 arcsec",
                )
        if target_coords is not None:
            identity_coords = catalog_coords or object_coords
            if identity_coords is None:
                return TargetResolution(
                    resolved=False,
                    source_id=str(target_source_id),
                    catalog_name=catalog_name,
                    target_ra_deg=target_coords[0],
                    target_dec_deg=target_coords[1],
                    coordinate_provenance=target_coordinate_provenance,
                    method="unresolved",
                    reason="cannot compare explicit target coordinates: catalog and object positions are unavailable",
                )
            if _sky_separation_arcsec(*target_coords, *identity_coords) > 2.0:
                return TargetResolution(
                    resolved=False, source_id=str(target_source_id), catalog_name=catalog_name,
                    target_ra_deg=target_coords[0], target_dec_deg=target_coords[1],
                    coordinate_provenance=target_coordinate_provenance, method="unresolved",
                    reason="explicit target coordinates disagree with the catalog position",
                )

    if target_object_id is not None and target_source_id and int(target_object_id) != int(source_matches[0][0]):
        return TargetResolution(
            resolved=False, method="conflicting_target_overrides",
            reason="target_object_id disagrees with scoped target_source_id",
        )
    selected_id = (
        int(target_object_id) if target_object_id is not None
        else int(source_matches[0][0]) if source_matches else None
    )
    selected_row = identities.get(selected_id) if selected_id is not None else None
    if selected_id is not None and selected_row is None:
        return TargetResolution(
            resolved=False,
            object_id=selected_id,
            catalog_name=catalog_name,
            method="target_object_id" if target_object_id is not None else "catalog_identifier",
            reason="target object has no measurements in selected calibration/frame set",
        )
    if selected_row is not None and target_object_id is not None and not target_source_id:
        object_coords, object_position_error = _coordinate_pair_status(
            selected_row.get("ra_deg"), selected_row.get("dec_deg")
        )
        if object_position_error:
            return TargetResolution(
                resolved=False,
                object_id=selected_id,
                method="target_object_id",
                reason="explicit object has partially populated or invalid sky coordinates",
            )

    identity_coords = catalog_coords or object_coords

    coordinate_matches: list[dict[str, Any]] = []
    nearest: dict[str, Any] | None = None
    identity_coordinate_sep: float | None = None
    if target_coords is not None:
        ra, dec = target_coords
        candidates = []
        for item in identities.values():
            if not _valid_sky_position(item.get("ra_deg"), item.get("dec_deg")):
                continue
            row = dict(item)
            row["sep_arcsec"] = _sky_separation_arcsec(
                ra, dec, float(row["ra_deg"]), float(row["dec_deg"])
            )
            candidates.append(row)
        candidates.sort(key=lambda row: (row["sep_arcsec"], row["object_id"]))
        nearest = candidates[0] if candidates else None
        # The established unique-object radius is independent of max_sep_arcsec.
        coordinate_matches = [row for row in candidates if row["sep_arcsec"] <= 2.0]
        if len(coordinate_matches) > 1:
            return TargetResolution(
                resolved=False, catalog_name=catalog_name,
                target_ra_deg=ra, target_dec_deg=dec,
                coordinate_provenance=target_coordinate_provenance,
                method=target_method or "explicit_object_coordinates",
                reason="multiple measured objects within 2 arcsec of target coordinates",
                nearest_object_id=nearest["object_id"] if nearest else None,
                nearest_source_id=nearest.get("source_id") if nearest else None,
                nearest_catalog_source_id=nearest.get("catalog_source_id") if nearest else None,
                nearest_ra_deg=nearest.get("ra_deg") if nearest else None,
                nearest_dec_deg=nearest.get("dec_deg") if nearest else None,
                nearest_sep_arcsec=nearest["sep_arcsec"] if nearest else None,
                nearest_n_measurements=nearest.get("n_measurements") if nearest else None,
            )
        if coordinate_matches and selected_id is not None:
            if coordinate_matches[0]["object_id"] != selected_id:
                return TargetResolution(
                    resolved=False, target_ra_deg=ra, target_dec_deg=dec,
                    coordinate_provenance=target_coordinate_provenance,
                    method="conflicting_target_overrides",
                    reason="explicit target identity disagrees with target coordinates",
                )
        elif not coordinate_matches:
            if selected_id is None:
                return TargetResolution(
                    resolved=False, catalog_name=catalog_name,
                    target_ra_deg=ra, target_dec_deg=dec,
                    coordinate_provenance=target_coordinate_provenance,
                    method=target_method or "explicit_object_coordinates",
                    reason="no measured object within 2 arcsec of target coordinates",
                    nearest_object_id=nearest["object_id"] if nearest else None,
                    nearest_source_id=nearest.get("source_id") if nearest else None,
                    nearest_catalog_source_id=nearest.get("catalog_source_id") if nearest else None,
                    nearest_ra_deg=nearest.get("ra_deg") if nearest else None,
                    nearest_dec_deg=nearest.get("dec_deg") if nearest else None,
                    nearest_sep_arcsec=nearest["sep_arcsec"] if nearest else None,
                    nearest_n_measurements=nearest.get("n_measurements") if nearest else None,
                )
            if identity_coords is None:
                return TargetResolution(
                    resolved=False, object_id=selected_id,
                    source_id=str(target_source_id) if target_source_id else None,
                    catalog_name=catalog_name, target_ra_deg=ra, target_dec_deg=dec,
                    coordinate_provenance=target_coordinate_provenance,
                    method=target_method or "target_object_id",
                    reason="cannot compare explicit target coordinates with identity: sky coordinates are unavailable",
                    nearest_object_id=nearest["object_id"] if nearest else None,
                    nearest_source_id=nearest.get("source_id") if nearest else None,
                    nearest_catalog_source_id=nearest.get("catalog_source_id") if nearest else None,
                    nearest_ra_deg=nearest.get("ra_deg") if nearest else None,
                    nearest_dec_deg=nearest.get("dec_deg") if nearest else None,
                    nearest_sep_arcsec=nearest["sep_arcsec"] if nearest else None,
                    nearest_n_measurements=nearest.get("n_measurements") if nearest else None,
                )
            identity_coordinate_sep = _sky_separation_arcsec(
                *target_coords, *identity_coords
            )
            if identity_coordinate_sep > 2.0:
                return TargetResolution(
                    resolved=False, object_id=selected_id,
                    source_id=str(target_source_id) if target_source_id else None,
                    catalog_name=catalog_name, target_ra_deg=ra, target_dec_deg=dec,
                    coordinate_provenance=target_coordinate_provenance,
                    method="conflicting_target_overrides",
                    reason="explicit target coordinates disagree with the selected identity",
                    nearest_object_id=nearest["object_id"] if nearest else None,
                    nearest_source_id=nearest.get("source_id") if nearest else None,
                    nearest_catalog_source_id=nearest.get("catalog_source_id") if nearest else None,
                    nearest_ra_deg=nearest.get("ra_deg") if nearest else None,
                    nearest_dec_deg=nearest.get("dec_deg") if nearest else None,
                    nearest_sep_arcsec=nearest["sep_arcsec"] if nearest else None,
                    nearest_n_measurements=nearest.get("n_measurements") if nearest else None,
                )

    if selected_id is not None:
        row = dict(selected_row)
        row["sep_arcsec"] = (
            coordinate_matches[0]["sep_arcsec"] if coordinate_matches
            else catalog_sep_arcsec if catalog_sep_arcsec is not None
            else identity_coordinate_sep
        )
        if source_matches:
            row.update(source_id=str(target_source_id), catalog_name=catalog_name,
                       catalog_source_id=int(source_matches[0][1]))
        res = _resolution_from_row(row, method=("target_object_id" if target_object_id is not None
                                                else "catalog_identifier"))
        if target_coords is not None:
            res.target_ra_deg, res.target_dec_deg = target_coords
            res.coordinate_provenance = target_coordinate_provenance
        elif catalog_coords is not None:
            res.target_ra_deg, res.target_dec_deg = catalog_coords
            res.coordinate_provenance = "catalog_identifier"
        elif object_coords is not None:
            res.target_ra_deg = float(row["ra_deg"])
            res.target_dec_deg = float(row["dec_deg"])
            res.coordinate_provenance = "object_record"
        return res

    if coordinate_matches:
        res = _resolution_from_row(
            coordinate_matches[0], method=target_method or "explicit_object_coordinates"
        )
        res.target_ra_deg, res.target_dec_deg = target_coords
        res.coordinate_provenance = target_coordinate_provenance
        return res

    return TargetResolution(
        resolved=False, catalog_name=catalog_name, method="unresolved",
        reason="RoboPhot target identity is not supplied; frame center is pointing only",
    )


def _measured_source_agrees(
    flags: Any, *, source_id: str, catalog_source_id: int
) -> bool:
    if not isinstance(flags, dict):
        return True
    if flags.get("source_id") is not None and str(flags["source_id"]) != source_id:
        return False
    stored_id = flags.get("catalog_source_id")
    return (stored_id is None
            or (not isinstance(stored_id, bool) and isinstance(stored_id, (int, str))
                and str(stored_id).strip().isdigit()
                and int(str(stored_id).strip()) == catalog_source_id))


def validate_target_coordinates(
    ra_deg: float | None, dec_deg: float | None
) -> tuple[float, float] | None:
    """Validate paired ICRS-degree input before any archive/pipeline writes."""
    if ra_deg is None and dec_deg is None:
        return None
    if ra_deg is None or dec_deg is None:
        raise ValueError("target RA and Dec must be supplied together in degrees")
    ra, dec = float(ra_deg), float(dec_deg)
    if not _valid_sky_position(ra, dec):
        raise ValueError("target RA must be finite in [0, 360), Dec finite in [-90, 90] degrees")
    return ra, dec


def resolve_explicit_target_catalog_source(
    session: Session,
    *,
    catalog_name: str,
    target_source_id: str | None = None,
    target_object_id: int | None = None,
    target_ra_deg: float | None = None,
    target_dec_deg: float | None = None,
) -> dict[str, Any]:
    """Select an explicit target's unique catalog source before photometry.

    This read-only pre-photometry lookup never creates an Object or
    ObjectCatalogLink. Source-ID and coordinate selectors can select an existing
    CatalogSource without an identity link so it can enter forced photometry.
    Object-ID selection is deliberately stricter: it must already identify one
    unique linked CatalogSource in the requested namespace.
    """

    target_coords = validate_target_coordinates(target_ra_deg, target_dec_deg)
    if target_object_id is None and not target_source_id and target_coords is None:
        return {"eligible": False, "reason": "no explicit target selector"}
    if not catalog_name:
        return {"eligible": False, "reason": "catalog namespace is required"}

    statement = select(
        CatalogSource.catalog_source_id,
        CatalogSource.source_id,
        CatalogSource.ra_deg,
        CatalogSource.dec_deg,
        ObjectCatalogLink.object_id,
    ).select_from(CatalogSource)
    if target_object_id is not None:
        # Check uniqueness over all links in this namespace before applying
        # any other selector. Coordinates/source IDs must not astrometrically
        # guess which of several links represents an opaque object ID.
        statement = statement.join(
            ObjectCatalogLink,
            ObjectCatalogLink.catalog_source_id == CatalogSource.catalog_source_id,
        ).where(ObjectCatalogLink.object_id == int(target_object_id))
    else:
        statement = statement.outerjoin(
            ObjectCatalogLink,
            ObjectCatalogLink.catalog_source_id == CatalogSource.catalog_source_id,
        )
    statement = statement.where(CatalogSource.catalog_name == str(catalog_name))

    if target_object_id is None and target_source_id:
        statement = statement.where(CatalogSource.source_id == str(target_source_id))
    elif target_object_id is None and target_coords is not None:
        ra, dec = target_coords
        dec_radius = 2.0 / 3600.0
        dec_low = max(-90.0, dec - dec_radius)
        dec_high = min(90.0, dec + dec_radius)
        cos_dec_min = min(
            abs(math.cos(math.radians(dec_low))),
            abs(math.cos(math.radians(dec_high))),
        )
        ra_radius = dec_radius / max(cos_dec_min, 1e-12)
        statement = statement.where(
            CatalogSource.dec_deg >= dec_low,
            CatalogSource.dec_deg <= dec_high,
        )
        if ra_radius < 180.0:
            low_ra = (ra - ra_radius) % 360.0
            high_ra = (ra + ra_radius) % 360.0
            ra_predicate = (
                (CatalogSource.ra_deg >= low_ra)
                & (CatalogSource.ra_deg <= high_ra)
                if low_ra <= high_ra
                else or_(CatalogSource.ra_deg >= low_ra, CatalogSource.ra_deg <= high_ra)
            )
            statement = statement.where(ra_predicate)

    rows = list(session.execute(statement).all())
    if target_object_id is not None:
        rows = [
            row
            for row in rows
            if row[4] is not None and int(row[4]) == int(target_object_id)
        ]
    if target_object_id is not None and len(rows) != 1:
        reason = (
            "target object has multiple catalog source links in the selected namespace"
            if len(rows) > 1
            else "target object has no unique catalog source link in the selected namespace"
        )
        return {"eligible": False, "reason": reason, "candidate_count": len(rows)}

    if target_source_id:
        rows = [row for row in rows if str(row[1]) == str(target_source_id)]
    if target_coords is not None:
        rows = [
            row
            for row in rows
            if _valid_sky_position(row[2], row[3])
            and _sky_separation_arcsec(
                target_coords[0], target_coords[1], float(row[2]), float(row[3])
            ) <= 2.0
        ]
    if len(rows) != 1:
        reason = (
            "explicit target matches multiple catalog sources within 2 arcsec"
            if len(rows) > 1 and target_coords is not None
            else "explicit target does not map to exactly one catalog source"
        )
        return {"eligible": False, "reason": reason, "candidate_count": len(rows)}

    row = rows[0]
    if target_coords is not None and _sky_separation_arcsec(
        target_coords[0], target_coords[1], float(row[2]), float(row[3])
    ) > 2.0:
        return {
            "eligible": False,
            "reason": "explicit target coordinates disagree with catalog source",
        }
    return {
        "eligible": True,
        "catalog_name": str(catalog_name),
        "catalog_source_id": int(row[0]),
        "source_id": str(row[1]),
        "object_id": int(row[4]) if row[4] is not None else None,
        "identity_link_exists": row[4] is not None,
        "ra_deg": float(row[2]) if _is_finite(row[2]) else None,
        "dec_deg": float(row[3]) if _is_finite(row[3]) else None,
        "coordinate_match_radius_arcsec": 2.0 if target_coords is not None else None,
    }


def _valid_sky_position(ra: Any, dec: Any) -> bool:
    return (_is_finite(ra) and _is_finite(dec)
            and 0 <= float(ra) < 360 and -90 <= float(dec) <= 90)


def _coordinate_pair_status(
    ra: Any, dec: Any
) -> tuple[tuple[float, float] | None, bool]:
    if ra is None and dec is None:
        return None, False
    if not _valid_sky_position(ra, dec):
        return None, True
    return (float(ra), float(dec)), False


def _sky_separation_arcsec(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
    ddec = math.radians(dec2 - dec1)
    dra = math.radians((ra2 - ra1 + 180) % 360 - 180)
    a = (math.sin(ddec / 2) ** 2
         + math.cos(math.radians(dec1)) * math.cos(math.radians(dec2))
         * math.sin(dra / 2) ** 2)
    return math.degrees(2 * math.atan2(math.sqrt(min(1.0, a)),
                                        math.sqrt(max(0.0, 1 - a)))) * 3600


def _measured_scope_instrument(session: Session, frame_ids: set[int]) -> str | None:
    if not frame_ids:
        return None
    rows = session.execute(
        select(Frame.frame_id, Frame.instrument).where(Frame.frame_id.in_(sorted(frame_ids)))
    ).all()
    if (len(rows) != len(frame_ids) or
            {int(row[0]) for row in rows} != frame_ids):
        return None
    instruments = {row[1] for row in rows}
    return next(iter(instruments)) if len(instruments) == 1 and instruments <= {"MASTER", "RoboPhot"} else None


def _identity_rows_from_photometry_view(
    session: Session, rows: Iterable[Any]
) -> dict[int, dict[str, Any]]:
    rows = list(rows)
    object_ids = sorted({int(row.object_id) for row in rows})
    objects = {
        int(obj.object_id): obj
        for obj in session.scalars(select(Object).where(Object.object_id.in_(object_ids))).all()
    } if object_ids else {}
    grouped: dict[int, dict[str, Any]] = {}
    frames_by_object: dict[int, set[int]] = {}
    for measurement in rows:
        object_id = int(measurement.object_id)
        obj = objects.get(object_id)
        if obj is None:
            continue
        flags = measurement.flags if isinstance(measurement.flags, dict) else {}
        item = grouped.setdefault(
            object_id,
            {
                "object_id": object_id,
                "ra_deg": obj.ra_deg,
                "dec_deg": obj.dec_deg,
                "catalog_name": flags.get("catalog_name"),
                "source_id": flags.get("source_id"),
                "catalog_source_id": _int_or_none(flags.get("catalog_source_id")),
                "n_measurements": 0,
            },
        )
        if item.get("source_id") is None and flags.get("source_id") is not None:
            item["source_id"] = flags.get("source_id")
        if item.get("catalog_source_id") is None:
            item["catalog_source_id"] = _int_or_none(flags.get("catalog_source_id"))
        frames_by_object.setdefault(object_id, set()).add(int(measurement.frame_id))
    for object_id, frame_set in frames_by_object.items():
        grouped[object_id]["n_measurements"] = len(frame_set)
    return grouped


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None and str(value).strip() else None
    except (TypeError, ValueError):
        return None


def cleanup_target_light_curve_artifacts(
    *,
    out_dir: str | Path | None,
    calibration_prefix: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Remove target products that are not valid for the current pipeline run.

    Target files live in the series output root rather than an atomically replaced
    subdirectory.  A rerun with target export disabled or with an unresolved target
    must therefore delete earlier CSV/JSON/PNG products explicitly.
    """

    paths: list[Path] = []
    if out_dir:
        root = Path(out_dir)
        stem = f"{calibration_prefix}_target_light_curve"
        paths = [root / f"{stem}{suffix}" for suffix in (".csv", ".json", ".png")]

    existing = [path for path in paths if path.exists()]
    removed: list[str] = []
    if not dry_run:
        for path in existing:
            path.unlink()
            removed.append(str(path))

    return {
        "out_dir": str(out_dir) if out_dir else None,
        "calibration_prefix": calibration_prefix,
        "dry_run": bool(dry_run),
        "candidate_paths": [str(path) for path in paths],
        "existing_artifacts": len(existing),
        "removed_artifacts": len(existing) if not dry_run else 0,
        "removed_paths": removed,
    }


def export_target_light_curve_product(
    session: Session,
    *,
    target: TargetResolution,
    calibration_name: str,
    catalog_name: str | None = None,
    series_id: int | None = None,
    frame_ids: list[int] | None = None,
    interseries_generation_ids: list[int] | None = None,
    reject_flags: set[str] | None = None,
    out_csv: str | Path | None = None,
    out_json: str | Path | None = None,
) -> TargetLightCurveProduct:
    product = TargetLightCurveProduct(target=target)
    if not target.resolved or target.object_id is None:
        product.warnings.append(target.reason or "target object was not resolved")
        if out_json:
            product.json_path = str(out_json)
            _write_json(product.to_dict(), out_json)
        return product

    scope_series_id = None if frame_ids else series_id
    selection_scope = preflight_science_light_curve_selection(
        session,
        calibration_names=[calibration_name],
        catalog_name=catalog_name,
        series_id=scope_series_id,
        frame_ids=frame_ids,
        interseries_generation_ids=interseries_generation_ids,
    )
    product.interseries_generation_ids = list(
        getattr(selection_scope, "interseries_generation_ids", []) or []
    )
    product.interseries_generation_scopes = list(
        getattr(selection_scope, "interseries_generation_scopes", []) or []
    )
    product.warnings.extend(getattr(selection_scope, "warnings", []) or [])

    # ``process-archive-path`` passes both the series id and an explicit list of
    # frames that came from the requested filesystem path.  The light-curve scope
    # resolver intentionally rejects combining field-series selectors with
    # explicit frame_ids, because the combination is ambiguous for public CLI
    # calls.  For target export the explicit path frame selection is the stricter
    # and safer scope, so prefer it and do not also pass series_id downstream.
    scope, rows = load_science_light_curve_rows(
        session,
        calibration_names=[calibration_name],
        catalog_name=catalog_name,
        object_id=int(target.object_id),
        series_id=scope_series_id,
        frame_ids=frame_ids,
        interseries_generation_ids=(
            interseries_generation_ids
            if interseries_generation_ids is not None
            else (product.interseries_generation_ids or None)
        ),
        reject_flags=reject_flags,
        exclude_rejected=False,
    )
    if (
        scope is not None
        and (
            list(getattr(scope, "interseries_generation_ids", []) or [])
            != product.interseries_generation_ids
            or list(getattr(scope, "interseries_generation_scopes", []) or [])
            != product.interseries_generation_scopes
        )
    ):
        raise LightCurveSelectionError(
            "M4 generation selection changed after target-export preflight; refusing output"
        )
    product.rows_total = len(rows)
    product.rows_accepted = sum(1 for r in rows if not r.get("is_rejected"))
    product.rows_rejected = sum(1 for r in rows if r.get("is_rejected"))
    mags = [float(r["mag_global"]) for r in rows if _is_finite(r.get("mag_global"))]
    errs = [float(r["mag_global_err_random"]) for r in rows if _is_finite(r.get("mag_global_err_random"))]
    systematic_errs = [float(r["mag_global_err_systematic"]) for r in rows if _is_finite(r.get("mag_global_err_systematic"))]
    mjds = [float(r["mjd"]) for r in rows if _is_finite(r.get("mjd"))]
    product.mag_median = _median(mags)
    product.mag_mean = _mean(mags)
    product.mag_rms = _sample_rms(mags)
    product.mag_mad = _mad(mags)
    product.mag_err_median = _median(errs)
    product.mag_err_systematic_median = _median(systematic_errs)
    product.mag_min = min(mags) if mags else None
    product.mag_max = max(mags) if mags else None
    product.peak_to_peak = (max(mags) - min(mags)) if mags else None
    product.mjd_min = min(mjds) if mjds else None
    product.mjd_max = max(mjds) if mjds else None

    if out_csv:
        export_science_light_curve_csv(rows, out_csv)
        product.csv_path = str(out_csv)
    if out_json:
        product.json_path = str(out_json)
        _write_json(product.to_dict(), out_json)
    return product


def _load_object_measurement_row(
    session: Session,
    *,
    calibration_version_id: int,
    object_id: int,
    catalog_name: str | None,
    frame_ids: list[int] | None,
) -> dict[str, Any] | None:
    where = ["m.calibration_version_id = :cv_id", "m.object_id = :object_id"]
    params: dict[str, Any] = {"cv_id": calibration_version_id, "object_id": object_id}
    return _load_measurement_identity_row(session, where=where, params=params, catalog_name=catalog_name, frame_ids=frame_ids)


def _load_source_measurement_row(
    session: Session,
    *,
    calibration_version_id: int,
    source_id: str,
    catalog_name: str | None,
    frame_ids: list[int] | None,
) -> dict[str, Any] | None:
    where = ["m.calibration_version_id = :cv_id", "m.flags ->> 'source_id' = :source_id"]
    params: dict[str, Any] = {"cv_id": calibration_version_id, "source_id": str(source_id)}
    return _load_measurement_identity_row(session, where=where, params=params, catalog_name=catalog_name, frame_ids=frame_ids)


def _load_nearest_measurement_row(
    session: Session,
    *,
    calibration_version_id: int,
    target_ra_deg: float,
    target_dec_deg: float,
    catalog_name: str | None,
    frame_ids: list[int] | None,
) -> dict[str, Any] | None:
    where = ["m.calibration_version_id = :cv_id"]
    params: dict[str, Any] = {
        "cv_id": calibration_version_id,
        "target_ra": float(target_ra_deg),
        "target_dec": float(target_dec_deg),
    }
    if catalog_name:
        where.append("m.flags ->> 'catalog_name' = :catalog_name")
        params["catalog_name"] = catalog_name
    if frame_ids:
        where.append("m.frame_id IN :frame_ids")
        params["frame_ids"] = list(frame_ids)
    sql = f"""
        SELECT
            m.object_id,
            o.ra_deg,
            o.dec_deg,
            m.flags ->> 'catalog_name' AS catalog_name,
            m.flags ->> 'source_id' AS source_id,
            MIN(NULLIF(m.flags ->> 'catalog_source_id', '')::bigint) AS catalog_source_id,
            COUNT(DISTINCT m.frame_id) AS n_measurements,
            SQRT(
                POWER((o.ra_deg - :target_ra) * COS(RADIANS(:target_dec)), 2)
                + POWER(o.dec_deg - :target_dec, 2)
            ) * 3600.0 AS sep_arcsec
        FROM measurements m
        JOIN objects o ON o.object_id = m.object_id
        WHERE {' AND '.join(where)}
        GROUP BY m.object_id, o.ra_deg, o.dec_deg, m.flags ->> 'catalog_name', m.flags ->> 'source_id'
        ORDER BY sep_arcsec ASC, n_measurements DESC, m.object_id
        LIMIT 1
    """
    stmt = text(sql)
    if frame_ids:
        stmt = stmt.bindparams(bindparam("frame_ids", expanding=True))
    row = session.execute(stmt, params).mappings().first()
    return dict(row) if row is not None else None


def _load_measurement_identity_row(
    session: Session,
    *,
    where: list[str],
    params: dict[str, Any],
    catalog_name: str | None,
    frame_ids: list[int] | None,
) -> dict[str, Any] | None:
    if catalog_name:
        where.append("m.flags ->> 'catalog_name' = :catalog_name")
        params["catalog_name"] = catalog_name
    if frame_ids:
        where.append("m.frame_id IN :frame_ids")
        params["frame_ids"] = list(frame_ids)
    sql = f"""
        SELECT
            m.object_id,
            o.ra_deg,
            o.dec_deg,
            m.flags ->> 'catalog_name' AS catalog_name,
            m.flags ->> 'source_id' AS source_id,
            MIN(NULLIF(m.flags ->> 'catalog_source_id', '')::bigint) AS catalog_source_id,
            COUNT(DISTINCT m.frame_id) AS n_measurements,
            0.0::double precision AS sep_arcsec
        FROM measurements m
        JOIN objects o ON o.object_id = m.object_id
        WHERE {' AND '.join(where)}
        GROUP BY m.object_id, o.ra_deg, o.dec_deg, m.flags ->> 'catalog_name', m.flags ->> 'source_id'
        ORDER BY n_measurements DESC, m.object_id
        LIMIT 1
    """
    stmt = text(sql)
    if frame_ids:
        stmt = stmt.bindparams(bindparam("frame_ids", expanding=True))
    row = session.execute(stmt, params).mappings().first()
    return dict(row) if row is not None else None


def _resolution_from_row(row: dict[str, Any], *, method: str) -> TargetResolution:
    return TargetResolution(
        resolved=True,
        object_id=int(row["object_id"]) if row.get("object_id") is not None else None,
        source_id=str(row["source_id"]) if row.get("source_id") is not None else None,
        catalog_source_id=int(row["catalog_source_id"]) if row.get("catalog_source_id") is not None else None,
        catalog_name=str(row["catalog_name"]) if row.get("catalog_name") is not None else None,
        ra_deg=float(row["ra_deg"]) if row.get("ra_deg") is not None else None,
        dec_deg=float(row["dec_deg"]) if row.get("dec_deg") is not None else None,
        sep_arcsec=float(row["sep_arcsec"]) if row.get("sep_arcsec") is not None else None,
        n_measurements=int(row["n_measurements"] or 0),
        method=method,
    )


def _write_json(data: dict[str, Any], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _is_finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except Exception:
        return False


def _mean(values: Iterable[float]) -> float | None:
    vals = [float(v) for v in values if _is_finite(v)]
    return sum(vals) / len(vals) if vals else None


def _median(values: Iterable[float]) -> float | None:
    vals = [float(v) for v in values if _is_finite(v)]
    return median(vals) if vals else None


def _sample_rms(values: Iterable[float]) -> float | None:
    vals = [float(v) for v in values if _is_finite(v)]
    if len(vals) < 2:
        return None
    mu = sum(vals) / len(vals)
    return math.sqrt(sum((v - mu) ** 2 for v in vals) / (len(vals) - 1))


def _mad(values: Iterable[float]) -> float | None:
    vals = [float(v) for v in values if _is_finite(v)]
    if not vals:
        return None
    med = median(vals)
    return median(abs(v - med) for v in vals)
