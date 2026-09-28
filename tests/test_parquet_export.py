from datetime import UTC, datetime

import pytest

from grandgousier.lightcurves import parquet_export as export
from grandgousier.lightcurves.product import _science_row_from_db, light_curve_reject_flags


def _tables():
    return {
        "sources.parquet": iter([{"object_uid": "opaque-a", "ra_deg": 12.5, "dec_deg": None,
            "identity_scheme": "INTERNAL_UUID_V1", "catalog_links_json": '[{"catalog_name":"Gaia","source_id":"42"}]'}]),
        "frames.parquet": iter([{"series_uid": "series-a", "observation_uid": "obs-a", "frame_uid": "frame-a",
            "identity_scheme": "MASTER_UNIQNUM", "native_identity": "204_1", "date_obs": datetime(2024, 1, 2, 11, tzinfo=UTC),
            "observing_night": "2024-01-01", "filter_name": "V", "instrument": "MASTER", "site": None,
            "tube": None, "camera": None, "exptime": 30.0, "frame_quality_flags_json": "{}", "frame_qc_json": "{}"},
            {"series_uid": "series-a", "observation_uid": "obs-zero", "frame_uid": "frame-zero", "identity_scheme": None,
            "native_identity": None, "date_obs": None, "observing_night": None, "filter_name": "V", "instrument": "MASTER",
            "site": None, "tube": None, "camera": None, "exptime": None, "frame_quality_flags_json": "{}", "frame_qc_json": "{}"}]),
        "photometry.parquet": iter([{"object_uid": "opaque-a", "observation_uid": "obs-a", "frame_uid": "frame-a",
            "identity_scheme": "MASTER_UNIQNUM", "native_identity": "204_1", "series_uid": "series-a",
            "mag_inst": None, "mag_cal": 13.2, "flux": 10.0, "flux_err": None, "mag_err": None,
            "mag_err_random": 0.02, "mag_err_systematic": None, "mag_global": 13.2, "mag_global_err": 0.02,
            "mag_global_err_random": 0.02, "mag_global_err_systematic": None, "interseries_offset_mag": None,
            "interseries_offset_err": None, "interseries_offset_status": "NO_INTERSERIES_OFFSET",
            "local_zp_correction": None, "aperture_radius_pix": 3.0, "measurement_flags_json": "{}",
            "object_qc_flags_json": "{}", "quality_flags_json": "{}", "is_rejected": False, "reject_reasons_json": "[]",
            "observing_night": "2024-01-01", "calibration_name": "science-v1"}]),
        "series.parquet": iter([{"series_uid": "series-a", "field_uid": "field-a", "filter_name": "V", "status": "READY",
            "frame_count": 2, "science_row_count": 1, "recipe_json": "{}", "calibration_name": "science-v1",
            "stage2_pipeline_status": None, "stage2_science_status": None, "calibration_role": "SCIENCE",
             "calibration_lifecycle_status": "COMPLETE_IMMUTABLE", "stage3_generation_uid": None, "stage3_status": None,
             "stage3_offset_mag": None, "interseries_offset_status": "NO_INTERSERIES_OFFSET",
             "object_qc_good_count": 0, "object_qc_suspect_count": 0, "object_qc_bad_count": 0,
             "frame_qc_good_count": 0, "frame_qc_suspect_count": 0, "frame_qc_bad_count": 0,
             "qc_status": "UNASSESSED"}]),
    }


def test_utc_observing_night_uses_noon_boundary():
    assert export.observing_night("2024-01-02T11:59:59Z") == "2024-01-01"
    assert export.observing_night(datetime(2024, 1, 2, 12, tzinfo=UTC)) == "2024-01-02"
    assert export.observing_night(None) is None


def test_photometry_qc_key_uses_base_measurement_and_science_slots():
    from types import SimpleNamespace

    # Match photometry_iterator's SELECT order: BASE Measurement is slot 7,
    # SciencePhotometry is slot 6; slots 3 and 4 are identity strings.
    row = (
        "object-uid", "observation-uid", "frame-uid", "MASTER_UNIQNUM",
        "204_1", "series-uid", SimpleNamespace(calibration_version_id=23),
        SimpleNamespace(object_id=17), "calibration-name", None, {},
    )
    key = (int(row[7].object_id), int(row[6].calibration_version_id))
    assert key == (17, 23)


def test_explicit_schema_round_trip_nullable_and_zero_measurement_frame(tmp_path):
    pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    target = tmp_path / "product"
    result = export.export_parquet_product(_tables(), target, metadata={"database_logical_id": "test"}, batch_size=1)
    assert result["row_counts"] == {"sources": 1, "frames": 2, "photometry": 1, "series": 1}
    frames = pq.read_table(target / "frames.parquet")
    photometry = pq.read_table(target / "photometry.parquet")
    assert frames.schema == export._schemas()["frames.parquet"]
    assert frames.num_rows == 2 and frames.column("frame_uid").to_pylist()[-1] == "frame-zero"
    assert photometry.column("mag_inst").to_pylist() == [None]
    assert "object_uid" in photometry.column_names and "object_id" not in photometry.column_names
    assert result["files"]["photometry.parquet"]["sha256"]


