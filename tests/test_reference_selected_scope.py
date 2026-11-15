from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from grandgousier.archive import auto_process
from grandgousier.calibration.photometric_policy import ColorCalibrationPolicy
from grandgousier.catalogs import reference_photometry


def _policy() -> ColorCalibrationPolicy:
    return ColorCalibrationPolicy(
        enabled=True,
        reference_catalog_name="panstarrs_dr2",
        reference_band="g",
        color_index="g-r",
        require_bands=("g", "r"),
        min_stars_per_frame=1,
    )


class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)


class _PersistedIdentitySession:
    def __init__(self, events):
        self.events = events

    def execute(self, statement, *_args, **_kwargs):
        sql = str(statement)
        assert "catalog_sources.catalog_name" in sql
        assert "catalog_sources.source_id IN" in sql
        self.events.append("load_persisted_identities")
        return _Rows([("42", 10.0, 20.0), ("84", 30.0, -5.0)])


def test_reference_provider_queries_only_selected_identity_positions(monkeypatch, tmp_path):
    requested = []
    events = []

    def fake_fetch(ra_deg, dec_deg, radius_deg, **kwargs):
        requested.append((ra_deg, dec_deg, radius_deg, kwargs))
        events.append("provider_request")
        return (
            pd.DataFrame(
                [
                    {
                        "objID": f"ps1-{len(requested)}",
                        "raMean": ra_deg,
                        "decMean": dec_deg,
                        "gMeanPSFMag": 15.0,
                        "rMeanPSFMag": 14.8,
                        "nDetections": 12,
                    }
                ]
            ),
            "https://provider.invalid/query",
            {},
            {"source": "test", "path": None},
        )

    monkeypatch.setattr(
        reference_photometry, "_fetch_panstarrs_dr2_mean_csv", fake_fetch
    )
    result = reference_photometry.create_reference_photometry_csv(
        _PersistedIdentitySession(events),
        out_path=tmp_path / "selected.csv",
        selected_identity_catalog_name="gaia_dr3",
        selected_identity_source_ids={"42", "84"},
        selected_identity_match_radius_arcsec=1.0,
    )

    assert [(ra, dec) for ra, dec, _radius, _kwargs in requested] == [
        (10.0, 20.0),
        (30.0, -5.0),
    ]
    assert all(radius == 1.0 / 3600.0 for _ra, _dec, radius, _kwargs in requested)
    assert events == [
        "load_persisted_identities",
        "provider_request",
        "provider_request",
    ]
    assert result.filter_summary["scope"] == "selected_identity_sources"
    assert result.filter_summary["selected_identity_count"] == 2
    written = pd.read_csv(result.path, dtype={"selected_identity_source_id": "string"})
    assert written["selected_identity_source_id"].tolist() == ["42", "84"]


def test_selected_reference_import_drops_unmatched_and_ambiguous_rows(
    monkeypatch, tmp_path
):
    identity_sources = [
        {
            "catalog_source_id": 1,
            "catalog_name": "gaia_dr3",
            "source_id": "42",
            "ra_deg": 10.0,
            "dec_deg": 20.0,
        },
        {
            "catalog_source_id": 2,
            "catalog_name": "gaia_dr3",
            "source_id": "84",
            "ra_deg": 10.0005,
            "dec_deg": 20.0,
        },
    ]
    monkeypatch.setattr(
        reference_photometry,
        "_load_match_catalog_sources",
        lambda *_args, **_kwargs: identity_sources,
    )
    csv_path = tmp_path / "refs.csv"
    pd.DataFrame(
        [
            {
                "catalog_source_id": "ps1-1",
                "selected_identity_source_id": "42",
                "ra": 10.0,
                "dec": 20.0,
                "mag_g": 15.0,
                "mag_r": 14.8,
            },
            {
                "catalog_source_id": "ps1-wrong-scope",
                "selected_identity_source_id": "84",
                "ra": 10.0,
                "dec": 20.0,
                "mag_g": 15.1,
                "mag_r": 14.9,
            },
            {
                "catalog_source_id": "ps1-ambiguous",
                "selected_identity_source_id": "42",
                "ra": 10.00025,
                "dec": 20.0,
                "mag_g": 15.2,
                "mag_r": 15.0,
            },
            {
                "catalog_source_id": "ps1-unmatched",
                "selected_identity_source_id": "42",
                "ra": 100.0,
                "dec": 20.0,
                "mag_g": 15.3,
                "mag_r": 15.1,
            },
        ]
    ).to_csv(csv_path, index=False)

    result = reference_photometry.import_reference_photometry(
        object(),
        path=csv_path,
        catalog_name="panstarrs_dr2__gaia_dr3",
        source_id_col="catalog_source_id",
        mag_cols={"g": "mag_g", "r": "mag_r"},
        match_to_catalog="gaia_dr3",
        selected_identity_source_ids={"42", "84"},
        requested_identity_source_id_col="selected_identity_source_id",
        dry_run=True,
    )

    assert result.read_rows == 4
    assert result.valid_rows == 1
    assert result.matched_to_gaia == 1
    assert result.unmatched_to_gaia == 3
    assert result.inserted_or_updated == 0
    assert result.skipped_ambiguous_identity_matches >= 2


