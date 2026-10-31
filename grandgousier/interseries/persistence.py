from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from grandgousier.interseries.analysis import (
    INTERSERIES_ANALYSIS_ALGORITHM,
    INTERSERIES_SIGN_CONVENTION,
    InterseriesAnalysis,
    analyze_interseries_calibration,
)


INTERSERIES_PERSISTENCE_ALGORITHM = "interseries_generation_v1"
RELIABILITY_REDUNDANT = "REDUNDANT_GRAPH_OK"
RELIABILITY_SPARSE = "SPARSE_GRAPH_NO_CLOSURE"
LIFECYCLE_COMPLETE = "COMPLETE_IMMUTABLE"


@dataclass(frozen=True)
class PersistedInterseriesGeneration:
    interseries_generation_id: int
    generation_uid: str
    fingerprint_sha256: str
    field_id: int
    field_name: str | None
    instrument_unit_id: int | None
    filter_name: str | None
    catalog_name: str | None
    analysis_algorithm: str
    reference_series_id: int
    analysis_status: str
    graph_status: str
    reliability_status: str
    n_series: int
    n_edges: int
    graph_dof: int
    chi2: float
    reduced_chi2: float | None
    max_abs_standardized_residual: float | None
    min_points_per_object: int
    min_common_objects: int
    clip_sigma: float
    max_clip_iterations: int
    lifecycle_status: str
    created: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class InterseriesVerification:
    interseries_generation_id: int
    generation_uid: str
    status: str
    fingerprint_ok: bool
    source_calibrations_ok: bool
    graph_ok: bool
    n_series: int
    n_ok_edges: int
    graph_dof: int
    reliability_status: str
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify_graph_reliability(*, n_series: int, n_edges: int, dof: int, graph_status: str) -> str:
    if graph_status != "OK":
        return graph_status
    if n_series >= 2 and n_edges >= n_series and dof > 0:
        return RELIABILITY_REDUNDANT
    if n_series >= 2 and n_edges == n_series - 1 and dof == 0:
        return RELIABILITY_SPARSE
    if n_edges < max(0, n_series - 1):
        return "DISCONNECTED"
    return "GRAPH_OK_NO_REDUNDANCY"


def build_generation_input_snapshot(analysis: InterseriesAnalysis) -> dict[str, Any]:
    by_series = {row.series_id: row for row in analysis.series_summaries}
    sources = [
        {
            "series_id": int(series_id),
            "calibration_version_id": int(by_series[series_id].calibration_version_id),
        }
        for series_id in sorted(analysis.selected_series_ids)
    ]
    units = sorted(
        {
            int(row.instrument_unit_id)
            for row in analysis.series_summaries
            if row.instrument_unit_id is not None
        }
    )
    filters = sorted({str(row.filter_name) for row in analysis.series_summaries if row.filter_name is not None})
    catalogs = sorted({str(row.catalog_name) for row in analysis.series_summaries if row.catalog_name is not None})
    return {
        "persistence_algorithm": INTERSERIES_PERSISTENCE_ALGORITHM,
        "analysis_algorithm": analysis.algorithm,
        "sign_convention": analysis.sign_convention,
        "field_id": int(analysis.field_id),
        "instrument_unit_id": units[0] if len(units) == 1 else None,
        "filter_name": filters[0] if len(filters) == 1 else None,
        "catalog_name": catalogs[0] if len(catalogs) == 1 else None,
        "reference_series_id": int(analysis.reference_series_id),
        "min_points_per_object": int(analysis.min_points_per_object),
        "min_common_objects": int(analysis.min_common_objects),
        "clip_sigma": float(analysis.clip_sigma),
        "max_clip_iterations": int(analysis.max_clip_iterations),
        "sources": sources,
    }


