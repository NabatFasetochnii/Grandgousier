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


def _provider_diagnostics(**overrides):
    diagnostics = {
        "logical_query_groups": 1,
        "provider_requests": 1,
        "provider_response_bytes_received": 1,
        "provider_rows_received": 1,
        "provider_rows_retained": 1,
        "provider_exact_duplicates_collapsed": 0,
        "provider_conflicting_source_ids": 0,
        "provider_conflicting_rows": 0,
        "matched_rows": 1,
        "ambiguous_rows": 0,
        "ambiguous_identities": 0,
        "unmatched_rows": 0,
        "unmatched_identities": 0,
    }
    diagnostics.update(overrides)
    return diagnostics


class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)


class _PersistedIdentitySession:
    def __init__(self, events, identity_rows=None):
        self.events = events
        self.identity_rows = identity_rows or [("42", 10.0, 20.0), ("84", 30.0, -5.0)]

    def execute(self, statement, *_args, **_kwargs):
        sql = str(statement)
        assert "catalog_sources.catalog_name" in sql
        assert "catalog_sources.source_id IN" in sql
        self.events.append("load_persisted_identities")
        return _Rows(self.identity_rows)


def _ps1_row(obj_id, ra, dec):
    return {
        "objID": obj_id,
        "raMean": ra,
        "decMean": dec,
        "gMeanPSFMag": 15.0,
        "rMeanPSFMag": 14.8,
        "nDetections": 12,
    }


def test_reference_provider_groups_close_selected_positions_and_exports_only_matches(
    monkeypatch, tmp_path
):
    requested = []
    events = []
    second_ra = 10.0 + 5.0 / (3600.0 * 0.9396926207859084)
    identity_rows = [("42", 10.0, 20.0), ("84", second_ra, 20.0)]
    provider_rows = [
        {
            "objID": "ps1-42",
            "raMean": 10.0,
            "decMean": 20.0,
            "gMeanPSFMag": 15.0,
            "rMeanPSFMag": 14.8,
            "nDetections": 12,
        },
        {
            "objID": "ps1-84",
            "raMean": second_ra,
            "decMean": 20.0,
            "gMeanPSFMag": 15.1,
            "rMeanPSFMag": 14.9,
            "nDetections": 12,
        },
        {
            "objID": "unrelated-provider-row",
            "raMean": 10.01,
            "decMean": 20.0,
            "gMeanPSFMag": 15.2,
            "rMeanPSFMag": 15.0,
            "nDetections": 12,
        },
    ]

    def fake_fetch(ra_deg, dec_deg, radius_deg, **kwargs):
        requested.append((ra_deg, dec_deg, radius_deg, kwargs))
        events.append("provider_request")
        return (
            pd.DataFrame(provider_rows),
            "https://provider.invalid/query",
            {},
            {"source": "test", "path": None},
        )

    monkeypatch.setattr(
        reference_photometry, "_fetch_panstarrs_dr2_mean_csv", fake_fetch
    )
    result = reference_photometry.create_reference_photometry_csv(
        _PersistedIdentitySession(events, identity_rows),
        out_path=tmp_path / "selected.csv",
        selected_identity_catalog_name="gaia_dr3",
        selected_identity_source_ids={"42", "84"},
        selected_identity_match_radius_arcsec=1.0,
    )

    assert len(requested) == 1 < len(identity_rows)
    ra, dec, radius, request_kwargs = requested[0]
    assert (ra, dec) == (10.0, 20.0)
    assert 6.0 / 3600.0 <= radius <= reference_photometry._MAX_SELECTED_REQUEST_RADIUS_DEG
    assert request_kwargs["max_response_bytes"] == (
        reference_photometry._MAX_SELECTED_RESPONSE_BYTES_PER_REQUEST
    )
    assert events == [
        "load_persisted_identities",
        "provider_request",
    ]
    assert result.filter_summary["scope"] == "selected_identity_sources"
    assert result.filter_summary["selected_identity_count"] == 2
    diagnostics = result.filter_summary["selected_reference_diagnostics"]
    assert diagnostics == {
        "logical_query_groups": 1,
        "provider_requests": 1,
        "provider_response_bytes_received": len(pd.DataFrame(provider_rows).to_csv(index=False).encode("utf-8")),
        "provider_rows_received": 3,
        "provider_rows_retained": 2,
        "provider_exact_duplicates_collapsed": 0,
        "provider_conflicting_source_ids": 0,
        "provider_conflicting_rows": 0,
        "matched_rows": 2,
        "ambiguous_rows": 0,
        "ambiguous_identities": 0,
        "unmatched_rows": 1,
        "unmatched_identities": 0,
    }
    written = pd.read_csv(result.path, dtype={"selected_identity_source_id": "string"})
    assert written["selected_identity_source_id"].tolist() == ["42", "84"]
    assert written["catalog_source_id"].tolist() == ["ps1-42", "ps1-84"]


