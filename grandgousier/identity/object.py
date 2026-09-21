from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from grandgousier.db.models import CatalogSource, Object, ObjectCatalogLink

# Conservative automatic association.  This is deliberately much tighter than
# the typical MASTER/RoboPhot seeing disk: catalog changes may reuse an existing
# Object only when there is one unambiguous astrometric candidate.
OBJECT_ASSOCIATION_RADIUS_ARCSEC = 0.75


@dataclass(frozen=True, slots=True)
class ObjectAssociationResult:
    object_id: int | None
    object_uid: str | None
    method: str
    separation_arcsec: float | None = None
    created: bool = False


def new_object_uid() -> str:
    """Create a catalog-independent opaque identity for one internal sky object."""

    return f"obj_{uuid.uuid4().hex}"


def get_or_create_object_for_catalog_source(
    session: Session,
    source: CatalogSource,
    *,
    dry_run: bool = False,
    association_radius_arcsec: float = OBJECT_ASSOCIATION_RADIUS_ARCSEC,
) -> ObjectAssociationResult:
    """Resolve a catalog source to the stable internal Object identity.

    Policy, in order:
      1. exact existing ``object_catalog_links`` row;
      2. exactly one astrometric Object within the conservative radius after
         propagating its stored position to the source epoch when possible,
         excluding Objects already linked to another source in the same catalog;
      3. create a new opaque internal UUID and link the catalog source to it.

    Multiple nearby candidates are never auto-merged.  A new Object is created
    instead, preserving the evidence for later explicit association/merge work.
    """

    existing_link = session.scalar(
        select(ObjectCatalogLink).where(
            ObjectCatalogLink.catalog_source_id == int(source.catalog_source_id)
        )
    )
    if existing_link is not None:
        obj = session.get(Object, int(existing_link.object_id))
        if obj is None:
            raise RuntimeError(
                "object_catalog_links points to missing object: "
                f"catalog_source_id={source.catalog_source_id}"
            )
        return ObjectAssociationResult(
            object_id=int(obj.object_id),
            object_uid=str(obj.object_uid),
            method="EXISTING_CATALOG_LINK",
            separation_arcsec=existing_link.separation_arcsec,
            created=False,
        )

    candidates = _astrometric_candidates(
        session,
        ra_deg=float(source.ra_deg),
        dec_deg=float(source.dec_deg),
        epoch=_finite_or_none(source.epoch),
        pmra_masyr=_finite_or_none(source.pmra_masyr),
        pmdec_masyr=_finite_or_none(source.pmdec_masyr),
        radius_arcsec=float(association_radius_arcsec),
        source_catalog_name=str(source.catalog_name),
    )
    if len(candidates) == 1:
        obj, separation = candidates[0]
        if dry_run:
            return ObjectAssociationResult(
                object_id=int(obj.object_id),
                object_uid=str(obj.object_uid),
                method="ASTROMETRIC_NEAREST_UNIQUE",
                separation_arcsec=separation,
                created=False,
            )
        link = _make_link(
            obj=obj,
            source=source,
            method="ASTROMETRIC_NEAREST_UNIQUE",
            separation_arcsec=separation,
            comparison_epoch=_finite_or_none(source.epoch),
            primary=not bool(obj.catalog_links),
            evidence={
                "association_radius_arcsec": float(association_radius_arcsec),
                "candidate_count_within_radius": 1,
                "proper_motion_propagation": _propagation_evidence(obj, source.epoch),
            },
        )
        session.add(link)
        session.flush()
        return ObjectAssociationResult(
            object_id=int(obj.object_id),
            object_uid=str(obj.object_uid),
            method="ASTROMETRIC_NEAREST_UNIQUE",
            separation_arcsec=separation,
            created=False,
        )

    if dry_run:
        return ObjectAssociationResult(
            object_id=None,
            object_uid=None,
            method="WOULD_CREATE_NEW_OBJECT_AMBIGUOUS" if candidates else "WOULD_CREATE_NEW_OBJECT",
            created=False,
        )

    object_uid = new_object_uid()
    obj = Object(
        object_uid=object_uid,
        identity_scheme="INTERNAL_UUID_V1",
        ra_deg=float(source.ra_deg),
        dec_deg=float(source.dec_deg),
        reference_epoch=_finite_or_none(source.epoch),
        ref_mags=dict(source.ref_mags or {}),
        colors=dict(source.colors or {}),
        pmra_masyr=_finite_or_none(source.pmra_masyr),
        pmdec_masyr=_finite_or_none(source.pmdec_masyr),
        object_flags={
            "created_from_catalog_source_id": int(source.catalog_source_id),
            "object_identity_policy": "catalog_link_then_unique_astrometric_v1",
            "ambiguous_nearby_objects": [int(row.object_id) for row, _sep in candidates],
        },
    )
    session.add(obj)
    session.flush()
    link = _make_link(
        obj=obj,
        source=source,
        method="CREATED_FROM_CATALOG_SOURCE",
        separation_arcsec=0.0,
        comparison_epoch=_finite_or_none(source.epoch),
        primary=True,
        evidence={
            "association_radius_arcsec": float(association_radius_arcsec),
            "candidate_count_within_radius": len(candidates),
            "reason": "no_unique_existing_object_within_radius",
        },
    )
    session.add(link)
    session.flush()
    return ObjectAssociationResult(
        object_id=int(obj.object_id),
        object_uid=object_uid,
        method="CREATED_FROM_CATALOG_SOURCE",
        separation_arcsec=0.0,
        created=True,
    )


