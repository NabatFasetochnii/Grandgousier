import pytest

from grandgousier.quality.field_report import (
    _field_series_metadata,
    _frame_selection_mode,
    _resolve_field_series_filter,
)


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return self.rows


class FakeSession:
    def __init__(self, results):
        self.results = list(results)

    def execute(self, _stmt, _params=None):
        if not self.results:
            raise AssertionError("unexpected execute call")
        return FakeResult(self.results.pop(0))


def test_resolve_field_series_filter_loads_frame_ids_by_series_id():
    session = FakeSession(
        [
            [
                {
                    "series_id": 42,
                    "series_uid": "series_abc",
                    "field_id": 3,
                    "field_name": "field_a",
                    "instrument_unit_id": 9,
                    "filter_name": "I",
                    "time_start": None,
                    "time_end": None,
                    "mjd_start": 60206.1,
                    "mjd_end": 60206.2,
                    "n_frames": 2,
                    "grouping_method": "test",
                    "status": "READY",
                    "metadata_json": {"primary_object_name": "obj"},
                }
            ],
            [(1001,), (1002,)],
        ]
    )

    selection = _resolve_field_series_filter(
        session,
        series_id=42,
        field_series_name=None,
        frame_ids=None,
    )

    assert selection["series_id"] == 42
    assert selection["frame_ids"] == [1001, 1002]
    assert selection["instrument_unit_id"] == 9
    assert _field_series_metadata(selection)["selected_frame_ids_count"] == 2
    assert _frame_selection_mode(series_selection=selection, frame_ids=selection["frame_ids"]) == "field_series"


def test_resolve_field_series_filter_rejects_explicit_frames_with_series():
    with pytest.raises(ValueError, match="cannot be combined"):
        _resolve_field_series_filter(
            FakeSession([]),
            series_id=42,
            field_series_name=None,
            frame_ids=[1, 2],
        )


def _series_row(series_id=1, *, last_calibration_prefix="source_prefix"):
    return {
        "series_id": series_id,
        "series_uid": f"series_{series_id}",
        "field_id": 3,
        "field_name": "TIC420268415_01",
        "instrument_unit_id": 9,
        "filter_name": "I",
        "time_start": None,
        "time_end": None,
        "mjd_start": 60206.1,
        "mjd_end": 60206.2,
        "n_frames": 2,
        "grouping_method": "test",
        "status": "READY",
        "metadata_json": {"primary_object_name": "TIC420268415_01"},
        "last_calibration_prefix": last_calibration_prefix,
    }


def test_resolve_field_series_name_accepts_derived_calibration_prefix_via_overlap():
    session = FakeSession(
        [
            [],  # direct alias lookup: no stored v30 series/pipeline prefix
            [dict(_series_row(1), overlap_frames=18, match_reason="calibration_overlap")],
            [(1,), (2,), (3,)],
        ]
    )

    selection = _resolve_field_series_filter(
        session,
        series_id=None,
        field_series_name="master_test_20230919_s1_v30",
        frame_ids=None,
        calibration_version_id=62,
        calibration_names=["master_test_20230919_s1_v30_ps1_i_color_v1"],
    )

    assert selection["series_id"] == 1
    assert selection["match_reason"] == "calibration_overlap"
    assert selection["frame_ids"] == [1, 2, 3]


def test_resolve_field_series_name_accepts_source_pipeline_prefix_alias():
    session = FakeSession(
        [
            [_series_row(1, last_calibration_prefix="master_test_20230919_s1_v27")],
            [(1,), (2,)],
        ]
    )

    selection = _resolve_field_series_filter(
        session,
        series_id=None,
        field_series_name="master_test_20230919_s1_v30",
        frame_ids=None,
        calibration_version_id=62,
        calibration_names=[
            "master_test_20230919_s1_v30_ps1_i_color_v1",
            "master_test_20230919_s1_v27_ensemble_g_v1",
        ],
    )

    assert selection["series_id"] == 1
    assert selection["last_calibration_prefix"] == "master_test_20230919_s1_v27"
    assert selection["frame_ids"] == [1, 2]


def test_resolve_field_series_name_not_found_lists_available_series_without_unrelated_overlap():
    session = FakeSession(
        [
            [],
            [_series_row(1, last_calibration_prefix="master_test_20230919_s1_v27")],
        ]
    )

    with pytest.raises(ValueError) as excinfo:
        _resolve_field_series_filter(
            session,
            series_id=None,
            field_series_name="not_a_known_series",
            frame_ids=None,
            calibration_version_id=62,
            calibration_names=["master_test_20230919_s1_v30_ps1_i_color_v1"],
        )

    message = str(excinfo.value)
    assert "Available field_series values" in message
    assert "series_id=1" in message
    assert "last_calibration_prefix=master_test_20230919_s1_v27" in message
