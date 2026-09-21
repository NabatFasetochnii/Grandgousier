from __future__ import annotations

import pytest
from sqlalchemy import BigInteger, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from grandgousier.db.models import Base, CatalogSource, Object, ObjectCatalogLink
from grandgousier.identity.object import get_or_create_object_for_catalog_source


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(_type, _compiler, **_kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(_type, _compiler, **_kw):
    return "INTEGER"


@pytest.fixture()
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _source(
    session: Session,
    *,
    catalog: str,
    source_id: str,
    ra: float,
    dec: float,
    epoch: float | None = 2016.0,
    pmra: float | None = None,
    pmdec: float | None = None,
) -> CatalogSource:
    row = CatalogSource(
        catalog_name=catalog,
        source_id=source_id,
        source_uid=f"{catalog}:{source_id}",
        ra_deg=ra,
        dec_deg=dec,
        epoch=epoch,
        pmra_masyr=pmra,
        pmdec_masyr=pmdec,
        ref_mags={},
        colors={},
        source_flags={},
        raw_json={},
    )
    session.add(row)
    session.flush()
    return row


def test_internal_object_uid_is_catalog_independent_and_second_catalog_reuses_object(db_session: Session) -> None:
    gaia = _source(
        db_session,
        catalog="gaia_dr3",
        source_id="111",
        ra=120.0,
        dec=30.0,
        epoch=2016.0,
        pmra=100.0,
        pmdec=0.0,
    )
    first = get_or_create_object_for_catalog_source(db_session, gaia)
    db_session.commit()

    assert first.created is True
    assert first.object_uid is not None and first.object_uid.startswith("obj_")
    assert "gaia" not in first.object_uid.lower()
    assert len(first.object_uid) == 36

    # 100 mas/yr in pmra* over five years is ~0.5 arcsec on sky.  The object
    # position is propagated to the new catalog epoch before the exact match.
    dec_rad_factor = 0.8660254037844386
    ra_2021 = 120.0 + (100.0 * 5.0 / 3_600_000.0 / dec_rad_factor)
    other = _source(
        db_session,
        catalog="other_release",
        source_id="A-9",
        ra=ra_2021,
        dec=30.0,
        epoch=2021.0,
    )
    second = get_or_create_object_for_catalog_source(db_session, other)
    db_session.commit()

    assert second.object_id == first.object_id
    assert second.object_uid == first.object_uid
    assert second.method == "ASTROMETRIC_NEAREST_UNIQUE"
    assert len(list(db_session.scalars(select(Object)))) == 1
    links = list(db_session.scalars(select(ObjectCatalogLink).order_by(ObjectCatalogLink.object_catalog_link_id)))
    assert len(links) == 2
    assert {row.catalog_source_id for row in links} == {gaia.catalog_source_id, other.catalog_source_id}


def test_ambiguous_astrometric_candidates_are_not_auto_merged(db_session: Session) -> None:
    first_source = _source(db_session, catalog="cat_a", source_id="1", ra=10.0, dec=20.0)
    first = get_or_create_object_for_catalog_source(db_session, first_source)
    db_session.commit()

    # Create a second legitimate Object 0.5 arcsec away with its own exact link.
    second_source = _source(
        db_session,
        catalog="cat_b",
        source_id="2",
        ra=10.0 + (0.5 / 3600.0 / 0.9396926207859084),
        dec=20.0,
    )
    second = get_or_create_object_for_catalog_source(
        db_session, second_source, association_radius_arcsec=0.1
    )
    db_session.commit()
    assert second.object_id != first.object_id

    middle = _source(
        db_session,
        catalog="cat_c",
        source_id="3",
        ra=10.0 + (0.25 / 3600.0 / 0.9396926207859084),
        dec=20.0,
    )
    result = get_or_create_object_for_catalog_source(db_session, middle)
    db_session.commit()

    assert result.created is True
    assert result.object_id not in {first.object_id, second.object_id}
    assert len(list(db_session.scalars(select(Object)))) == 3


def test_distinct_sources_in_same_catalog_are_never_astrometrically_merged(db_session: Session) -> None:
    first_source = _source(
        db_session,
        catalog="gaia_dr3",
        source_id="close-a",
        ra=42.0,
        dec=15.0,
    )
    first = get_or_create_object_for_catalog_source(db_session, first_source)
    db_session.commit()

    # Distinct source IDs in one catalog release are independent catalog
    # entities.  Even inside the cross-catalog association radius they must not
    # collapse to one internal Object, otherwise one frame would produce two
    # BASE measurements with the same (object_id, frame_id, calibration_version).
    second_source = _source(
        db_session,
        catalog="gaia_dr3",
        source_id="close-b",
        ra=42.0 + (0.2 / 3600.0 / 0.9659258262890683),
        dec=15.0,
    )
    second = get_or_create_object_for_catalog_source(db_session, second_source)
    db_session.commit()

    assert second.created is True
    assert second.object_id != first.object_id
    assert len(list(db_session.scalars(select(Object)))) == 2
    links = list(db_session.scalars(select(ObjectCatalogLink)))
    assert len(links) == 2
    assert len({int(link.object_id) for link in links}) == 2


def test_distinct_catalogs_still_crossmatch_within_association_radius(db_session: Session) -> None:
    first_source = _source(
        db_session,
        catalog="gaia_dr3",
        source_id="gaia-a",
        ra=84.0,
        dec=-10.0,
    )
    first = get_or_create_object_for_catalog_source(db_session, first_source)
    db_session.commit()

    other_source = _source(
        db_session,
        catalog="panstarrs_dr2",
        source_id="ps-a",
        ra=84.0 + (0.2 / 3600.0 / 0.984807753012208),
        dec=-10.0,
    )
    second = get_or_create_object_for_catalog_source(db_session, other_source)
    db_session.commit()

    assert second.created is False
    assert second.object_id == first.object_id
    assert second.method == "ASTROMETRIC_NEAREST_UNIQUE"
