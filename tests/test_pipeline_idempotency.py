from grandgousier.pipeline.idempotency import (
    IdempotencyCheckResult,
    PipelineIdempotencyReport,
    _Scope,
    _duplicate_check,
    idempotency_summary_rows,
)


class FakeScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value


class FakeMappingsResult:
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
        self.calls = []
        self.rollback_called = False

    def execute(self, stmt, params=None):
        self.calls.append((str(stmt), dict(params or {})))
        if not self.results:
            raise AssertionError("unexpected execute call")
        value = self.results.pop(0)
        if isinstance(value, int):
            return FakeScalarResult(value)
        return FakeMappingsResult(value)

    def rollback(self):
        self.rollback_called = True


def test_duplicate_check_reports_violation_and_extra_rows():
    session = FakeSession(
        [
            10,
            [{"duplicate_groups": 2, "duplicate_extra_rows": 3}],
            [{"object_id": 1, "frame_id": 2, "calibration_version_id": 3, "n": 2}],
        ]
    )
    scope = _Scope(
        calibration_version_id=3,
        catalog_name="gaia",
        instrument_unit_id=1,
        frame_ids=[1, 2],
        object_qc_algorithm="object_qc_v1",
        detection_algorithm="numpy_localmax_v1",
        detection_match_algorithm="nearest_sky_v1",
        frame_qc_algorithm="detection_catalog_qc_v1",
        example_limit=5,
    )

    check = _duplicate_check(
        session,
        name="measurements",
        table_name="measurements",
        identity=["object_id", "frame_id", "calibration_version_id"],
        from_sql="measurements m",
        where_sql="TRUE",
        group_sql="m.object_id, m.frame_id, m.calibration_version_id",
        select_sql="m.object_id, m.frame_id, m.calibration_version_id",
        params={"frame_ids": scope.frame_ids},
        example_limit=scope.example_limit,
    )

    assert check.status == "VIOLATION"
    assert check.total_rows == 10
    assert check.duplicate_groups == 2
    assert check.duplicate_extra_rows == 3
    assert check.examples[0]["object_id"] == 1


def test_duplicate_check_reports_ok_when_no_groups():
    session = FakeSession([7, [{"duplicate_groups": 0, "duplicate_extra_rows": 0}], []])

    check = _duplicate_check(
        session,
        name="frame_qc",
        table_name="frame_qc",
        identity=["frame_id", "catalog_name", "algorithm"],
        from_sql="frame_qc fq",
        where_sql="TRUE",
        group_sql="fq.frame_id, fq.catalog_name, fq.algorithm",
        select_sql="fq.frame_id, fq.catalog_name, fq.algorithm",
        params={},
        example_limit=5,
    )

    assert check.status == "OK"
    assert check.total_rows == 7
    assert check.duplicate_groups == 0
    assert check.duplicate_extra_rows == 0
    assert check.examples == []


def test_idempotency_summary_rows_include_scope_and_checks():
    report = PipelineIdempotencyReport(
        status="VIOLATION",
        calibration_name="cal_v1",
        calibration_version_id=42,
        catalog_name="gaia",
        instrument_unit_id=1,
        frame_selection_mode="field_series",
        selected_frame_ids_count=18,
        series_id=1,
        field_name="field",
        checks=[
            IdempotencyCheckResult(
                name="measurements",
                table_name="measurements",
                identity=["object_id", "frame_id", "calibration_version_id"],
                total_rows=100,
                duplicate_groups=1,
                duplicate_extra_rows=2,
                status="VIOLATION",
            )
        ],
    )

    rows = idempotency_summary_rows(report)
    assert {row["metric"]: row["value"] for row in rows if row["section"] == "scope"}["selected_frame_ids_count"] == 18
    measurement_rows = [row for row in rows if row["section"] == "measurements"]
    assert {row["metric"]: row["value"] for row in measurement_rows}["duplicate_groups"] == 1
