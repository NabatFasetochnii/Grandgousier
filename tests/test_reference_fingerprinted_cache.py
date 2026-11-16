from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from grandgousier.archive import auto_process
from grandgousier.calibration.photometric_policy import ColorCalibrationPolicy


def _policy() -> ColorCalibrationPolicy:
    return ColorCalibrationPolicy(
        enabled=True,
        reference_catalog_name="panstarrs_dr2",
        reference_band="g",
        color_index="g-r",
        require_bands=("g", "r"),
        min_stars_per_frame=1,
    )


def _positions(source_ids=("42",)) -> list[dict]:
    return [
        {"source_id": source_id, "ra_deg": 10.0 + index, "dec_deg": 20.0}
        for index, source_id in enumerate(sorted(source_ids))
    ]


def _fingerprint(source_ids=("42",), positions=None, **overrides):
    options = {
        "provider": "panstarrs_dr2",
        "provider_release": "dr2",
        "identity_catalog_name": "gaia_dr3",
        "missing_identity_source_ids": list(source_ids),
        "persisted_identity_sources": positions or _positions(source_ids),
        "required_bands": ["g", "r"],
        "filters": {
            "require_bands": ["g", "r"],
            "min_mag": None,
            "max_mag": None,
            "max_mag_err": None,
            "min_detections": None,
            "use_remote_filters": True,
            "limit": None,
        },
        "match_radius_arcsec": 1.0,
    }
    options.update(overrides)
    return auto_process._selected_reference_cache_fingerprint(**options)


def _configure(monkeypatch, *, provider, importer):
    ids = ["42"]
    monkeypatch.setattr(
        auto_process,
        "load_persisted_identity_sources",
        lambda *_args, **_kwargs: _positions(ids),
    )
    monkeypatch.setattr(
        auto_process,
        "_reference_band_counts_for_series",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        auto_process,
        "_reference_band_coverage_by_identity",
        lambda _session, *, selected_identity_source_ids, **_kwargs: {
            source_id: set() for source_id in selected_identity_source_ids
        },
    )
    monkeypatch.setattr(auto_process, "create_reference_photometry_csv", provider)
    monkeypatch.setattr(auto_process, "import_reference_photometry", importer)


def _ensure(tmp_path, session=None, process_local_cache=None):
    return auto_process._ensure_reference_photometry_for_series(
        session or SimpleNamespace(rollback=lambda: None),
        series={"series_id": 5, "field_id": 8},
        policy=_policy(),
        identity_catalog_name="gaia_dr3",
        selected_identity_source_ids={"42"},
        out_dir=tmp_path,
        allow_remote_fetch=True,
        process_local_cache=process_local_cache,
    )