def test_selected_provider_collapses_identical_rows_after_scientific_normalization(
    monkeypatch, tmp_path
):
    provider_row = _ps1_row("ps1-42", 0.0, -0.0)
    provider_row.update(
        {
            "gMeanPSFMagErr": None,
            "qualityFlag": None,
            "objInfoFlag": None,
        }
    )
    normalized_variant = provider_row.copy()
    normalized_variant.update(
        {
            "raMean": 360.0,
            "decMean": 0.0,
            "gMeanPSFMagErr": float("nan"),
            "qualityFlag": float("nan"),
            "objInfoFlag": float("nan"),
        }
    )

    monkeypatch.setattr(
        reference_photometry,
        "_fetch_panstarrs_dr2_mean_csv",
        lambda *_args, **_kwargs: (
            pd.DataFrame(
                [provider_row, provider_row.copy(), normalized_variant]
            ),
            "",
            {},
            {"source": "test", "path": None},
        ),
    )
    result = reference_photometry.create_reference_photometry_csv(
        _PersistedIdentitySession([], [("42", 0.0, 0.0)]),
        out_path=tmp_path / "exact-duplicates.csv",
        selected_identity_catalog_name="gaia_dr3",
        selected_identity_source_ids={"42"},
        selected_identity_match_radius_arcsec=1.0,
    )

    assert result.raw_rows == 3
    assert result.standardized_rows == result.written_rows == 1
    assert result.skipped_rows == 2
    diagnostics = result.filter_summary["selected_reference_diagnostics"]
    assert diagnostics["provider_exact_duplicates_collapsed"] == 2
    assert diagnostics["provider_conflicting_source_ids"] == 0
    assert diagnostics["provider_conflicting_rows"] == 0
    written = pd.read_csv(result.path)
    assert written["catalog_source_id"].tolist() == ["ps1-42"]
    assert written[["ra", "dec"]].iloc[0].tolist() == [0.0, 0.0]


@pytest.mark.parametrize(
    ("conflicting_field", "conflicting_value"),
    [
        ("gMeanPSFMag", 15.2),
        ("gMeanPSFMagErr", 0.02),
        ("qualityFlag", "bad-quality"),
        ("objInfoFlag", "extended"),
    ],
    ids=["magnitude", "magnitude-error", "quality-flag", "object-info-flag"],
)
def test_selected_provider_excludes_every_row_for_conflicting_source_id(
    monkeypatch, tmp_path, conflicting_field, conflicting_value
):
    provider_row = _ps1_row("ps1-conflict", 10.0, 20.0)
    provider_row.update(
        {
            "gMeanPSFMagErr": 0.01,
            "qualityFlag": "good",
            "objInfoFlag": "stellar",
        }
    )
    conflicting_row = provider_row.copy()
    conflicting_row[conflicting_field] = conflicting_value

    monkeypatch.setattr(
        reference_photometry,
        "_fetch_panstarrs_dr2_mean_csv",
        lambda *_args, **_kwargs: (
            pd.DataFrame([provider_row, provider_row.copy(), conflicting_row]),
            "",
            {},
            {"source": "test", "path": None},
        ),
    )
    out_path = tmp_path / f"conflicting-{conflicting_field}.csv"
    result = reference_photometry.create_reference_photometry_csv(
        _PersistedIdentitySession([], [("42", 10.0, 20.0)]),
        out_path=out_path,
        selected_identity_catalog_name="gaia_dr3",
        selected_identity_source_ids={"42"},
        selected_identity_match_radius_arcsec=1.0,
    )

    assert result.raw_rows == 3
    assert result.standardized_rows == result.written_rows == 0
    assert result.skipped_rows == 3
    diagnostics = result.filter_summary["selected_reference_diagnostics"]
    assert diagnostics["provider_exact_duplicates_collapsed"] == 0
    assert diagnostics["provider_conflicting_source_ids"] == 1
    assert diagnostics["provider_conflicting_rows"] == 3
    assert pd.read_csv(out_path).empty