def generation_fingerprint(snapshot: dict[str, Any]) -> str:
    canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def persist_interseries_calibration(
    session: Session,
    *,
    field_id: int | None = None,
    field_name: str | None = None,
    instrument_unit_id: int | None = None,
    filter_name: str | None = None,
    catalog_name: str | None = None,
    reference_series_id: int | None = None,
    min_points_per_object: int = 5,
    min_common_objects: int = 20,
    clip_sigma: float = 4.0,
    max_clip_iterations: int = 5,
    allow_sparse_graph: bool = False,
    required_stage2_recipe: str | None = None,
    science_calibration_version_ids: list[int] | None = None,
) -> PersistedInterseriesGeneration:
    if required_stage2_recipe is None:
        raise ValueError(
            "required_stage2_recipe is required for M4 persistence; "
            "currently supported recipe: 'stage3_scale_v1'"
        )
    if required_stage2_recipe != "stage3_scale_v1":
        raise ValueError(
            "Unsupported required_stage2_recipe for M4 persistence: "
            f"{required_stage2_recipe!r}; currently supported recipe: 'stage3_scale_v1'"
        )

    analysis = analyze_interseries_calibration(
        session,
        field_id=field_id,
        field_name=field_name,
        instrument_unit_id=instrument_unit_id,
        filter_name=filter_name,
        catalog_name=catalog_name,
        reference_series_id=reference_series_id,
        min_points_per_object=min_points_per_object,
        min_common_objects=min_common_objects,
        clip_sigma=clip_sigma,
        max_clip_iterations=max_clip_iterations,
        required_stage2_recipe=required_stage2_recipe,
        science_calibration_version_ids=science_calibration_version_ids,
    )
    if analysis.status != "READY_M1_M3" or analysis.graph_solution is None:
        raise ValueError(
            "Inter-series solution is not persistable: "
            f"analysis_status={analysis.status}. Persist only a connected M1-M3 solution."
        )
    graph = analysis.graph_solution
    if graph.status != "OK" or len(graph.offsets) != len(analysis.selected_series_ids):
        raise ValueError(
            "Inter-series graph is not persistable: "
            f"graph_status={graph.status}, offsets={len(graph.offsets)}, series={len(analysis.selected_series_ids)}"
        )

    reliability = classify_graph_reliability(
        n_series=graph.n_series,
        n_edges=graph.n_edges,
        dof=graph.dof,
        graph_status=graph.status,
    )
    if reliability == RELIABILITY_SPARSE and not allow_sparse_graph:
        raise ValueError(
            "Stage-3 graph has no closure redundancy (SPARSE_GRAPH_NO_CLOSURE). "
            "Re-run with --allow-sparse-graph only after reviewing the bridge-edge diagnostics."
        )
    if reliability not in {RELIABILITY_REDUNDANT, RELIABILITY_SPARSE}:
        raise ValueError(f"Unsupported graph reliability for persistence: {reliability}")

    snapshot = build_generation_input_snapshot(analysis)
    fingerprint = generation_fingerprint(snapshot)
    generation_uid = f"m4_{fingerprint}"
    instrument_units = {row.instrument_unit_id for row in analysis.series_summaries if row.instrument_unit_id is not None}
    filters = {row.filter_name for row in analysis.series_summaries if row.filter_name is not None}
    catalogs = {row.catalog_name for row in analysis.series_summaries if row.catalog_name is not None}
    if len(instrument_units) > 1 or len(filters) > 1 or len(catalogs) > 1:
        raise ValueError("Persisted Stage-3 component must have one instrument unit, filter and catalog namespace")
    resolved_unit = next(iter(instrument_units), None)
    resolved_filter = next(iter(filters), None)
    resolved_catalog = next(iter(catalogs), None)

    payload = analysis.to_dict(include_object_states=False, include_calibrators=False)
    insert_row = session.execute(
        text(
            """
            INSERT INTO interseries_calibration_generations (
                generation_uid, fingerprint_sha256, field_id, instrument_unit_id, filter_name, catalog_name,
                analysis_algorithm, sign_convention, reference_series_id,
                analysis_status, graph_status, reliability_status,
                n_series, n_edges, graph_dof, chi2, reduced_chi2, max_abs_standardized_residual,
                min_points_per_object, min_common_objects, clip_sigma, max_clip_iterations,
                input_snapshot, analysis_snapshot, lifecycle_status
            ) VALUES (
                :generation_uid, :fingerprint_sha256, :field_id, :instrument_unit_id, :filter_name, :catalog_name,
                :analysis_algorithm, :sign_convention, :reference_series_id,
                :analysis_status, :graph_status, :reliability_status,
                :n_series, :n_edges, :graph_dof, :chi2, :reduced_chi2, :max_abs_standardized_residual,
                :min_points_per_object, :min_common_objects, :clip_sigma, :max_clip_iterations,
                CAST(:input_snapshot AS jsonb), CAST(:analysis_snapshot AS jsonb), :lifecycle_status
            )
            ON CONFLICT (fingerprint_sha256) DO NOTHING
            RETURNING interseries_generation_id
            """
        ),
        {
            "generation_uid": generation_uid,
            "fingerprint_sha256": fingerprint,
            "field_id": int(analysis.field_id),
            "instrument_unit_id": resolved_unit,
            "filter_name": resolved_filter,
            "catalog_name": resolved_catalog,
            "analysis_algorithm": analysis.algorithm,
            "sign_convention": analysis.sign_convention,
            "reference_series_id": int(analysis.reference_series_id),
            "analysis_status": analysis.status,
            "graph_status": graph.status,
            "reliability_status": reliability,
            "n_series": int(graph.n_series),
            "n_edges": int(graph.n_edges),
            "graph_dof": int(graph.dof),
            "chi2": float(graph.chi2),
            "reduced_chi2": graph.reduced_chi2,
            "max_abs_standardized_residual": graph.max_abs_standardized_residual,
            "min_points_per_object": int(analysis.min_points_per_object),
            "min_common_objects": int(analysis.min_common_objects),
            "clip_sigma": float(analysis.clip_sigma),
            "max_clip_iterations": int(analysis.max_clip_iterations),
            "input_snapshot": json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
            "analysis_snapshot": json.dumps(payload, ensure_ascii=False, sort_keys=True),
            "lifecycle_status": LIFECYCLE_COMPLETE,
        },
    ).mappings().first()

    created = insert_row is not None
    if created:
        generation_id = int(insert_row["interseries_generation_id"])
        summaries = {row.series_id: row for row in analysis.series_summaries}
        graph_offsets = {row.series_id: row for row in graph.offsets}
        series_rows = []
        for series_id in sorted(analysis.selected_series_ids):
            summary = summaries[series_id]
            offset = graph_offsets[series_id]
            series_rows.append(
                {
                    "generation_id": generation_id,
                    "field_id": int(analysis.field_id),
                    "series_id": int(series_id),
                    "science_calibration_version_id": int(summary.calibration_version_id),
                    "offset_mag": float(offset.offset_mag),
                    "offset_err_mag": offset.offset_err_mag,
                    "is_reference_series": bool(offset.is_reference_series),
                    "n_incident_edges": int(offset.n_incident_edges),
                    "n_frames": int(summary.n_frames),
                    "n_science_points": int(summary.n_science_points),
                    "n_objects": int(summary.n_objects),
                    "n_state_objects": int(summary.n_state_objects),
                    "n_ensemble_references": int(summary.n_ensemble_references),
                    "n_reference_eligible": int(summary.n_reference_eligible),
                    "median_within_series_robust_sigma_mag": summary.median_within_series_robust_sigma_mag,
                    "details": json.dumps({"reference_source": summary.reference_source}, ensure_ascii=False),
                }
            )
        session.execute(
            text(
                """
                INSERT INTO interseries_calibration_series (
                    interseries_generation_id, field_id, series_id, science_calibration_version_id,
                    offset_mag, offset_err_mag, is_reference_series, n_incident_edges,
                    n_frames, n_science_points, n_objects, n_state_objects,
                    n_ensemble_references, n_reference_eligible, median_within_series_robust_sigma_mag, details
                ) VALUES (
                    :generation_id, :field_id, :series_id, :science_calibration_version_id,
                    :offset_mag, :offset_err_mag, :is_reference_series, :n_incident_edges,
                    :n_frames, :n_science_points, :n_objects, :n_state_objects,
                    :n_ensemble_references, :n_reference_eligible, :median_within_series_robust_sigma_mag,
                    CAST(:details AS jsonb)
                )
                """
            ),
            series_rows,
        )

        graph_diag = {(row.left_series_id, row.right_series_id): row for row in graph.edges}
        states_by_series: dict[int, set[int]] = {}
        for state in analysis.object_states:
            if state.reference_eligible:
                states_by_series.setdefault(state.series_id, set()).add(state.object_id)
        edge_rows = []
        for pair in analysis.pairwise_offsets:
            original_left = int(pair.left_series_id)
            original_right = int(pair.right_series_id)
            if original_left == original_right:
                raise ValueError(f"Inter-series edge cannot join a series to itself: {original_left}")

            diag = graph_diag.get((original_left, original_right))
            common_ids = sorted(
                states_by_series.get(original_left, set())
                & states_by_series.get(original_right, set())
            )
            used_ids = sorted(cal.object_id for cal in pair.calibrators if cal.used)
            rejected_ids = sorted(cal.object_id for cal in pair.calibrators if not cal.used)

            # The Stage-3 analysis intentionally preserves the planner's pair orientation,
            # while the immutable M4 schema stores every undirected edge canonically as
            # left_series_id < right_series_id.  Reversing an edge changes the sign of all
            # signed delta/residual terms but not their uncertainties, scatter, counts or
            # calibrator membership.
            reverse_for_storage = original_left > original_right
            storage_left = min(original_left, original_right)
            storage_right = max(original_left, original_right)
            sign = -1.0 if reverse_for_storage else 1.0

            measured_delta = None if pair.delta_mag is None else sign * float(pair.delta_mag)
            predicted_delta = None if diag is None else sign * float(diag.predicted_delta_mag)
            closure_residual = None if diag is None else sign * float(diag.closure_residual_mag)
            standardized_residual = None if diag is None else sign * float(diag.standardized_residual)

            edge_rows.append(
                {
                    "generation_id": generation_id,
                    "left_series_id": storage_left,
                    "right_series_id": storage_right,
                    "measured_delta_mag": measured_delta,
                    "measured_err_mag": pair.offset_err_mag,
                    "scatter_mag": pair.scatter_mag,
                    "n_initial_calibrators": int(pair.n_initial_calibrators),
                    "n_used_calibrators": int(pair.n_used_calibrators),
                    "n_rejected_calibrators": int(pair.n_rejected_calibrators),
                    "clip_iterations": int(pair.clip_iterations),
                    "pair_status": pair.status,
                    "predicted_delta_mag": predicted_delta,
                    "closure_residual_mag": closure_residual,
                    "standardized_residual": standardized_residual,
                    "common_reference_object_ids": json.dumps(common_ids),
                    "used_object_ids": json.dumps(used_ids),
                    "rejected_object_ids": json.dumps(rejected_ids),
                }
            )
        session.execute(
            text(
                """
                INSERT INTO interseries_calibration_edges (
                    interseries_generation_id, left_series_id, right_series_id,
                    measured_delta_mag, measured_err_mag, scatter_mag,
                    n_initial_calibrators, n_used_calibrators, n_rejected_calibrators,
                    clip_iterations, pair_status,
                    predicted_delta_mag, closure_residual_mag, standardized_residual,
                    common_reference_object_ids, used_object_ids, rejected_object_ids
                ) VALUES (
                    :generation_id, :left_series_id, :right_series_id,
                    :measured_delta_mag, :measured_err_mag, :scatter_mag,
                    :n_initial_calibrators, :n_used_calibrators, :n_rejected_calibrators,
                    :clip_iterations, :pair_status,
                    :predicted_delta_mag, :closure_residual_mag, :standardized_residual,
                    CAST(:common_reference_object_ids AS jsonb), CAST(:used_object_ids AS jsonb),
                    CAST(:rejected_object_ids AS jsonb)
                )
                """
            ),
            edge_rows,
        )
    else:
        existing = session.execute(
            text(
                """
                SELECT interseries_generation_id, input_snapshot
                FROM interseries_calibration_generations
                WHERE fingerprint_sha256 = :fingerprint_sha256
                """
            ),
            {"fingerprint_sha256": fingerprint},
        ).mappings().first()
        if existing is None:
            raise RuntimeError("Inter-series generation insert conflicted but existing row could not be loaded")
        stored_snapshot = existing["input_snapshot"]
        if isinstance(stored_snapshot, str):
            stored_snapshot = json.loads(stored_snapshot)
        if stored_snapshot != snapshot:
            raise RuntimeError("Inter-series fingerprint collision or corrupted input_snapshot")
        generation_id = int(existing["interseries_generation_id"])

    row = load_interseries_generation(session, generation_id=generation_id)
    return PersistedInterseriesGeneration(**row, created=created)