def _provider_diagnostics(**overrides):
    diagnostics = {
        "logical_query_groups": 1,
        "provider_requests": 1,
        "provider_response_bytes_received": 42,
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


def _provider_result(out_path: str | Path, diagnostics=None) -> SimpleNamespace:
    diagnostics = diagnostics or _provider_diagnostics()
    Path(out_path).write_text(
        "catalog_source_id,selected_identity_source_id\nps1-42,42\n",
        encoding="utf-8",
    )
    return SimpleNamespace(
        raw_rows=diagnostics["provider_rows_received"],
        written_rows=diagnostics["provider_rows_retained"],
        path=str(out_path),
        filter_summary={"selected_reference_diagnostics": diagnostics},
    )


def _successful_import(_session, **kwargs):
    assert kwargs["commit"] is False
    return SimpleNamespace(
        inserted_or_updated=1,
        matched_to_gaia=1,
        unmatched_to_gaia=0,
    )


def test_fingerprint_is_canonical_and_sensitive_to_acquisition_inputs():
    ids = ["42", "84"]
    positions = _positions(ids)
    base, _ = _fingerprint(ids, positions)
    reordered, _ = _fingerprint(list(reversed(ids)), list(reversed(positions)))
    assert reordered == base

    moved_positions = [dict(source) for source in positions]
    moved_positions[0]["ra_deg"] += 0.001
    changed_grouping = auto_process._selected_reference_grouping_algorithm()
    changed_grouping["limits"]["max_selected_groups"] += 1
    changed_filters = _fingerprint(ids, positions)[1]["filters"] | {
        "max_mag": {"g": 18.0}
    }

    variants = [
        _fingerprint(["42"], _positions(["42"]))[0],
        _fingerprint(ids, moved_positions)[0],
        _fingerprint(ids, positions, required_bands=["g"])[0],
        _fingerprint(ids, positions, filters=changed_filters)[0],
        _fingerprint(ids, positions, match_radius_arcsec=2.0)[0],
        _fingerprint(ids, positions, grouping_algorithm=changed_grouping)[0],
        _fingerprint(ids, positions, provider_release="dr3")[0],
        _fingerprint(ids, positions, identity_catalog_name="other_catalog")[0],
    ]
    assert all(value != base for value in variants)


def test_compatible_cache_is_reused_without_a_second_provider_request(monkeypatch, tmp_path):
    provider_calls = []
    import_calls = []

    def provider(_session, **kwargs):
        provider_calls.append(kwargs["out_path"])
        return _provider_result(kwargs["out_path"])

    def importer(session, **kwargs):
        import_calls.append(Path(kwargs["path"]))
        return _successful_import(session, **kwargs)

    _configure(monkeypatch, provider=provider, importer=importer)

    first = _ensure(tmp_path)
    second = _ensure(tmp_path)

    assert first["cache_miss"] is True
    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert second["cache_miss"] is False
    assert len(provider_calls) == 1
    assert len(import_calls) == 2
    assert Path(second["csv_path"]).is_file()
    assert Path(second["manifest_path"]).is_file()
    assert second["cache_provenance"]["complete"] is True
    assert second["cache_provenance"]["fingerprint"] == second["cache_fingerprint"]
    assert {
        "schema_version",
        "fingerprint",
        "provider",
        "provider_release",
        "identity_catalog_name",
        "identity_count",
        "identity_set_sha256",
        "identity_positions_sha256",
        "required_bands",
        "filters",
        "match_radius_arcsec",
        "grouping_algorithm",
        "logical_query_groups",
        "provider_requests",
        "rows_received",
        "retained_rows",
        "selected_reference_diagnostics",
        "csv_sha256",
        "complete",
    }.issubset(second["cache_provenance"])
    assert second["cache_provenance"]["csv_sha256"] == hashlib.sha256(
        Path(second["csv_path"]).read_bytes()
    ).hexdigest()
    assert second["cache_provenance"]["schema_version"] == 3
    manifest = second["cache_provenance"]
    diagnostics = manifest["selected_reference_diagnostics"]
    assert manifest["logical_query_groups"] == diagnostics["logical_query_groups"] == 1
    assert manifest["provider_requests"] == diagnostics["provider_requests"] == 1
    assert manifest["rows_received"] == diagnostics["provider_rows_received"] == 1
    assert manifest["retained_rows"] == diagnostics["provider_rows_retained"] == 1


def test_process_local_cache_scope_and_disk_recovery(monkeypatch, tmp_path):
    provider_calls = []
    import_calls = []
    validation_calls = []
    diagnostics = _provider_diagnostics(
        logical_query_groups=2,
        provider_requests=2,
        provider_response_bytes_received=123,
        provider_rows_received=6,
        provider_rows_retained=1,
        provider_exact_duplicates_collapsed=1,
        provider_conflicting_source_ids=1,
        provider_conflicting_rows=2,
        matched_rows=2,
        ambiguous_rows=1,
        ambiguous_identities=1,
        unmatched_rows=2,
        unmatched_identities=1,
    )

    def provider(_session, **kwargs):
        provider_calls.append(kwargs["out_path"])
        return _provider_result(kwargs["out_path"], diagnostics)

    def importer(session, **kwargs):
        import_calls.append(Path(kwargs["path"]))
        return _successful_import(session, **kwargs)

    _configure(monkeypatch, provider=provider, importer=importer)
    validator = auto_process._validate_selected_reference_cache

    def record_validation(**kwargs):
        validation_calls.append(kwargs["fingerprint"])
        return validator(**kwargs)

    monkeypatch.setattr(auto_process, "_validate_selected_reference_cache", record_validation)

    first_invocation_cache = {}
    first = _ensure(tmp_path, process_local_cache=first_invocation_cache)
    local_hit = _ensure(tmp_path, process_local_cache=first_invocation_cache)

    assert first["cache_miss"] is True
    assert local_hit["cache_hit"] is True
    assert len(validation_calls) == 1  # The second call reuses the validated process-local entry.

    second_invocation_cache = {}
    disk_hit = _ensure(tmp_path, process_local_cache=second_invocation_cache)

    assert second_invocation_cache is not first_invocation_cache
    assert disk_hit["cache_hit"] is True
    assert len(validation_calls) == 2  # A new invocation recovers through validated disk cache.
    assert len(provider_calls) == 1
    assert len(import_calls) == 3
    assert second_invocation_cache[first["cache_fingerprint"]]

    for key, expected in diagnostics.items():
        assert local_hit[key] == expected
        assert disk_hit[key] == expected
        assert local_hit["cache_provenance"]["selected_reference_diagnostics"][key] == expected
    assert local_hit["selected_identity_count"] == 1
    assert local_hit["already_complete_identity_count"] == 0
    assert local_hit["missing_identity_count"] == 1
    assert local_hit["imported_or_updated"] == 1
    assert local_hit["band_counts_before"] == {}
    assert local_hit["band_counts_after"] == {}
    manifest_diagnostics = local_hit["cache_provenance"][
        "selected_reference_diagnostics"
    ]
    assert all(type(value) is int for value in manifest_diagnostics.values())
    assert all(not isinstance(value, str) for value in manifest_diagnostics.values())


def test_import_rollback_retry_reuses_completed_provider_cache(monkeypatch, tmp_path):
    provider_calls = []
    importer_calls = []
    session = SimpleNamespace(rollback_count=0)
    session.rollback = lambda: setattr(session, "rollback_count", session.rollback_count + 1)

    def provider(_session, **kwargs):
        provider_calls.append(kwargs["out_path"])
        return _provider_result(kwargs["out_path"])

    def importer(_session, **kwargs):
        importer_calls.append(Path(kwargs["path"]))
        assert kwargs["commit"] is False
        if len(importer_calls) == 1:
            raise RuntimeError("synthetic import rollback")
        return _successful_import(_session, **kwargs)

    _configure(monkeypatch, provider=provider, importer=importer)

    process_local_cache = {}
    failed = _ensure(tmp_path, session, process_local_cache)
    retried = _ensure(tmp_path, session, process_local_cache)

    assert failed["status"] == "REFERENCE_ACQUISITION_FAILED"
    assert retried["cache_hit"] is True
    assert retried["status"] == "INSUFFICIENT_REFERENCE_COVERAGE"
    assert session.rollback_count == 1
    assert len(provider_calls) == 1
    assert importer_calls[0] == importer_calls[1] == Path(retried["csv_path"])
    assert retried["cache_hit"] is True
    assert retried["provider_rows_received"] == 1
    assert retried["matched_rows"] == 1


@pytest.mark.parametrize(
    "corruption",
    [
        "incomplete_manifest",
        "csv_hash",
        *[
            (kind, key)
            for kind in ("malformed_count", "mismatched_count")
            for key in (
                "logical_query_groups",
                "provider_requests",
                "rows_received",
                "retained_rows",
            )
        ],
    ],
)
def test_invalid_cache_fails_closed_without_provider_or_import(
    monkeypatch, tmp_path, corruption
):
    provider_calls = []
    import_calls = []

    def provider(_session, **kwargs):
        provider_calls.append(kwargs["out_path"])
        return _provider_result(kwargs["out_path"])

    def importer(session, **kwargs):
        import_calls.append(kwargs["path"])
        return _successful_import(session, **kwargs)

    _configure(monkeypatch, provider=provider, importer=importer)
    created = _ensure(tmp_path)
    manifest_path = Path(created["manifest_path"])
    csv_path = Path(created["csv_path"])
    if corruption == "incomplete_manifest":
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["complete"] = False
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif corruption == "csv_hash":
        csv_path.write_bytes(csv_path.read_bytes() + b"corruption\n")
    else:
        kind, key = corruption
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest[key] = "1" if kind == "malformed_count" else manifest[key] + 1
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    rejected = _ensure(tmp_path)

    assert rejected["status"] == "REFERENCE_CACHE_INVALID"
    assert rejected["cache_invalid"] is True
    assert len(provider_calls) == 1
    assert len(import_calls) == 1


def test_concurrent_existing_cache_artifact_is_never_overwritten(monkeypatch, tmp_path):
    fingerprint, _provenance = _fingerprint()
    cache_dir = tmp_path / "reference_photometry"
    cache_dir.mkdir()
    csv_path = cache_dir / f"panstarrs_dr2_{fingerprint}.csv"
    existing_bytes = b"user-owned concurrent artifact\n"
    provider_calls = []
    import_calls = []

    def provider(_session, **kwargs):
        provider_calls.append(kwargs["out_path"])
        result = _provider_result(kwargs["out_path"])
        csv_path.write_bytes(existing_bytes)
        return result

    def importer(session, **kwargs):
        import_calls.append(kwargs["path"])
        return _successful_import(session, **kwargs)

    _configure(monkeypatch, provider=provider, importer=importer)
    result = _ensure(tmp_path)

    assert result["status"] == "REFERENCE_ACQUISITION_FAILED"
    assert "FileExistsError" in result["reason"]
    assert csv_path.read_bytes() == existing_bytes
    assert len(provider_calls) == 1
    assert import_calls == []