def test_output_must_be_absolute_and_nonexistent(tmp_path):
    with pytest.raises(ValueError, match="absolute"):
        export.export_parquet_product(_tables(), "relative", metadata={})
    target = tmp_path / "existing"
    target.mkdir()
    with pytest.raises(FileExistsError):
        export.export_parquet_product(_tables(), target, metadata={})


def test_shared_science_rejection_and_global_offset_contract():
    row = {"measurement_flags": {"NO_DETECTION_SUPPORT": True}, "object_qc_flags": {},
           "frame_quality_flags": {}, "mag_cal": 12.0, "mag_err": .1,
           "interseries_offset_mag": None}
    strict = _science_row_from_db(row, reject_flags=light_curve_reject_flags())
    relaxed = _science_row_from_db(row, reject_flags=light_curve_reject_flags(require_detection_support=False))
    assert strict["is_rejected"] is True
    assert relaxed["is_rejected"] is False
    assert strict["interseries_offset_status"] == "NO_INTERSERIES_OFFSET"
    adjusted = _science_row_from_db({**row, "interseries_offset_mag": .25}, reject_flags=set())
    assert adjusted["mag_global"] == 12.25


def test_frame_qc_summary_uses_nested_quality_flags_status():
    from grandgousier.calibration.photometric_frame_qc import PHOTOMETRIC_FRAME_QC_FLAG

    flags = {PHOTOMETRIC_FRAME_QC_FLAG: {"status": "BAD", "metric": 1}}
    encoded, counts = export._frame_qc_summary(flags)
    assert '"metric": 1' in encoded and counts == {"GOOD": 0, "SUSPECT": 0, "BAD": 1}


def test_frame_qc_count_query_reuses_jsonb_status_expression():
    from sqlalchemy.dialects import postgresql

    from grandgousier.db.models import FieldSeriesFrame, Frame

    statement = export._frame_qc_counts_statement([1, 2], FieldSeriesFrame, Frame)
    selected_status = statement.selected_columns[1]
    grouped_status = tuple(statement._group_by_clauses)[1]
    assert selected_status is grouped_status

    compiled = statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "frames.quality_flags[" in sql and "->>" in sql
    assert sql.count("frames.quality_flags[") == 2
    assert compiled.params["quality_flags_1"] == "frame_photometric_qc_v1"


def test_persisted_frame_qc_statuses_count_13_10_2_and_bad_rejects():
    from grandgousier.calibration.photometric_frame_qc import (
        BAD_FRAME_REJECT_FLAG,
        PHOTOMETRIC_FRAME_QC_FLAG,
    )

    frame_flags = [
        {PHOTOMETRIC_FRAME_QC_FLAG: {"status": status}, BAD_FRAME_REJECT_FLAG: status == "BAD"}
        for status, count in (("GOOD", 13), ("SUSPECT", 10), ("BAD", 2))
        for _ in range(count)
    ]
    summaries = [export._frame_qc_summary(flags)[1] for flags in frame_flags]
    counts = {status: sum(row[status] for row in summaries) for status in ("GOOD", "SUSPECT", "BAD")}
    assert counts == {"GOOD": 13, "SUSPECT": 10, "BAD": 2}
    assert export._series_qc_status(counts, 25) == "DEGRADED"

    rejected = _science_row_from_db(
        {"measurement_flags": {}, "object_qc_flags": {}, "frame_quality_flags": frame_flags[-1],
         "mag_cal": 12.0, "mag_err": .1},
        reject_flags=light_curve_reject_flags(extra_flags=BAD_FRAME_REJECT_FLAG),
    )
    assert rejected["is_rejected"] is True
    assert BAD_FRAME_REJECT_FLAG in rejected["reject_reasons_list"]


@pytest.mark.parametrize(("counts", "frame_count", "expected"), [
    ({"GOOD": 2, "SUSPECT": 0, "BAD": 0}, 2, "GOOD"),
    ({"GOOD": 1, "SUSPECT": 1, "BAD": 0}, 2, "DEGRADED"),
    ({"GOOD": 1, "SUSPECT": 0, "BAD": 1}, 2, "DEGRADED"),
    ({"GOOD": 1, "SUSPECT": 0, "BAD": 1}, 3, "UNASSESSED"),
])
def test_series_qc_status_from_persisted_frame_classifications(counts, frame_count, expected):
    assert export._series_qc_status(counts, frame_count) == expected


def test_series_qc_status_is_separate_from_processor_lifecycle_status():
    series_status = "PROCESSED"
    qc_status = export._series_qc_status({"GOOD": 1, "SUSPECT": 0, "BAD": 0}, 1)
    assert series_status == "PROCESSED"
    assert qc_status == "GOOD"