def preferred_catalog_identity_for_object(
    session: Session,
    object_id: int,
) -> tuple[str | None, str | None]:
    """Return a deterministic external identity without storing it on Object."""

    rows = list(
        session.execute(
            select(
                ObjectCatalogLink.is_primary,
                CatalogSource.catalog_name,
                CatalogSource.source_id,
            )
            .join(CatalogSource, CatalogSource.catalog_source_id == ObjectCatalogLink.catalog_source_id)
            .where(ObjectCatalogLink.object_id == int(object_id))
        ).all()
    )
    if not rows:
        return None, None

    def rank(row) -> tuple[int, int, str, str]:
        is_primary, catalog_name, source_id = row
        name = str(catalog_name)
        lname = name.casefold()
        gaia_rank = 0 if lname in {"gaia_dr3", "gaia_dr3_master", "gaia", "gaiadr3"} else 1
        # Reference-photometry lookup historically preferred Gaia identity.
        # Primary-link status breaks ties within the same catalog class.
        return (gaia_rank, 0 if bool(is_primary) else 1, lname, str(source_id))

    _primary, catalog_name, source_id = sorted(rows, key=rank)[0]
    return str(catalog_name), str(source_id)


def _make_link(
    *,
    obj: Object,
    source: CatalogSource,
    method: str,
    separation_arcsec: float | None,
    comparison_epoch: float | None,
    primary: bool,
    evidence: dict,
) -> ObjectCatalogLink:
    return ObjectCatalogLink(
        object_id=int(obj.object_id),
        catalog_source_id=int(source.catalog_source_id),
        association_method=str(method),
        separation_arcsec=separation_arcsec,
        comparison_epoch=comparison_epoch,
        is_primary=bool(primary),
        evidence=dict(evidence),
    )



def _astrometric_candidates(
    session: Session,
    *,
    ra_deg: float,
    dec_deg: float,
    epoch: float | None,
    pmra_masyr: float | None,
    pmdec_masyr: float | None,
    radius_arcsec: float,
    source_catalog_name: str | None = None,
) -> list[tuple[Object, float]]:
    if radius_arcsec <= 0:
        return []
    # SQL prefilter is deliberately broader than the association radius so that
    # ordinary catalog-epoch differences and moderate proper motions are not lost.
    # Very-high-PM cases outside 30 arcsec fail safe by creating a new Object
    # rather than risking a false merge.
    margin_arcsec = max(radius_arcsec, 30.0)
    dec_factor = max(0.05, abs(math.cos(math.radians(dec_deg))))
    dra = margin_arcsec / 3600.0 / dec_factor
    ddec = margin_arcsec / 3600.0
    low_ra = (ra_deg - dra) % 360.0
    high_ra = (ra_deg + dra) % 360.0
    if low_ra <= high_ra:
        ra_clause = (Object.ra_deg >= low_ra, Object.ra_deg <= high_ra)
    else:
        ra_clause = (or_(Object.ra_deg >= low_ra, Object.ra_deg <= high_ra),)
    predicates = [
        *ra_clause,
        Object.dec_deg >= max(-90.0, dec_deg - ddec),
        Object.dec_deg <= min(90.0, dec_deg + ddec),
    ]
    if source_catalog_name:
        # Distinct source IDs inside one catalog release are independent catalog
        # entities.  Because the exact catalog-source link was checked before
        # entering this path, any existing link from this catalog name means the
        # candidate Object belongs to another source from the same release and
        # must not be auto-merged.  Cross-catalog association remains allowed.
        same_catalog_link = (
            select(ObjectCatalogLink.object_catalog_link_id)
            .join(
                CatalogSource,
                CatalogSource.catalog_source_id == ObjectCatalogLink.catalog_source_id,
            )
            .where(
                ObjectCatalogLink.object_id == Object.object_id,
                CatalogSource.catalog_name == str(source_catalog_name),
            )
            .exists()
        )
        predicates.append(~same_catalog_link)
    rows = list(session.scalars(select(Object).where(*predicates)))
    out: list[tuple[Object, float]] = []
    for obj in rows:
        separation = _source_object_separation_arcsec(
            obj,
            source_ra_deg=ra_deg,
            source_dec_deg=dec_deg,
            source_epoch=epoch,
            source_pmra_masyr=pmra_masyr,
            source_pmdec_masyr=pmdec_masyr,
        )
        if separation <= radius_arcsec:
            out.append((obj, separation))
    out.sort(key=lambda item: (item[1], int(item[0].object_id)))
    return out