def load_interseries_generation(session: Session, *, generation_id: int) -> dict[str, Any]:
    row = session.execute(
        text(
            """
            SELECT
                g.interseries_generation_id, g.generation_uid, g.fingerprint_sha256,
                g.field_id, f.field_name, g.instrument_unit_id, g.filter_name, g.catalog_name,
                g.analysis_algorithm, g.reference_series_id, g.analysis_status, g.graph_status,
                g.reliability_status, g.n_series, g.n_edges, g.graph_dof, g.chi2,
                g.reduced_chi2, g.max_abs_standardized_residual,
                g.min_points_per_object, g.min_common_objects, g.clip_sigma, g.max_clip_iterations,
                g.lifecycle_status
            FROM interseries_calibration_generations g
            JOIN fields f ON f.field_id = g.field_id
            WHERE g.interseries_generation_id = :generation_id
            """
        ),
        {"generation_id": int(generation_id)},
    ).mappings().first()
    if row is None:
        raise ValueError(f"Inter-series generation not found: {generation_id}")
    return dict(row)


def list_interseries_generations(
    session: Session,
    *,
    generation_id: int | None = None,
    field_id: int | None = None,
    filter_name: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    where = []
    params: dict[str, Any] = {"limit": max(1, int(limit))}
    if generation_id is not None:
        where.append("g.interseries_generation_id = :generation_id")
        params["generation_id"] = int(generation_id)
    if field_id is not None:
        where.append("g.field_id = :field_id")
        params["field_id"] = int(field_id)
    if filter_name is not None:
        where.append("g.filter_name = :filter_name")
        params["filter_name"] = str(filter_name)
    predicate = "WHERE " + " AND ".join(where) if where else ""
    return [
        dict(row)
        for row in session.execute(
            text(
                f"""
                SELECT g.interseries_generation_id, g.generation_uid, g.field_id, f.field_name,
                       g.instrument_unit_id, g.filter_name, g.catalog_name,
                       g.reference_series_id, g.reliability_status, g.n_series, g.n_edges,
                       g.graph_dof, g.reduced_chi2, g.min_common_objects, g.lifecycle_status,
                       g.created_at
                FROM interseries_calibration_generations g
                JOIN fields f ON f.field_id = g.field_id
                {predicate}
                ORDER BY g.created_at DESC, g.interseries_generation_id DESC
                LIMIT :limit
                """
            ),
            params,
        ).mappings().all()
    ]


def load_interseries_generation_series(session: Session, *, generation_id: int) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in session.execute(
            text(
                """
                SELECT s.interseries_generation_id, s.field_id, s.series_id, s.science_calibration_version_id,
                       cv.name AS science_calibration_name,
                       s.offset_mag, s.offset_err_mag, s.is_reference_series, s.n_incident_edges,
                       s.n_frames, s.n_science_points, s.n_ensemble_references, s.n_reference_eligible,
                       s.median_within_series_robust_sigma_mag
                FROM interseries_calibration_series s
                JOIN calibration_versions cv ON cv.calibration_version_id = s.science_calibration_version_id
                WHERE s.interseries_generation_id = :generation_id
                ORDER BY s.series_id
                """
            ),
            {"generation_id": int(generation_id)},
        ).mappings().all()
    ]