def test_selected_provider_csv_and_counters_are_stable_across_response_and_group_permutations(
    monkeypatch, tmp_path
):
    identity_rows = [("20", 30.0, -5.0), ("7", 10.0, 20.0)]
    permuted_identity_rows = list(reversed(identity_rows))

    def run(identity_order, reverse_response_rows, out_path):
        requests = []

        def fake_fetch(ra_deg, dec_deg, _radius_deg, **_kwargs):
            requests.append((ra_deg, dec_deg))
            if ra_deg < 20.0:
                rows = [
                    _ps1_row("12", 10.0, 20.0),
                    _ps1_row("12", 10.0, 20.0),
                    _ps1_row("900", 10.01, 20.0),
                ]
            else:
                rows = [
                    _ps1_row("3", 30.0, -5.0),
                    _ps1_row("3", 30.0, -5.0),
                    _ps1_row("800", 30.01, -5.0),
                ]
            if reverse_response_rows:
                rows.reverse()
            return (
                pd.DataFrame(rows),
                "",
                {},
                {"source": "test", "path": None},
            )

        monkeypatch.setattr(
            reference_photometry, "_fetch_panstarrs_dr2_mean_csv", fake_fetch
        )
        result = reference_photometry.create_reference_photometry_csv(
            _PersistedIdentitySession([], identity_order),
            out_path=out_path,
            selected_identity_catalog_name="gaia_dr3",
            selected_identity_source_ids={"20", "7"},
            selected_identity_match_radius_arcsec=1.0,
        )
        return result, requests, out_path.read_bytes()

    first, first_requests, first_csv = run(
        identity_rows, False, tmp_path / "permutation-a.csv"
    )
    second, second_requests, second_csv = run(
        permuted_identity_rows, True, tmp_path / "permutation-b.csv"
    )

    assert first_requests == second_requests == [(30.0, -5.0), (10.0, 20.0)]
    assert first_csv == second_csv
    assert first.filter_summary == second.filter_summary
    assert (
        first.raw_rows,
        first.standardized_rows,
        first.written_rows,
        first.skipped_rows,
        first.filtered_rows,
    ) == (
        second.raw_rows,
        second.standardized_rows,
        second.written_rows,
        second.skipped_rows,
        second.filtered_rows,
    ) == (6, 2, 2, 4, 0)
    diagnostics = first.filter_summary["selected_reference_diagnostics"]
    assert diagnostics["provider_exact_duplicates_collapsed"] == 2
    assert diagnostics["provider_conflicting_source_ids"] == 0
    assert diagnostics["provider_conflicting_rows"] == 0
    assert diagnostics["unmatched_rows"] == 2
    written = pd.read_csv(
        tmp_path / "permutation-a.csv",
        dtype={
            "selected_identity_source_id": "string",
            "catalog_source_id": "string",
        },
    )
    assert written["selected_identity_source_id"].tolist() == ["7", "20"]
    assert written["catalog_source_id"].tolist() == ["12", "3"]


def test_selected_provider_crossmatch_handles_ra_wrap_and_high_declination(
    monkeypatch, tmp_path
):
    identity_rows = [
        ("pole-a", 0.0, 89.999),
        ("pole-b", 180.0, 89.999),
        ("wrap0", 0.0006, 0.0),
        ("wrap360", 359.9998, 0.0),
    ]
    requested = []

    def fake_fetch(ra_deg, dec_deg, radius_deg, **_kwargs):
        requested.append((ra_deg, dec_deg, radius_deg))
        if dec_deg > 80.0:
            frame = pd.DataFrame(
                [
                    _ps1_row("ps1-pole-a", 0.0, 89.999),
                    _ps1_row("ps1-pole-b", 180.0, 89.999),
                ]
            )
        else:
            frame = pd.DataFrame(
                [
                    _ps1_row("ps1-wrap360", 360.0, 0.0),
                    _ps1_row("ps1-wrap0", 0.0006, 0.0),
                ]
            )
        return frame, "", {}, {"source": "test", "path": None}

    monkeypatch.setattr(reference_photometry, "_fetch_panstarrs_dr2_mean_csv", fake_fetch)
    result = reference_photometry.create_reference_photometry_csv(
        _PersistedIdentitySession([], identity_rows),
        out_path=tmp_path / "wrap-pole.csv",
        selected_identity_catalog_name="gaia_dr3",
        selected_identity_source_ids={"pole-a", "pole-b", "wrap0", "wrap360"},
        selected_identity_match_radius_arcsec=1.0,
    )

    assert len(requested) == 2
    assert all(radius <= reference_photometry._MAX_SELECTED_REQUEST_RADIUS_DEG for _, _, radius in requested)
    written = pd.read_csv(result.path, dtype={"selected_identity_source_id": "string"})
    assert written["selected_identity_source_id"].tolist() == [
        "pole-a",
        "pole-b",
        "wrap0",
        "wrap360",
    ]
    assert written["catalog_source_id"].tolist() == [
        "ps1-pole-a",
        "ps1-pole-b",
        "ps1-wrap0",
        "ps1-wrap360",
    ]
    assert written["ra"].tolist() == pytest.approx([0.0, 180.0, 0.0006, 0.0])
    diagnostics = result.filter_summary["selected_reference_diagnostics"]
    assert diagnostics["matched_rows"] == 4
    assert diagnostics["unmatched_identities"] == 0