def test_reference_ensure_skips_before_any_provider_or_import_without_selected_set(
    monkeypatch,
):
    for name in (
        "load_persisted_identity_sources",
        "create_reference_photometry_csv",
        "import_reference_photometry",
    ):
        monkeypatch.setattr(
            auto_process,
            name,
            lambda *_args, _name=name, **_kwargs: (_ for _ in ()).throw(
                AssertionError(f"{_name} must not run without selected identities")
            ),
        )
    monkeypatch.setattr(
        auto_process,
        "_reference_band_counts_for_series",
        lambda *_args, **_kwargs: pytest.fail("coverage cannot be field-scoped before selection"),
    )

    result = auto_process._ensure_reference_photometry_for_series(
        object(),
        series={"series_id": 5, "field_id": 8},
        policy=_policy(),
        identity_catalog_name="gaia_dr3",
        selected_identity_source_ids=None,
        out_dir=None,
        allow_remote_fetch=True,
    )

    assert result["status"] == "SKIPPED_SELECTED_IDENTITY_SET_UNAVAILABLE"
    assert result["ready"] is False


def test_reference_ensure_counts_and_imports_only_the_exact_selected_set(
    monkeypatch, tmp_path
):
    events = []
    selected_sources = [
        {"source_id": "42", "ra_deg": 10.0, "dec_deg": 20.0},
        {"source_id": "84", "ra_deg": 30.0, "dec_deg": -5.0},
    ]

    def load_selected(_session, *, identity_catalog_name, source_ids):
        events.append("load_persisted_identities")
        assert identity_catalog_name == "gaia_dr3"
        assert set(source_ids) == {"42", "84"}
        return selected_sources

    count_calls = []

    def count_selected(_session, *, identity_catalog_name, reference_catalog_name, selected_identity_source_ids):
        count_calls.append(set(selected_identity_source_ids))
        if len(count_calls) < 3:
            return {}
        return {"g": 2, "r": 2}

    def fetch_selected(_session, **kwargs):
        events.append("provider_selected_scope")
        assert "series_id" not in kwargs and "field_id" not in kwargs
        assert kwargs["selected_identity_catalog_name"] == "gaia_dr3"
        assert kwargs["selected_identity_source_ids"] == ["42", "84"]
        return SimpleNamespace(raw_rows=2, written_rows=2, path=str(tmp_path / "selected.csv"))

    def import_selected(_session, **kwargs):
        events.append("import_selected_scope")
        assert kwargs["selected_identity_source_ids"] == ["42", "84"]
        assert kwargs["requested_identity_source_id_col"] == "selected_identity_source_id"
        return SimpleNamespace(inserted_or_updated=2, matched_to_gaia=2, unmatched_to_gaia=0)

    monkeypatch.setattr(auto_process, "load_persisted_identity_sources", load_selected)
    monkeypatch.setattr(auto_process, "_reference_band_counts_for_series", count_selected)
    monkeypatch.setattr(auto_process, "create_reference_photometry_csv", fetch_selected)
    monkeypatch.setattr(auto_process, "import_reference_photometry", import_selected)

    result = auto_process._ensure_reference_photometry_for_series(
        object(),
        series={"series_id": 5, "field_id": 8},
        policy=_policy(),
        identity_catalog_name="gaia_dr3",
        selected_identity_source_ids={"42", "84"},
        out_dir=tmp_path,
        allow_remote_fetch=True,
    )

    assert events == [
        "load_persisted_identities",
        "provider_selected_scope",
        "import_selected_scope",
    ]
    assert count_calls == [{"42", "84"}] * 3
    assert result["status"] == "FETCHED_IMPORTED"
    assert result["selected_identity_count"] == 2
    assert result["ready"] is True


def test_existing_selected_reference_csv_is_not_overwritten_or_fetched(
    monkeypatch, tmp_path
):
    selected_sources = [{"source_id": "42", "ra_deg": 10.0, "dec_deg": 20.0}]
    load_events = []

    def load_selected(_session, *, identity_catalog_name, source_ids):
        assert identity_catalog_name == "gaia_dr3"
        assert list(source_ids) == ["42"]
        load_events.append("load")
        return selected_sources

    monkeypatch.setattr(auto_process, "load_persisted_identity_sources", load_selected)
    monkeypatch.setattr(reference_photometry, "load_persisted_identity_sources", load_selected)
    monkeypatch.setattr(
        auto_process, "_reference_band_counts_for_series", lambda *_args, **_kwargs: {}
    )

    def fail_if_fetched(*_args, **_kwargs):
        pytest.fail("an existing selected-scope CSV must be rejected before provider fetch")

    monkeypatch.setattr(
        reference_photometry, "_fetch_panstarrs_dr2_mean_csv", fail_if_fetched
    )

    csv_path = tmp_path / "reference_photometry" / "series_5_panstarrs_dr2.csv"
    csv_path.parent.mkdir(parents=True)
    original_contents = b"existing broader-scope reference artifact\n"
    csv_path.write_bytes(original_contents)
    session = SimpleNamespace(rollback=lambda: None)

    result = auto_process._ensure_reference_photometry_for_series(
        session,
        series={"series_id": 5, "field_id": 8},
        policy=_policy(),
        identity_catalog_name="gaia_dr3",
        selected_identity_source_ids={"42"},
        out_dir=tmp_path,
        allow_remote_fetch=True,
    )

    assert result["status"] == "REFERENCE_ACQUISITION_FAILED"
    assert "FileExistsError" in result["reason"]
    assert csv_path.read_bytes() == original_contents
    assert load_events == ["load", "load"]
