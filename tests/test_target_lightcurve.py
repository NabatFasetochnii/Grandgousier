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