def _source_object_separation_arcsec(
    obj: Object,
    *,
    source_ra_deg: float,
    source_dec_deg: float,
    source_epoch: float | None,
    source_pmra_masyr: float | None,
    source_pmdec_masyr: float | None,
) -> float:
    object_epoch = _finite_or_none(obj.reference_epoch)
    source_epoch = _finite_or_none(source_epoch)
    object_has_pm = _finite_or_none(obj.pmra_masyr) is not None or _finite_or_none(obj.pmdec_masyr) is not None
    source_has_pm = source_pmra_masyr is not None or source_pmdec_masyr is not None

    if object_epoch is not None and source_epoch is not None and object_epoch != source_epoch:
        if object_has_pm:
            ora, odec = propagate_object_position(obj, source_epoch)
            return angular_separation_arcsec(source_ra_deg, source_dec_deg, ora, odec)
        if source_has_pm:
            sra, sdec = propagate_position(
                source_ra_deg,
                source_dec_deg,
                source_epoch,
                object_epoch,
                source_pmra_masyr,
                source_pmdec_masyr,
            )
            return angular_separation_arcsec(sra, sdec, float(obj.ra_deg), float(obj.dec_deg))
    return angular_separation_arcsec(
        source_ra_deg, source_dec_deg, float(obj.ra_deg), float(obj.dec_deg)
    )


def propagate_object_position(obj: Object, target_epoch: float | None) -> tuple[float, float]:
    return propagate_position(
        float(obj.ra_deg),
        float(obj.dec_deg),
        _finite_or_none(obj.reference_epoch),
        _finite_or_none(target_epoch),
        _finite_or_none(obj.pmra_masyr),
        _finite_or_none(obj.pmdec_masyr),
    )


def propagate_position(
    ra_deg: float,
    dec_deg: float,
    source_epoch: float | None,
    target_epoch: float | None,
    pmra_masyr: float | None,
    pmdec_masyr: float | None,
) -> tuple[float, float]:
    ra = float(ra_deg)
    dec = float(dec_deg)
    if source_epoch is None or target_epoch is None or source_epoch == target_epoch:
        return ra % 360.0, dec
    dt = target_epoch - source_epoch
    if pmdec_masyr is not None:
        dec += pmdec_masyr * dt / 3_600_000.0
    if pmra_masyr is not None:
        cos_dec = max(1e-8, abs(math.cos(math.radians(dec))))
        ra += pmra_masyr * dt / 3_600_000.0 / cos_dec
    return ra % 360.0, max(-90.0, min(90.0, dec))


def angular_separation_arcsec(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
    r1 = math.radians(float(ra1))
    d1 = math.radians(float(dec1))
    r2 = math.radians(float(ra2))
    d2 = math.radians(float(dec2))
    cos_sep = math.sin(d1) * math.sin(d2) + math.cos(d1) * math.cos(d2) * math.cos(r1 - r2)
    cos_sep = max(-1.0, min(1.0, cos_sep))
    return math.degrees(math.acos(cos_sep)) * 3600.0


def _propagation_evidence(obj: Object, target_epoch: float | None) -> dict:
    return {
        "object_reference_epoch": _finite_or_none(obj.reference_epoch),
        "target_epoch": _finite_or_none(target_epoch),
        "pmra_masyr": _finite_or_none(obj.pmra_masyr),
        "pmdec_masyr": _finite_or_none(obj.pmdec_masyr),
    }


def _finite_or_none(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