@pytest.mark.parametrize(
    ("identity_rows", "provider_rows", "expected_ambiguous_rows", "expected_ambiguous_ids"),
    [
        (
            [("42", 10.0, 20.0), ("84", 10.0001, 20.0)],
            [_ps1_row("one-to-many-identities", 10.00005, 20.0)],
            1,
            2,
        ),
        (
            [("42", 10.0, 20.0)],
            [
                _ps1_row("many-to-one-row-a", 10.00005, 20.0),
                _ps1_row("many-to-one-row-b", 9.99995, 20.0),
            ],
            2,
            1,
        ),
    ],
    ids=["external-row-to-many-identities", "identity-to-many-external-rows"],
)
def test_selected_provider_rejects_both_ambiguity_directions(
    monkeypatch,
    tmp_path,
    identity_rows,
    provider_rows,
    expected_ambiguous_rows,
    expected_ambiguous_ids,
):
    monkeypatch.setattr(
        reference_photometry,
        "_fetch_panstarrs_dr2_mean_csv",
        lambda *_args, **_kwargs: (
            pd.DataFrame(provider_rows),
            "",
            {},
            {"source": "test", "path": None},
        ),
    )
    selected_ids = {row[0] for row in identity_rows}
    result = reference_photometry.create_reference_photometry_csv(
        _PersistedIdentitySession([], identity_rows),
        out_path=tmp_path / "ambiguous.csv",
        selected_identity_catalog_name="gaia_dr3",
        selected_identity_source_ids=selected_ids,
        selected_identity_match_radius_arcsec=1.0,
    )

    assert result.standardized_rows == result.written_rows == 0
    diagnostics = result.filter_summary["selected_reference_diagnostics"]
    assert diagnostics["matched_rows"] == 0
    assert diagnostics["ambiguous_rows"] == expected_ambiguous_rows
    assert diagnostics["ambiguous_identities"] == expected_ambiguous_ids
    assert "source_ids" not in diagnostics
    assert pd.read_csv(result.path).empty


