from __future__ import annotations

from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from grandgousier.catalogs.reference_photometry import (
    find_object_reference_photometry,
    load_reference_photometry_by_identity,
    summarize_reference_photometry_catalogs,
)
from grandgousier.db.models import (
    Base,
    CatalogSource,
    PhotometricBand,
    ReferencePhotometryCatalog,
    ReferencePhotometryPoint,
    ReferencePhotometrySource,
)


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(_type, _compiler, **_kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(_type, _compiler, **_kw):
    return "INTEGER"


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_normalized_reference_payload_preserves_legacy_calibration_shape() -> None:
    session = _session()
    identity = CatalogSource(
        catalog_name="gaia_dr3_master",
        source_id="12345",
        source_uid="gaia:12345",
        ra_deg=10.0,
        dec_deg=20.0,
        ref_mags={},
        colors={},
        source_flags={},
        raw_json={},
    )
    catalog = ReferencePhotometryCatalog(
        catalog_name="panstarrs_dr2__test",
        catalog_version="dr2",
        photometric_system="ps1_ab",
        source_priority=10,
        metadata_json={},
    )
    session.add_all([identity, catalog])
    session.flush()

    g_band = PhotometricBand(
        refcat_id=catalog.refcat_id,
        canonical_name="g",
        provider_band_name="gMeanPSFMag",
        photometric_system="ps1_ab",
        metadata_json={},
    )
    r_band = PhotometricBand(
        refcat_id=catalog.refcat_id,
        canonical_name="r",
        provider_band_name="rMeanPSFMag",
        photometric_system="ps1_ab",
        metadata_json={},
    )
    u_band = PhotometricBand(
        refcat_id=catalog.refcat_id,
        canonical_name="u",
        provider_band_name="uProvider",
        photometric_system="custom",
        metadata_json={},
    )
    session.add_all([g_band, r_band, u_band])
    session.flush()

    source = ReferencePhotometrySource(
        refcat_id=catalog.refcat_id,
        catalog_source_id="ps1-abc",
        ref_uid="rp_test",
        identity_catalog_name="gaia_dr3_master",
        identity_source_id="12345",
        identity_catalog_source_id=identity.catalog_source_id,
        distance_to_identity_arcsec=0.12,
        ra_deg=10.00001,
        dec_deg=20.00001,
        epoch=2016.0,
        colors={"g_r": 0.5},
        quality_flags={"good": True},
        raw_json={"provider": "test"},
    )
    session.add(source)
    session.flush()
    session.add_all(
        [
            ReferencePhotometryPoint(
                refphot_source_id=source.refphot_source_id,
                band_id=g_band.band_id,
                magnitude=15.0,
                magnitude_error=0.01,
                quality_flags={},
                provider_payload={},
            ),
            ReferencePhotometryPoint(
                refphot_source_id=source.refphot_source_id,
                band_id=r_band.band_id,
                magnitude=14.5,
                magnitude_error=0.02,
                quality_flags={},
                provider_payload={},
            ),
            ReferencePhotometryPoint(
                refphot_source_id=source.refphot_source_id,
                band_id=u_band.band_id,
                magnitude=16.2,
                magnitude_error=0.03,
                quality_flags={},
                provider_payload={},
            ),
        ]
    )
    session.commit()

    by_identity = load_reference_photometry_by_identity(
        session,
        reference_catalog_name="panstarrs_dr2__test",
        identity_catalog_name="gaia_dr3_master",
        source_ids=["12345"],
    )
    payload = by_identity["12345"]
    assert payload["catalog_source_id"] == "ps1-abc"
    assert payload["mag_g"] == 15.0
    assert payload["mag_r"] == 14.5
    assert payload["mag_i"] is None
    assert payload["bands"]["u"] == 16.2
    assert "mag_u" not in payload
    assert payload["reference_mag_errors"] == {"g": 0.01, "r": 0.02, "u": 0.03}
    assert payload["colors"] == {"g_r": 0.5}
    assert payload["quality_flags"] == {"good": True}

    rows = find_object_reference_photometry(
        session,
        identity_catalog_name="gaia_dr3_master",
        source_id="12345",
    )
    assert len(rows) == 1
    assert rows[0]["gaia_catalog_name"] == "gaia_dr3_master"
    assert rows[0]["gaia_source_id"] == "12345"
    assert rows[0]["mag_g"] == 15.0
    assert rows[0]["mag_r"] == 14.5

    summary = summarize_reference_photometry_catalogs(session)
    assert len(summary) == 1
    assert summary[0]["n_sources"] == 1
    assert summary[0]["n_matched_identity"] == 1
    assert summary[0]["n_g"] == 1
    assert summary[0]["n_r"] == 1
    assert summary[0]["n_i"] == 0
    assert summary[0]["n_u"] == 1


def test_new_band_requires_rows_not_schema_columns() -> None:
    table = Base.metadata.tables["reference_photometry_sources"]
    assert "mag_u" not in table.c
    assert "mag_g" not in table.c
    assert {"canonical_name", "refcat_id"}.issubset(
        Base.metadata.tables["photometric_bands"].c.keys()
    )
    assert {"band_id", "magnitude", "magnitude_error"}.issubset(
        Base.metadata.tables["reference_photometry_points"].c.keys()
    )