def load_interseries_generation_edges(session: Session, *, generation_id: int) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in session.execute(
            text(
                """
                SELECT interseries_generation_id, left_series_id, right_series_id,
                       measured_delta_mag, measured_err_mag, scatter_mag,
                       n_initial_calibrators, n_used_calibrators, n_rejected_calibrators,
                       clip_iterations, pair_status, predicted_delta_mag,
                       closure_residual_mag, standardized_residual
                FROM interseries_calibration_edges
                WHERE interseries_generation_id = :generation_id
                ORDER BY left_series_id, right_series_id
                """
            ),
            {"generation_id": int(generation_id)},
        ).mappings().all()
    ]


def verify_interseries_generation(session: Session, *, generation_id: int, tolerance: float = 1.0e-10) -> InterseriesVerification:
    generation = session.execute(
        text(
            """
            SELECT g.*, f.field_name
            FROM interseries_calibration_generations g
            JOIN fields f ON f.field_id = g.field_id
            WHERE g.interseries_generation_id = :generation_id
            """
        ),
        {"generation_id": int(generation_id)},
    ).mappings().first()
    if generation is None:
        raise ValueError(f"Inter-series generation not found: {generation_id}")
    generation = dict(generation)
    snapshot = generation["input_snapshot"]
    if isinstance(snapshot, str):
        snapshot = json.loads(snapshot)
    fingerprint_ok = generation_fingerprint(snapshot) == generation["fingerprint_sha256"]

    series_rows = load_interseries_generation_series(session, generation_id=generation_id)
    orphan_rows = [row for row in series_rows if row["series_id"] is None]
    if orphan_rows:
        return InterseriesVerification(
            interseries_generation_id=int(generation_id),
            generation_uid=str(generation["generation_uid"]),
            status="FAILED",
            fingerprint_ok=fingerprint_ok,
            source_calibrations_ok=False,
            graph_ok=False,
            n_series=len(series_rows),
            n_ok_edges=0,
            graph_dof=0,
            reliability_status=str(generation["reliability_status"]),
            details={
                "failure_reason": "ORPHAN_M4_SERIES",
                "orphan_memberships": [
                    {
                        "field_id": row.get("field_id"),
                        "science_calibration_version_id": row["science_calibration_version_id"],
                    }
                    for row in orphan_rows
                ],
            },
        )
    source_checks = []
    source_ok = True
    for row in series_rows:
        source_cv_id = int(row["science_calibration_version_id"])
        check = session.execute(
            text(
                """
                SELECT cv.calibration_version_id, cv.series_id, cv.field_id, cv.product_role,
                       cv.lifecycle_status,
                       (SELECT COUNT(*) FROM science_photometry sp
                        WHERE sp.calibration_version_id = cv.calibration_version_id) AS n_science_rows
                FROM calibration_versions cv
                WHERE cv.calibration_version_id = :calibration_version_id
                """
            ),
            {"calibration_version_id": source_cv_id},
        ).mappings().first()
        ok = bool(
            check is not None
            and check["series_id"] is not None
            and check["field_id"] is not None
            and int(check["series_id"]) == int(row["series_id"])
            and int(check["field_id"]) == int(generation["field_id"])
            and row.get("field_id") is not None
            and int(row["field_id"]) == int(generation["field_id"])
            and check["product_role"] == "SCIENCE"
            and check["lifecycle_status"] == LIFECYCLE_COMPLETE
            and int(check["n_science_rows"] or 0) == int(row["n_science_points"])
            and int(check["n_science_rows"] or 0) > 0
        )
        source_ok = source_ok and ok
        source_checks.append({
            "series_id": int(row["series_id"]),
            "calibration_version_id": int(row["science_calibration_version_id"]),
            "ok": ok,
            "n_science_rows": 0 if check is None else int(check["n_science_rows"] or 0),
        })

    expected_sources = sorted(
        (int(row["series_id"]), int(row["calibration_version_id"]))
        for row in snapshot.get("sources", [])
    )
    stored_sources = sorted(
        (int(row["series_id"]), int(row["science_calibration_version_id"]))
        for row in series_rows
    )
    source_ok = source_ok and expected_sources == stored_sources
    reference_rows = [row for row in series_rows if bool(row["is_reference_series"])]
    source_ok = source_ok and len(reference_rows) == 1
    if reference_rows:
        source_ok = source_ok and int(reference_rows[0]["series_id"]) == int(generation["reference_series_id"])
        source_ok = source_ok and math.isclose(float(reference_rows[0]["offset_mag"]), 0.0, rel_tol=0.0, abs_tol=tolerance)

    edges = load_interseries_generation_edges(session, generation_id=generation_id)
    offsets = {int(row["series_id"]): float(row["offset_mag"]) for row in series_rows}
    ok_edges = [row for row in edges if row["pair_status"] == "OK"]
    graph_ok = len(series_rows) == int(generation["n_series"]) and len(ok_edges) == int(generation["n_edges"])
    chi2 = 0.0
    max_std = 0.0
    edge_checks = []
    for row in ok_edges:
        left = int(row["left_series_id"])
        right = int(row["right_series_id"])
        if left not in offsets or right not in offsets:
            graph_ok = False
            edge_checks.append({"left_series_id": left, "right_series_id": right, "ok": False})
            continue
        predicted = offsets[left] - offsets[right]
        measured = float(row["measured_delta_mag"])
        err = float(row["measured_err_mag"])
        residual = predicted - measured
        standardized = residual / err
        ok = (
            math.isclose(predicted, float(row["predicted_delta_mag"]), rel_tol=0.0, abs_tol=tolerance)
            and math.isclose(residual, float(row["closure_residual_mag"]), rel_tol=0.0, abs_tol=tolerance)
            and math.isclose(standardized, float(row["standardized_residual"]), rel_tol=0.0, abs_tol=tolerance)
        )
        graph_ok = graph_ok and ok
        chi2 += standardized * standardized
        max_std = max(max_std, abs(standardized))
        edge_checks.append({"left_series_id": left, "right_series_id": right, "ok": ok})

    dof = max(0, len(ok_edges) - (len(series_rows) - 1))
    reliability = classify_graph_reliability(
        n_series=len(series_rows), n_edges=len(ok_edges), dof=dof, graph_status="OK" if graph_ok else "INVALID"
    )
    graph_ok = graph_ok and dof == int(generation["graph_dof"])
    graph_ok = graph_ok and math.isclose(chi2, float(generation["chi2"]), rel_tol=0.0, abs_tol=max(tolerance, 1e-12))
    if generation["max_abs_standardized_residual"] is not None:
        graph_ok = graph_ok and math.isclose(
            max_std,
            float(generation["max_abs_standardized_residual"]),
            rel_tol=0.0,
            abs_tol=max(tolerance, 1e-12),
        )
    expected_reduced = (chi2 / dof) if dof > 0 else None
    if expected_reduced is None:
        graph_ok = graph_ok and generation["reduced_chi2"] is None
    else:
        graph_ok = graph_ok and generation["reduced_chi2"] is not None and math.isclose(
            expected_reduced, float(generation["reduced_chi2"]), rel_tol=0.0, abs_tol=max(tolerance, 1e-12)
        )
    graph_ok = graph_ok and reliability == generation["reliability_status"]

    status = "OK" if fingerprint_ok and source_ok and graph_ok else "FAILED"
    return InterseriesVerification(
        interseries_generation_id=int(generation_id),
        generation_uid=str(generation["generation_uid"]),
        status=status,
        fingerprint_ok=fingerprint_ok,
        source_calibrations_ok=source_ok,
        graph_ok=graph_ok,
        n_series=len(series_rows),
        n_ok_edges=len(ok_edges),
        graph_dof=dof,
        reliability_status=str(generation["reliability_status"]),
        details={"source_checks": source_checks, "edge_checks": edge_checks},
    )