@pytest.mark.parametrize(
    "cap", ["groups", "request_bytes", "total_bytes", "rows", "candidate_graph"]
)
def test_selected_provider_caps_fail_closed_without_partial_csv(monkeypatch, tmp_path, cap):
    identity_rows = [("42", 10.0, 20.0)]
    calls = []
    provider_rows = [_ps1_row("ps1-42", 10.0, 20.0), _ps1_row("unrelated", 10.01, 20.0)]
    if cap == "groups":
        identity_rows = [("42", 10.0, 20.0), ("84", 30.0, -5.0)]
        monkeypatch.setattr(reference_photometry, "_MAX_SELECTED_GROUPS", 1)
        error_match = "group/request cap"
    elif cap == "request_bytes":
        monkeypatch.setattr(reference_photometry, "_MAX_SELECTED_RESPONSE_BYTES_PER_REQUEST", 4)
        monkeypatch.setattr(reference_photometry, "_MAX_SELECTED_RESPONSE_BYTES_TOTAL", 100)
        error_match = "byte budget"
    elif cap == "total_bytes":
        identity_rows = [("42", 10.0, 20.0), ("84", 30.0, -5.0)]
        monkeypatch.setattr(reference_photometry, "_MAX_SELECTED_RESPONSE_BYTES_PER_REQUEST", 10)
        monkeypatch.setattr(reference_photometry, "_MAX_SELECTED_RESPONSE_BYTES_TOTAL", 5)
        error_match = "byte budget"
    else:
        if cap == "rows":
            monkeypatch.setattr(reference_photometry, "_MAX_SELECTED_RESPONSE_ROWS", 1)
            error_match = "row budget"
        else:
            identity_rows = [("42", 10.0, 20.0), ("84", 10.0001, 20.0)]
            provider_rows = [_ps1_row("ps1-ambiguous", 10.00005, 20.0)]
            monkeypatch.setattr(reference_photometry, "_MAX_SELECTED_CANDIDATE_COMPARISONS", 1)
            error_match = "comparison cap"

    def fake_fetch(ra_deg, dec_deg, radius_deg, **kwargs):
        calls.append((ra_deg, dec_deg, radius_deg, kwargs["max_response_bytes"]))
        frame = pd.DataFrame(provider_rows)
        frame.attrs["reference_response_bytes"] = 5 if cap != "total_bytes" else 3
        return frame, "", {}, {"source": "test", "path": None}

    monkeypatch.setattr(reference_photometry, "_fetch_panstarrs_dr2_mean_csv", fake_fetch)
    out_path = tmp_path / f"cap-{cap}.csv"
    with pytest.raises((RuntimeError, ValueError), match=error_match):
        reference_photometry.create_reference_photometry_csv(
            _PersistedIdentitySession([], identity_rows),
            out_path=out_path,
            selected_identity_catalog_name="gaia_dr3",
            selected_identity_source_ids={row[0] for row in identity_rows},
            selected_identity_match_radius_arcsec=1.0,
        )

    assert not out_path.exists()
    if cap == "groups":
        assert calls == []  # Complete grouping is checked before any HTTP attempt.
    if cap == "total_bytes":
        assert [call[3] for call in calls] == [5, 2]


def test_selected_provider_does_not_write_after_a_later_group_fails(monkeypatch, tmp_path):
    identity_rows = [("42", 10.0, 20.0), ("84", 30.0, -5.0)]
    calls = []

    def fake_fetch(*_args, **_kwargs):
        calls.append("attempt")
        if len(calls) == 2:
            raise RuntimeError("provider unavailable")
        return (
            pd.DataFrame(columns=["objID", "raMean", "decMean"]),
            "",
            {},
            {"source": "test", "path": None},
        )

    monkeypatch.setattr(reference_photometry, "_fetch_panstarrs_dr2_mean_csv", fake_fetch)
    out_path = tmp_path / "incomplete.csv"
    with pytest.raises(RuntimeError, match="provider unavailable"):
        reference_photometry.create_reference_photometry_csv(
            _PersistedIdentitySession([], identity_rows),
            out_path=out_path,
            selected_identity_catalog_name="gaia_dr3",
            selected_identity_source_ids={"42", "84"},
            selected_identity_match_radius_arcsec=1.0,
        )

    assert calls == ["attempt", "attempt"]
    assert not out_path.exists()


def test_panstarrs_fetch_reads_only_one_byte_past_the_selected_response_cap(monkeypatch):
    class _FakeResponse:
        def __init__(self):
            self.read_sizes = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, size=-1):
            self.read_sizes.append(size)
            payload = b"objID,raMean,decMean\n123,1,2\n"
            return payload if size < 0 else payload[:size]

    response = _FakeResponse()
    monkeypatch.setattr(
        reference_photometry,
        "_build_reference_https_context",
        lambda: (None, {"source": "test", "path": None}),
    )
    monkeypatch.setattr(
        reference_photometry.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: response,
    )

    with pytest.raises(RuntimeError, match="configured byte limit"):
        reference_photometry._fetch_panstarrs_dr2_mean_csv(
            10.0,
            20.0,
            0.001,
            use_remote_filters=False,
            max_response_bytes=5,
        )

    assert response.read_sizes == [6]


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
        pd.DataFrame(
            [
                {"catalog_source_id": "ps1-42", "selected_identity_source_id": "42"},
                {"catalog_source_id": "ps1-84", "selected_identity_source_id": "84"},
            ]
        ).to_csv(kwargs["out_path"], index=False)
        return SimpleNamespace(
            raw_rows=2,
            written_rows=2,
            path=str(kwargs["out_path"]),
            filter_summary={
                "selected_reference_diagnostics": _provider_diagnostics(
                    provider_rows_received=2,
                    provider_rows_retained=2,
                    matched_rows=2,
                )
            },
        )

    def import_selected(_session, **kwargs):
        events.append("import_selected_scope")
        assert kwargs["selected_identity_source_ids"] == ["42", "84"]
        assert kwargs["requested_identity_source_id_col"] == "selected_identity_source_id"
        return SimpleNamespace(inserted_or_updated=2, matched_to_gaia=2, unmatched_to_gaia=0)

    monkeypatch.setattr(auto_process, "load_persisted_identity_sources", load_selected)
    monkeypatch.setattr(auto_process, "_reference_band_counts_for_series", count_selected)
    monkeypatch.setattr(
        auto_process,
        "_reference_band_coverage_by_identity",
        lambda _session, *, selected_identity_source_ids, **_kwargs: {
            source_id: set() for source_id in selected_identity_source_ids
        },
    )
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


