from grandgousier.lightcurves.target import TargetResolution, export_target_light_curve_product


def test_target_export_prefers_explicit_frame_ids_over_series_selector(monkeypatch):
    calls = {}

    def fake_load_rows(session, **kwargs):
        calls.update(kwargs)
        return None, []

    monkeypatch.setattr(
        "grandgousier.lightcurves.target.load_science_light_curve_rows",
        fake_load_rows,
    )

    target = TargetResolution(resolved=True, object_id=6751)
    product = export_target_light_curve_product(
        object(),
        target=target,
        calibration_name="ensemble_v1",
        catalog_name="gaia",
        series_id=3,
        frame_ids=[19, 20, 21],
        reject_flags=set(),
    )

    assert product.rows_total == 0
    assert calls["series_id"] is None
    assert calls["frame_ids"] == [19, 20, 21]
    assert calls["object_id"] == 6751


def test_target_export_keeps_series_selector_when_no_explicit_frames(monkeypatch):
    calls = {}

    def fake_load_rows(session, **kwargs):
        calls.update(kwargs)
        return None, []

    monkeypatch.setattr(
        "grandgousier.lightcurves.target.load_science_light_curve_rows",
        fake_load_rows,
    )

    target = TargetResolution(resolved=True, object_id=6751)
    export_target_light_curve_product(
        object(),
        target=target,
        calibration_name="ensemble_v1",
        catalog_name="gaia",
        series_id=3,
        frame_ids=None,
        reject_flags=set(),
    )

    assert calls["series_id"] == 3
    assert calls["frame_ids"] is None


def test_cleanup_target_light_curve_artifacts_removes_stale_products(tmp_path):
    from grandgousier.lightcurves.target import cleanup_target_light_curve_artifacts

    prefix = "series_i_v1"
    stale_paths = [
        tmp_path / f"{prefix}_target_light_curve.csv",
        tmp_path / f"{prefix}_target_light_curve.json",
        tmp_path / f"{prefix}_target_light_curve.png",
    ]
    unrelated = tmp_path / "other_target_light_curve.csv"
    for path in stale_paths:
        path.write_text("stale", encoding="utf-8")
    unrelated.write_text("keep", encoding="utf-8")

    result = cleanup_target_light_curve_artifacts(
        out_dir=tmp_path,
        calibration_prefix=prefix,
    )

    assert result["existing_artifacts"] == 3
    assert result["removed_artifacts"] == 3
    assert all(not path.exists() for path in stale_paths)
    assert unrelated.exists()


def test_cleanup_target_light_curve_artifacts_dry_run_preserves_products(tmp_path):
    from grandgousier.lightcurves.target import cleanup_target_light_curve_artifacts

    prefix = "series_i_v1"
    stale = tmp_path / f"{prefix}_target_light_curve.json"
    stale.write_text("stale", encoding="utf-8")

    result = cleanup_target_light_curve_artifacts(
        out_dir=tmp_path,
        calibration_prefix=prefix,
        dry_run=True,
    )

    assert result["existing_artifacts"] == 1
    assert result["removed_artifacts"] == 0
    assert stale.exists()


def test_target_resolution_negative_limit_without_explicit_id_skips_database():
    from grandgousier.lightcurves.target import resolve_target_object

    class FailingSession:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("database must not be queried when target resolution is disabled")

    result = resolve_target_object(
        FailingSession(),
        calibration_name="science_v1",
        catalog_name="gaia",
        max_sep_arcsec=-1.0,
    )

    assert result.resolved is False
    assert result.method == "disabled"
    assert "disabled" in str(result.reason)
