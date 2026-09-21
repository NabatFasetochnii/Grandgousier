from __future__ import annotations

from types import SimpleNamespace

from grandgousier.services.acceptance import evaluate_acceptance, load_acceptance_spec


def _actual_from_spec(spec):
    stage2 = spec["stage2"]
    field_status = {
        "status": stage2["status"],
        "stage2": {
            "plan_status": stage2["plan_status"],
            "n_series": stage2["n_series"],
            "n_ready_series": stage2["n_ready_series"],
            "n_eligible_pairs": stage2["n_eligible_pairs"],
            "largest_component_size": stage2["largest_component_size"],
            "recipe_compatibility_counts": stage2["recipe_compatibility_counts"],
            "connected_components": stage2["connected_components"],
        },
        "stage3": [
            {key: value for key, value in row.items() if key != "reduced_chi2_abs_tol"}
            for row in spec["stage3"]
        ],
    }
    controls = {
        int(row["object_id"]): dict(row)
        for row in spec["m5"]
    }
    return field_status, controls


def test_packaged_gpx_acceptance_spec_loads():
    spec = load_acceptance_spec("gpx_tf17_2")
    assert spec["format"] == "grandgousier_acceptance_v1"
    assert spec["field_id"] == 33
    assert len(spec["stage3"]) == 2
    assert {row["object_id"] for row in spec["m5"]} == {29786, 30112}


def test_evaluate_acceptance_accepts_exact_frozen_baseline():
    spec = load_acceptance_spec("gpx_tf17_2")
    field_status, controls = _actual_from_spec(spec)
    checks = evaluate_acceptance(
        spec,
        doctor_status="READY",
        db_before=spec["database"],
        field_status=field_status,
        controls=controls,
        db_after=spec["database"],
    )
    assert checks
    assert all(row.status == "OK" for row in checks)
    assert {row.name for row in checks} >= {
        "full_science_environment",
        "database_baseline",
        "stage2_readiness",
        "stage3_V_unit7",
        "stage3_R_unit8",
        "m5_object_29786",
        "m5_object_30112",
        "no_unexpected_db_writes",
    }


def test_evaluate_acceptance_detects_stage3_science_drift():
    spec = load_acceptance_spec("gpx_tf17_2")
    field_status, controls = _actual_from_spec(spec)
    field_status["stage3"][0]["reduced_chi2"] += 0.01
    checks = evaluate_acceptance(
        spec,
        doctor_status="READY",
        db_before=spec["database"],
        field_status=field_status,
        controls=controls,
        db_after=spec["database"],
    )
    row = next(item for item in checks if item.name == "stage3_V_unit7")
    assert row.status == "FAIL"
    assert "reduced_chi2" in row.detail


def test_evaluate_acceptance_detects_m5_rejection_drift():
    spec = load_acceptance_spec("gpx_tf17_2")
    field_status, controls = _actual_from_spec(spec)
    controls[29786] = dict(controls[29786])
    controls[29786]["accepted_rows"] = 2579
    checks = evaluate_acceptance(
        spec,
        doctor_status="READY",
        db_before=spec["database"],
        field_status=field_status,
        controls=controls,
        db_after=spec["database"],
    )
    row = next(item for item in checks if item.name == "m5_object_29786")
    assert row.status == "FAIL"
    assert "accepted_rows" in row.detail


def test_evaluate_acceptance_detects_db_write_drift():
    spec = load_acceptance_spec("gpx_tf17_2")
    field_status, controls = _actual_from_spec(spec)
    after = dict(spec["database"])
    after["frames"] += 1
    checks = evaluate_acceptance(
        spec,
        doctor_status="READY",
        db_before=spec["database"],
        field_status=field_status,
        controls=controls,
        db_after=after,
    )
    row = next(item for item in checks if item.name == "no_unexpected_db_writes")
    assert row.status == "FAIL"


def test_evaluate_acceptance_requires_full_science_doctor_ready():
    spec = load_acceptance_spec("gpx_tf17_2")
    field_status, controls = _actual_from_spec(spec)
    checks = evaluate_acceptance(
        spec,
        doctor_status="READY_WITH_WARNINGS",
        db_before=spec["database"],
        field_status=field_status,
        controls=controls,
        db_after=spec["database"],
    )
    row = next(item for item in checks if item.name == "full_science_environment")
    assert row.status == "FAIL"