def test_fingerprinted_csv_without_manifest_is_rejected_before_provider(
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
    monkeypatch.setattr(
        auto_process, "_reference_band_counts_for_series", lambda *_args, **_kwargs: {}
    )
    monkeypatch.setattr(
        auto_process,
        "_reference_band_coverage_by_identity",
        lambda _session, *, selected_identity_source_ids, **_kwargs: {
            source_id: set() for source_id in selected_identity_source_ids
        },
    )

    def fail_if_fetched(*_args, **_kwargs):
        pytest.fail("an existing selected-scope CSV must be rejected before provider fetch")

    monkeypatch.setattr(auto_process, "create_reference_photometry_csv", fail_if_fetched)

    fingerprint, _provenance = auto_process._selected_reference_cache_fingerprint(
        provider="panstarrs_dr2",
        provider_release="dr2",
        identity_catalog_name="gaia_dr3",
        missing_identity_source_ids=["42"],
        persisted_identity_sources=selected_sources,
        required_bands=["g", "r"],
        filters={
            "require_bands": ["g", "r"],
            "min_mag": None,
            "max_mag": None,
            "max_mag_err": None,
            "min_detections": None,
            "use_remote_filters": True,
            "limit": None,
        },
        match_radius_arcsec=1.0,
    )
    csv_path = (
        tmp_path
        / "reference_photometry"
        / f"panstarrs_dr2_{fingerprint}.csv"
    )
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

    assert result["status"] == "REFERENCE_CACHE_INVALID"
    assert "incomplete" in result["reason"]
    assert csv_path.read_bytes() == original_contents
    assert load_events == ["load"]


class _MappingQueryResult:
    def __init__(self, rows):
        self.rows = list(rows)

    def mappings(self):
        return self

    def all(self):
        return list(self.rows)


def _coverage_policy(minimum_sources=1):
    return ColorCalibrationPolicy(
        enabled=True,
        reference_catalog_name="panstarrs_dr2",
        reference_band="g",
        color_index="g-r",
        require_bands=("g", "r"),
        min_stars_per_frame=minimum_sources,
    )


def test_reference_band_coverage_query_returns_exact_per_identity_bands_in_bounded_chunks(
    monkeypatch,
):
    monkeypatch.setattr(auto_process, "_REFERENCE_COVERAGE_QUERY_CHUNK_SIZE", 2)

    class _Session:
        def __init__(self):
            self.calls = []

        def execute(self, statement, params):
            sql = str(statement)
            assert "reference_photometry_sources" in sql
            assert "reference_photometry_points" in sql
            assert "photometric_bands" in sql
            assert "rps.identity_source_id IN" in sql
            assert "pb.canonical_name IN" in sql
            self.calls.append((sql, params))
            rows = []
            for source_id in params["selected_identity_source_ids"]:
                rows.extend(
                    {"identity_source_id": source_id, "band": band}
                    for band in {
                        "source-a": {"g", "r"},
                        "source-b": {"g"},
                        "source-c": set(),
                    }[source_id]
                )
            return _MappingQueryResult(rows)

    session = _Session()
    coverage = auto_process._reference_band_coverage_by_identity(
        session,
        identity_catalog_name="gaia_dr3",
        reference_catalog_name="panstarrs_dr2__gaia_dr3",
        selected_identity_source_ids={"source-a", "source-b", "source-c"},
        required_bands=["g", "r"],
    )

    assert coverage == {
        "source-a": {"g", "r"},
        "source-b": {"g"},
        "source-c": set(),
    }
    assert len(session.calls) == 2
    assert [call[1]["selected_identity_source_ids"] for call in session.calls] == [
        ["source-a", "source-b"],
        ["source-c"],
    ]
    assert all(call[1]["required_bands"] == ["g", "r"] for call in session.calls)
    assert all(call[1]["reference_catalog_name"] == "panstarrs_dr2__gaia_dr3" for call in session.calls)


@pytest.mark.parametrize(
    ("ready_catalog", "expected_status", "expected_namespace"),
    [
        ("panstarrs_dr2", "READY_EXISTING", "existing_global"),
        (
            "identity_scoped",
            "READY_EXISTING_SCOPED",
            "identity_scoped",
        ),
    ],
)
def test_reference_ensure_ready_coverage_does_not_fetch(
    monkeypatch, ready_catalog, expected_status, expected_namespace
):
    selected_ids = ["42", "84"]
    ready_catalog = (
        auto_process.identity_scoped_reference_catalog_name("panstarrs_dr2", "gaia_dr3")
        if ready_catalog == "identity_scoped"
        else ready_catalog
    )
    coverage_targets = []

    monkeypatch.setattr(
        auto_process,
        "load_persisted_identity_sources",
        lambda *_args, **_kwargs: [],
    )

    def count_coverage(_session, *, reference_catalog_name, **_kwargs):
        return {"g": 2, "r": 2} if reference_catalog_name == ready_catalog else {}

    def per_identity_coverage(
        _session,
        *,
        reference_catalog_name,
        selected_identity_source_ids,
        **_kwargs,
    ):
        coverage_targets.append((reference_catalog_name, list(selected_identity_source_ids)))
        return {source_id: {"g", "r"} for source_id in selected_identity_source_ids}

    monkeypatch.setattr(auto_process, "_reference_band_counts_for_series", count_coverage)
    monkeypatch.setattr(
        auto_process, "_reference_band_coverage_by_identity", per_identity_coverage
    )
    monkeypatch.setattr(
        auto_process,
        "create_reference_photometry_csv",
        lambda *_args, **_kwargs: pytest.fail("ready coverage must not fetch"),
    )
    monkeypatch.setattr(
        auto_process,
        "import_reference_photometry",
        lambda *_args, **_kwargs: pytest.fail("ready coverage must not import"),
    )

    result = auto_process._ensure_reference_photometry_for_series(
        object(),
        series={"series_id": 5, "field_id": 8},
        policy=_coverage_policy(),
        identity_catalog_name="gaia_dr3",
        selected_identity_source_ids=set(selected_ids),
        out_dir=None,
        allow_remote_fetch=True,
    )

    assert result["status"] == expected_status
    assert result["ready"] is True
    assert result["reference_namespace_mode"] == expected_namespace
    assert result["selected_identity_count"] == 2
    assert result["already_complete_identity_count"] == 2
    assert result["missing_identity_count"] == 0
    assert coverage_targets == [(ready_catalog, selected_ids)]


def test_reference_ensure_fetches_only_incomplete_identities_and_postchecks_full_selection(
    monkeypatch, tmp_path
):
    selected_ids = ["42", "84", "99"]
    scoped_name = auto_process.identity_scoped_reference_catalog_name(
        "panstarrs_dr2", "gaia_dr3"
    )
    count_calls = []
    coverage_calls = []
    provider_ids = []
    importer_ids = []

    monkeypatch.setattr(
        auto_process,
        "load_persisted_identity_sources",
        lambda *_args, **_kwargs: [
            {"source_id": source_id, "ra_deg": float(index * 10 + 10), "dec_deg": 20.0}
            for index, source_id in enumerate(selected_ids)
        ],
    )

    def count_coverage(
        _session,
        *,
        reference_catalog_name,
        selected_identity_source_ids,
        **_kwargs,
    ):
        ids = list(selected_identity_source_ids)
        count_calls.append((reference_catalog_name, ids))
        if reference_catalog_name == "panstarrs_dr2":
            return {}
        if len(count_calls) == 2:
            return {"g": 2, "r": 3}
        return {"g": 3, "r": 3}

    def per_identity_coverage(
        _session,
        *,
        reference_catalog_name,
        selected_identity_source_ids,
        **_kwargs,
    ):
        coverage_calls.append((reference_catalog_name, list(selected_identity_source_ids)))
        return {
            "42": {"g", "r"},
            "84": {"g"},
            "99": {"r"},
        }

    def fetch(_session, **kwargs):
        provider_ids.append(kwargs["selected_identity_source_ids"])
        assert kwargs["selected_identity_catalog_name"] == "gaia_dr3"
        pd.DataFrame(
            [
                {"catalog_source_id": "ps1-84", "selected_identity_source_id": "84"},
                {"catalog_source_id": "ps1-99", "selected_identity_source_id": "99"},
            ]
        ).to_csv(kwargs["out_path"], index=False)
        return SimpleNamespace(
            raw_rows=2,
            written_rows=2,
            path=str(kwargs["out_path"]),
            filter_summary={
                "selected_reference_diagnostics": _provider_diagnostics(
                    provider_rows_received=2,
                    provider_rows_retained=2,
                    matched_rows=2,
                )
            },
        )

    def importer(_session, **kwargs):
        importer_ids.append(kwargs["selected_identity_source_ids"])
        assert kwargs["catalog_name"] == scoped_name
        assert kwargs["commit"] is False
        return SimpleNamespace(inserted_or_updated=2, matched_to_gaia=2, unmatched_to_gaia=0)

    monkeypatch.setattr(auto_process, "_reference_band_counts_for_series", count_coverage)
    monkeypatch.setattr(
        auto_process, "_reference_band_coverage_by_identity", per_identity_coverage
    )
    monkeypatch.setattr(auto_process, "create_reference_photometry_csv", fetch)
    monkeypatch.setattr(auto_process, "import_reference_photometry", importer)

    result = auto_process._ensure_reference_photometry_for_series(
        object(),
        series={"series_id": 5, "field_id": 8},
        policy=_coverage_policy(minimum_sources=3),
        identity_catalog_name="gaia_dr3",
        selected_identity_source_ids=set(selected_ids),
        out_dir=tmp_path,
        allow_remote_fetch=True,
    )

    assert provider_ids == importer_ids == [["84", "99"]]
    assert coverage_calls == [(scoped_name, selected_ids)]
    assert count_calls == [
        ("panstarrs_dr2", selected_ids),
        (scoped_name, selected_ids),
        (scoped_name, selected_ids),
    ]
    assert result["status"] == "FETCHED_IMPORTED"
    assert result["ready"] is True
    assert result["selected_identity_count"] == 3
    assert result["already_complete_identity_count"] == 1
    assert result["missing_identity_count"] == 2
    assert result["band_counts_after"] == {"g": 3, "r": 3}


def test_reference_ensure_with_no_missing_identities_but_short_minimum_is_nonfatal_no_fetch(
    monkeypatch,
):
    selected_ids = ["42", "84"]
    provider_calls = []
    count_calls = []

    monkeypatch.setattr(
        auto_process,
        "load_persisted_identity_sources",
        lambda *_args, **_kwargs: [],
    )

    def count_coverage(_session, *, reference_catalog_name, **_kwargs):
        count_calls.append(reference_catalog_name)
        return {} if reference_catalog_name == "panstarrs_dr2" else {"g": 2, "r": 2}

    monkeypatch.setattr(auto_process, "_reference_band_counts_for_series", count_coverage)
    monkeypatch.setattr(
        auto_process,
        "_reference_band_coverage_by_identity",
        lambda _session, *, selected_identity_source_ids, **_kwargs: {
            source_id: {"g", "r"} for source_id in selected_identity_source_ids
        },
    )
    monkeypatch.setattr(
        auto_process,
        "create_reference_photometry_csv",
        lambda *_args, **_kwargs: provider_calls.append("fetch"),
    )
    monkeypatch.setattr(
        auto_process,
        "import_reference_photometry",
        lambda *_args, **_kwargs: provider_calls.append("import"),
    )

    result = auto_process._ensure_reference_photometry_for_series(
        object(),
        series={"series_id": 5, "field_id": 8},
        policy=_coverage_policy(minimum_sources=3),
        identity_catalog_name="gaia_dr3",
        selected_identity_source_ids=set(selected_ids),
        out_dir=None,
        allow_remote_fetch=True,
    )

    assert result["status"] == "INSUFFICIENT_REFERENCE_COVERAGE"
    assert result["ready"] is False
    assert result["selected_identity_count"] == 2
    assert result["already_complete_identity_count"] == 2
    assert result["missing_identity_count"] == 0
    assert count_calls == [
        "panstarrs_dr2",
        auto_process.identity_scoped_reference_catalog_name("panstarrs_dr2", "gaia_dr3"),
    ]
    assert provider_calls == []