def test_effective_detection_support_recipe_key_wins_over_base_key():
    assert export._effective_detection_support({"require_detection_support": True,
                                                "require_detection_support_effective": False}) is False


@pytest.mark.parametrize("recipe,expected", [
    ({"enable_photometric_frame_qc": True}, True),
    ({"enable_photometric_frame_qc": False}, False),
    ({"enable_photometric_frame_qc": False,
      "quality_policy": {"enable_photometric_frame_qc": True}}, True),
    ({"enable_photometric_frame_qc": True,
      "quality_policy": {"enable_photometric_frame_qc_effective": False,
                         "enable_photometric_frame_qc": True}}, False),
])
def test_effective_photometric_frame_qc_recipe_precedence(recipe, expected):
    assert export._effective_photometric_frame_qc(recipe) is expected


def test_stage3_explicit_offset_and_stage2_only_null_global_semantics():
    base = {"measurement_flags": {}, "object_qc_flags": {}, "frame_quality_flags": {},
            "mag_cal": 12.0, "mag_err": .1, "mag_err_random": .08,
            "mag_err_systematic": .06}
    explicit = export._stage3_values({**base, "interseries_offset_mag": .25,
        "interseries_offset_err": .03, "interseries_offset_status": "OFFSET_APPLIED"},
        requested=True, reject_flags=set())
    assert explicit["mag_global"] == 12.25
    assert explicit["interseries_offset_mag"] == .25
    stage2 = export._stage3_values(base, requested=False, reject_flags=set())
    assert stage2["mag_global"] is None and stage2["mag_global_err"] is None
    assert stage2["interseries_offset_status"] == "NOT_REQUESTED"


def test_schema_and_manifest_contract_does_not_expose_surrogate_cv_ids():
    pytest.importorskip("pyarrow")
    assert "calibration_version_id" not in export._schemas()["photometry.parquet"].names
    assert "calibration_version_id" not in export._schemas()["series.parquet"].names
    assert {"frame_uid", "identity_scheme", "native_identity"} <= set(
        export._schemas()["photometry.parquet"].names
    )


def test_photometry_identity_distinguishes_frames_and_requires_frame_uid(tmp_path):
    pa = pytest.importorskip("pyarrow")
    tables = _tables()
    row = next(iter(tables["photometry.parquet"]))
    duplicate_observation = {**row, "frame_uid": "frame-b"}
    tables["photometry.parquet"] = iter([row, duplicate_observation])
    result = export.export_parquet_product(tables, tmp_path / "two-frames", metadata={})
    assert result["row_counts"]["photometry"] == 2
    assert result["identity_set_sha256"]

    missing = _tables()
    missing_row = dict(next(iter(missing["photometry.parquet"])))
    missing_row["frame_uid"] = None
    missing["photometry.parquet"] = iter([missing_row])
    with pytest.raises(pa.ArrowInvalid, match="non-nullable"):
        export.export_parquet_product(missing, tmp_path / "missing-frame", metadata={})


@pytest.mark.parametrize("analysis,graph,reliability", [
    ("INVALID", "OK", "REDUNDANT_GRAPH_OK"),
    ("READY_M1_M3", "INVALID", "REDUNDANT_GRAPH_OK"),
    ("READY_M1_M3", "OK", "SPARSE_GRAPH_NO_CLOSURE"),
    ("READY_M1_M3", "OK", "FAILED"),
])
def test_stage3_rejects_nonaccepted_status_triplets(analysis, graph, reliability):
    from types import SimpleNamespace

    with pytest.raises(ValueError, match="diagnostic-only"):
        export._validate_stage3_generation(SimpleNamespace(
            analysis_status=analysis, graph_status=graph, reliability_status=reliability
        ))


def test_stage3_acceptance_contract_matches_persisted_redundant_graph_statuses():
    from types import SimpleNamespace

    export._validate_stage3_generation(SimpleNamespace(
        analysis_status="READY_M1_M3", graph_status="OK", reliability_status="REDUNDANT_GRAPH_OK"
    ))


def test_unpersisted_qc_counts_are_nullable_not_fabricated():
    pytest.importorskip("pyarrow")
    schema = export._schemas()["series.parquet"]
    for column in ("object_qc_row_count", "object_qc_flag_count", "frame_qc_good_count",
                   "frame_qc_suspect_count", "frame_qc_bad_count"):
        assert schema.field(column).nullable


def test_failure_cleans_only_staged_directory(tmp_path):
    def broken():
        raise RuntimeError("query failed")
        yield
    tables = _tables()
    tables["sources.parquet"] = broken()
    target = tmp_path / "failed"
    with pytest.raises(RuntimeError):
        export.export_parquet_product(tables, target, metadata={})
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_non_postgres_export_session_is_refused_before_query():
    class Bind:
        class dialect:
            name = "sqlite"
    class Session:
        bind = Bind()
    with pytest.raises(RuntimeError, match="PostgreSQL"):
        export.export_series(Session(), [1], "C:/output", logical_db_id="test")
