from __future__ import annotations

import json
import math
import time
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Iterable

from sqlalchemy import delete, func, inspect, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from grandgousier.calibration.reconstruction import (
    _RECONSTRUCTED_FIELDS,
    _chain_from_base,
    _latest_iteration,
    reconstruct_calibration_measurement_states,
)
from grandgousier.db.bulk import DEFAULT_BULK_INSERT_BATCH_SIZE, iter_sized_chunks, row_column_count
from grandgousier.db.models import (
    CalibrationVersion,
    Measurement,
    ScienceMeasurement,
    SciencePhotometry,
)
from grandgousier.photometry.science_photometry import SCIENCE_SCALAR_COLUMNS
from grandgousier.photometry.science_flags import (
    compact_science_flags_against_base,
    merge_effective_science_flags,
)


_SCIENCE_WRITE_COLUMNS = (
    "calibration_version_id",
    "source_measurement_id",
    "base_measurement_id",
    "object_id",
    "frame_id",
    *_RECONSTRUCTED_FIELDS,
    "flags",
    "materialization_source",
)


_SERVER_STATE_COLUMNS = (
    "base_measurement_id",
    "object_id",
    "frame_id",
    *_RECONSTRUCTED_FIELDS,
    "base_catalog_name",
    "base_random_error_physical_mag_err",
    "science_extra_flags",
)

_SUPPORTED_SERVER_STAGE_KINDS = {
    "APERTURE_CORRECTION",
    "APERTURE_ZP",
    "ENSEMBLE",
    "COLOR_CALIBRATION",
}


@dataclass(slots=True)
class _ServerReconstructionStage:
    calibration_version_id: int
    product_kind: str
    calibration_iteration_id: int
    parameters: dict[str, Any]
    metrics: dict[str, Any]


@dataclass(slots=True)
class _ServerReconstructionPlan:
    target_calibration_version_id: int
    target_calibration_name: str
    base_calibration_version_id: int
    stages: tuple[_ServerReconstructionStage, ...]
    frame_ids: tuple[int, ...]


def _state_projection(alias: str, overrides: dict[str, str] | None = None) -> str:
    overrides = overrides or {}
    return ",\n                ".join(
        f"{overrides.get(column, f'{alias}.{column}')} AS {column}"
        for column in _SERVER_STATE_COLUMNS
    )


def _json_float(json_expr: str, path: str) -> str:
    return f"NULLIF({json_expr} #>> '{{{path}}}', '')::double precision"


def _json_bool(json_expr: str, key: str, *, default: bool = False) -> str:
    default_sql = "TRUE" if default else "FALSE"
    return f"COALESCE(NULLIF({json_expr} ->> '{key}', '')::boolean, {default_sql})"


def _build_server_reconstruction_plan(
    session: Session,
    *,
    target_cv: CalibrationVersion,
    frame_ids: Iterable[int] | None,
) -> _ServerReconstructionPlan | None:
    """Resolve a compact standard chain that PostgreSQL can reconstruct directly.

    This fast path deliberately supports only the normalized science stages whose
    scalar semantics are covered by the reconstruction parity suite.  Any legacy,
    custom, or future stage automatically falls back to the Python reconstruction
    path instead of being guessed here.
    """

    chain = _chain_from_base(session, target_cv)
    if not chain or str(chain[0].product_kind or "").upper() != "BASE_PHOTOMETRY":
        return None
    stages: list[_ServerReconstructionStage] = []
    for cv in chain[1:]:
        kind = str(cv.product_kind or "").upper()
        if kind not in _SUPPORTED_SERVER_STAGE_KINDS:
            return None
        iteration = _latest_iteration(session, int(cv.calibration_version_id))
        if iteration is None:
            return None
        # The server fast path implements scalar semantics for the standard
        # product/iteration pairs only.  Derived ENSEMBLE products such as
        # PHOTOMETRIC_FRAME_QC and SPATIAL_ENSEMBLE intentionally keep
        # product_kind=ENSEMBLE for provenance, but their latest normalized
        # iteration does not contain ENSEMBLE_OFFSET terms.  Treating them as a
        # plain ENSEMBLE silently reconstructs zero rows.  The authoritative
        # Python reconstructor dispatches on iteration_kind first, so fall back
        # to it whenever the iteration kind is not one of the server-supported
        # standard stages.
        iteration_kind = str(getattr(iteration, "iteration_kind", "") or "").upper()
        if iteration_kind not in _SUPPORTED_SERVER_STAGE_KINDS:
            return None
        stages.append(
            _ServerReconstructionStage(
                calibration_version_id=int(cv.calibration_version_id),
                product_kind=kind,
                calibration_iteration_id=int(iteration.calibration_iteration_id),
                parameters=dict(iteration.parameters or {}),
                metrics=dict(iteration.metrics or {}),
            )
        )
    if not stages:
        return None
    return _ServerReconstructionPlan(
        target_calibration_version_id=int(target_cv.calibration_version_id),
        target_calibration_name=str(target_cv.name),
        base_calibration_version_id=int(chain[0].calibration_version_id),
        stages=tuple(stages),
        frame_ids=tuple(sorted({int(value) for value in (frame_ids or [])})),
    )


def _build_postgres_server_reconstruction_sql(
    plan: _ServerReconstructionPlan,
) -> tuple[str, dict[str, Any]]:
    """Build one INSERT..SELECT that reconstructs scalar science photometry in PostgreSQL.

    The query deliberately does not copy immutable BASE measurement flags into the
    retained science row.  ``base_measurement_id`` is the authoritative link to
    those per-epoch flags; ``science_measurements.flags`` stores only science-
    specific normalized summary data.  Readers reconstruct effective flags as
    BASE || SCIENCE, with science keys taking precedence.  This avoids duplicating
    a large detector/provenance JSONB payload for every final science row.

    BASE remains a MATERIALIZED CTE, but the normalized frame stages no longer
    materialize another ~50k-row state after every calibration step.  Each small
    frame-term table (normally tens of rows) is aggregated once into a JSONB map
    keyed by frame_id.  APERTURE/APERTURE_ZP/ENSEMBLE then decode one map entry
    per measurement through ``jsonb_to_record`` and stay NOT MATERIALIZED.  This
    keeps the anti-pathology boundary around the BASE scan while avoiding the
    repeated wide temp spills seen in the V5 execution plan.  COLOR_CALIBRATION
    uses the same pattern for its compact object details map.  No scientific
    term or scalar formula is changed; only the lookup/materialization strategy
    differs from V5.
    """

    params: dict[str, Any] = {
        "target_calibration_version_id": int(plan.target_calibration_version_id),
        "base_calibration_version_id": int(plan.base_calibration_version_id),
        "materialization_source": "POSTGRESQL_SERVER_RECONSTRUCTION_V7_MAP",
    }
    frame_filter = ""
    if plan.frame_ids:
        frame_list = ",".join(str(int(value)) for value in sorted(set(plan.frame_ids)))
        frame_filter = f" AND m.frame_id IN ({frame_list})"

    ctes: list[str] = [
        f"""
        s0 AS MATERIALIZED (
            SELECT
                m.measurement_id AS base_measurement_id,
                m.object_id,
                m.frame_id,
                {', '.join(f'm.{field}' for field in _RECONSTRUCTED_FIELDS)},
                m.flags ->> 'catalog_name' AS base_catalog_name,
                NULLIF(m.flags ->> 'random_error_physical_mag_err', '')::double precision
                    AS base_random_error_physical_mag_err,
                '{{}}'::jsonb AS science_extra_flags
            FROM measurements m
            WHERE m.calibration_version_id = :base_calibration_version_id{frame_filter}
        )
        """.strip()
    ]
    previous = "s0"
    floor_metrics: dict[str, Any] | None = None

    for index, stage in enumerate(plan.stages, start=1):
        name = f"s{index}"
        iteration_id = int(stage.calibration_iteration_id)
        kind = stage.product_kind
        if _positive_number(stage.metrics.get("residual_noise_floor_mag")) is not None:
            floor_metrics = dict(stage.metrics)

        if kind == "APERTURE_CORRECTION":
            width = _json_float("ft.details", "image_width")
            height = _json_float("ft.details", "image_height")
            pivot_x = f"COALESCE({_json_float('ft.details', 'pivot_xnorm')}, 0.0)"
            pivot_y = f"COALESCE({_json_float('ft.details', 'pivot_ynorm')}, 0.0)"
            c11 = f"COALESCE({_json_float('ft.details', 'covariance,1,1')}, 0.0)"
            c12 = f"COALESCE({_json_float('ft.details', 'covariance,1,2')}, 0.0)"
            c21 = f"COALESCE({_json_float('ft.details', 'covariance,2,1')}, 0.0)"
            c22 = f"COALESCE({_json_float('ft.details', 'covariance,2,2')}, 0.0)"
            accepted_xy = (
                "ft.calibration_frame_term_id IS NOT NULL AND ft.accepted "
                f"AND {previous}.x IS NOT NULL AND {previous}.y IS NOT NULL"
            )
            overrides = {
                "flux": (
                    f"CASE WHEN {previous}.flux IS NULL THEN NULL "
                    f"ELSE {previous}.flux * calc.scale END"
                ),
                "flux_err": (
                    f"CASE WHEN {previous}.flux_err IS NULL THEN NULL "
                    f"ELSE {previous}.flux_err * ABS(calc.scale) END"
                ),
                "mag_inst": (
                    f"CASE WHEN {previous}.mag_inst IS NULL THEN NULL "
                    f"ELSE {previous}.mag_inst + calc.correction END"
                ),
                "mag_cal": "NULL::double precision",
                "mag_err": (
                    f"SQRT(POWER(COALESCE({previous}.mag_err_random, {previous}.mag_err, 0.0), 2) "
                    "+ POWER(calc.differential_error, 2))"
                ),
                "mag_err_random": (
                    f"SQRT(POWER(COALESCE({previous}.mag_err_random, {previous}.mag_err, 0.0), 2) "
                    "+ POWER(calc.differential_error, 2))"
                ),
            }
            frame_map = f"ft{index}_map"
            ctes.append(
                f"""
                {frame_map} AS MATERIALIZED (
                    SELECT COALESCE(
                        jsonb_object_agg(
                            ft_src.frame_id::text,
                            jsonb_build_object(
                                'calibration_frame_term_id', ft_src.calibration_frame_term_id,
                                'accepted', ft_src.accepted,
                                'model_name', ft_src.model_name,
                                'coefficient_0', ft_src.coefficient_0,
                                'coefficient_x', ft_src.coefficient_x,
                                'coefficient_y', ft_src.coefficient_y,
                                'details', ft_src.details
                            )
                        ),
                        '{{}}'::jsonb
                    ) AS by_frame
                    FROM calibration_frame_terms ft_src
                    WHERE ft_src.calibration_iteration_id = {iteration_id}
                      AND ft_src.term_kind = 'APERTURE_CORRECTION'
                )
                """.strip()
            )
            ctes.append(
                f"""
                {name} AS NOT MATERIALIZED (
                    SELECT
                        {_state_projection(previous, overrides)}
                    FROM {previous}
                    CROSS JOIN {frame_map}
                    CROSS JOIN LATERAL jsonb_to_record(
                        COALESCE(
                            {frame_map}.by_frame -> ({previous}.frame_id::text),
                            '{{}}'::jsonb
                        )
                    ) AS ft(
                        calibration_frame_term_id bigint,
                        accepted boolean,
                        model_name text,
                        coefficient_0 double precision,
                        coefficient_x double precision,
                        coefficient_y double precision,
                        details jsonb
                    )
                    CROSS JOIN LATERAL (
                        SELECT
                            CASE WHEN {previous}.x IS NULL OR {width} IS NULL THEN NULL
                                 ELSE 2.0 * {previous}.x / GREATEST({width} - 1.0, 1.0) - 1.0 END AS xnorm,
                            CASE WHEN {previous}.y IS NULL OR {height} IS NULL THEN NULL
                                 ELSE 2.0 * {previous}.y / GREATEST({height} - 1.0, 1.0) - 1.0 END AS ynorm
                    ) norm
                    CROSS JOIN LATERAL (
                        SELECT
                            CASE WHEN {accepted_xy}
                                 THEN COALESCE(ft.coefficient_0, 0.0)
                                      + COALESCE(ft.coefficient_x, 0.0) * norm.xnorm
                                      + COALESCE(ft.coefficient_y, 0.0) * norm.ynorm
                                 ELSE 0.0 END::double precision AS correction,
                            CASE WHEN {accepted_xy} AND COALESCE(ft.model_name, 'identity') = 'plane'
                                 THEN SQRT(GREATEST(0.0,
                                      POWER(norm.xnorm - {pivot_x}, 2) * {c11}
                                      + (norm.xnorm - {pivot_x}) * (norm.ynorm - {pivot_y}) * ({c12} + {c21})
                                      + POWER(norm.ynorm - {pivot_y}, 2) * {c22}
                                 ))
                                 ELSE 0.0 END::double precision AS differential_error
                    ) err
                    CROSS JOIN LATERAL (
                        SELECT
                            err.correction,
                            err.differential_error,
                            POWER(10.0::double precision, -0.4 * err.correction) AS scale
                    ) calc
                )
                """.strip()
            )

        elif kind == "APERTURE_ZP":
            catalog_name = stage.parameters.get("catalog_name")
            catalog_filter = ""
            if catalog_name:
                key = f"catalog_name_{index}"
                params[key] = str(catalog_name)
                catalog_filter = f" AND {previous}.base_catalog_name = :{key}"
            random_expr = f"COALESCE({previous}.mag_err_random, {previous}.mag_err)"
            overrides = {
                "mag_cal": f"{previous}.mag_inst + ft.offset_mag",
                "mag_err": random_expr,
                "mag_err_random": random_expr,
                "local_zp_correction": "ft.offset_mag",
            }
            frame_map = f"ft{index}_map"
            ctes.append(
                f"""
                {frame_map} AS MATERIALIZED (
                    SELECT COALESCE(
                        jsonb_object_agg(
                            ft_src.frame_id::text,
                            jsonb_build_object(
                                'accepted', ft_src.accepted,
                                'offset_mag', ft_src.offset_mag
                            )
                        ),
                        '{{}}'::jsonb
                    ) AS by_frame
                    FROM calibration_frame_terms ft_src
                    WHERE ft_src.calibration_iteration_id = {iteration_id}
                      AND ft_src.term_kind = 'APERTURE_ZP'
                )
                """.strip()
            )
            ctes.append(
                f"""
                {name} AS NOT MATERIALIZED (
                    SELECT
                        {_state_projection(previous, overrides)}
                    FROM {previous}
                    CROSS JOIN {frame_map}
                    CROSS JOIN LATERAL jsonb_to_record(
                        COALESCE(
                            {frame_map}.by_frame -> ({previous}.frame_id::text),
                            '{{}}'::jsonb
                        )
                    ) AS ft(
                        accepted boolean,
                        offset_mag double precision
                    )
                    WHERE COALESCE(ft.accepted, FALSE)
                      AND ft.offset_mag IS NOT NULL
                      AND {previous}.mag_inst IS NOT NULL{catalog_filter}
                )
                """.strip()
            )

        elif kind == "ENSEMBLE":
            catalog_name = stage.parameters.get("catalog_name")
            catalog_filter = ""
            if catalog_name:
                key = f"catalog_name_{index}"
                params[key] = str(catalog_name)
                catalog_filter = f" AND {previous}.base_catalog_name = :{key}"
            prefer_mag_cal = bool(stage.parameters.get("prefer_mag_cal", True))
            value_expr = (
                f"COALESCE({previous}.mag_cal, {previous}.mag_inst)"
                if prefer_mag_cal
                else f"COALESCE({previous}.mag_inst, {previous}.mag_cal)"
            )
            random_expr = f"COALESCE({previous}.mag_err_random, {previous}.mag_err)"
            overrides = {
                "mag_cal": f"({value_expr}) - ft.offset_mag",
                "mag_err": random_expr,
                "mag_err_random": random_expr,
                "local_zp_correction": (
                    f"CASE WHEN {previous}.local_zp_correction IS NULL THEN -ft.offset_mag "
                    f"ELSE {previous}.local_zp_correction - ft.offset_mag END"
                ),
            }
            frame_map = f"ft{index}_map"
            ctes.append(
                f"""
                {frame_map} AS MATERIALIZED (
                    SELECT COALESCE(
                        jsonb_object_agg(
                            ft_src.frame_id::text,
                            jsonb_build_object(
                                'accepted', ft_src.accepted,
                                'offset_mag', ft_src.offset_mag
                            )
                        ),
                        '{{}}'::jsonb
                    ) AS by_frame
                    FROM calibration_frame_terms ft_src
                    WHERE ft_src.calibration_iteration_id = {iteration_id}
                      AND ft_src.term_kind = 'ENSEMBLE_OFFSET'
                )
                """.strip()
            )
            ctes.append(
                f"""
                {name} AS NOT MATERIALIZED (
                    SELECT
                        {_state_projection(previous, overrides)}
                    FROM {previous}
                    CROSS JOIN {frame_map}
                    CROSS JOIN LATERAL jsonb_to_record(
                        COALESCE(
                            {frame_map}.by_frame -> ({previous}.frame_id::text),
                            '{{}}'::jsonb
                        )
                    ) AS ft(
                        accepted boolean,
                        offset_mag double precision
                    )
                    WHERE COALESCE(ft.accepted, FALSE)
                      AND ft.offset_mag IS NOT NULL
                      AND ({value_expr}) IS NOT NULL{catalog_filter}
                )
                """.strip()
            )

        elif kind == "COLOR_CALIBRATION":
            required = ("transform_offset", "color_term", "color0")
            if any(_finite_number(stage.metrics.get(key)) is None for key in required):
                raise ValueError("Color normalized metrics are incomplete for server reconstruction")
            color0_key = f"color0_{index}"
            color_term_key = f"color_term_{index}"
            transform_key = f"transform_offset_{index}"
            params[color0_key] = float(stage.metrics["color0"])
            params[color_term_key] = float(stage.metrics["color_term"])
            params[transform_key] = float(stage.metrics["transform_offset"])
            identity_catalog = stage.parameters.get("identity_catalog_name")
            catalog_filter = ""
            if identity_catalog:
                key = f"identity_catalog_{index}"
                params[key] = str(identity_catalog)
                catalog_filter = f" AND {previous}.base_catalog_name = :{key}"
            reference_catalog_key = f"reference_catalog_{index}"
            reference_band_key = f"reference_band_{index}"
            color_index_key = f"color_index_{index}"
            params[reference_catalog_key] = stage.parameters.get("reference_catalog_name")
            params[reference_band_key] = stage.parameters.get("reference_band")
            params[color_index_key] = stage.parameters.get("color_index")
            params[f"fitted_color_term_{index}"] = _finite_number(stage.metrics.get("fitted_color_term"))
            params[f"fitted_color_term_err_{index}"] = _finite_number(stage.metrics.get("fitted_color_term_err"))
            params[f"transform_offset_err_{index}"] = _finite_number(stage.metrics.get("transform_offset_err"))
            params[f"color_term_err_{index}"] = _finite_number(stage.metrics.get("color_term_err"))
            params[f"transform_floor_{index}"] = _finite_number(stage.metrics.get("transform_residual_robust_sigma"))

            # Production EXPLAIN showed that even the V4 correlated LATERAL
            # lookup performed 50k+ probes of calibration_iteration_objects and
            # the planner preferred a weak iteration/boolean index.  Build one
            # compact object-id -> details map instead.  Color iterations contain
            # one row per object by schema contract, so jsonb_object_agg preserves
            # the same lookup semantics without a per-measurement table access.
            color_map = f"io{index}_map"
            ctes.append(
                f"""
                {color_map} AS MATERIALIZED (
                    SELECT COALESCE(
                        jsonb_object_agg(io_src.object_id::text, io_src.details),
                        '{{}}'::jsonb
                    ) AS by_object
                    FROM calibration_iteration_objects io_src
                    WHERE io_src.calibration_iteration_id = {iteration_id}
                )
                """.strip()
            )

            color_missing = "COALESCE(io.color_missing, FALSE)"
            color_outside = "COALESCE(io.color_outside_fit_range, FALSE)"
            applied_color = (
                f"COALESCE(io.applied_color_value, "
                f"CASE WHEN {color_missing} THEN :{color0_key} ELSE NULL END)"
            )
            correction = (
                f":{transform_key} + :{color_term_key} * (({applied_color}) - :{color0_key})"
            )
            random_expr = f"COALESCE({previous}.mag_err_random, {previous}.mag_err)"
            color_extra = f"""
                {previous}.science_extra_flags || jsonb_build_object(
                    'reference_catalog_name', CAST(:{reference_catalog_key} AS text),
                    'reference_band', CAST(:{reference_band_key} AS text),
                    'reference_color_index', CAST(:{color_index_key} AS text),
                    'reference_mag', io.reference_mag,
                    'reference_color_value', io.reference_color_value,
                    'reference_color_applied_value', ({applied_color}),
                    'reference_color_missing', {color_missing},
                    'reference_color_outside_fit_range', {color_outside},
                    'reference_catalog_source_id', io.reference_catalog_source_id,
                    'reference_color0', CAST(:{color0_key} AS double precision),
                    'reference_color_term', CAST(:{color_term_key} AS double precision),
                    'reference_fitted_color_term', CAST(:fitted_color_term_{index} AS double precision),
                    'reference_fitted_color_term_error', CAST(:fitted_color_term_err_{index} AS double precision),
                    'reference_transform_offset', CAST(:{transform_key} AS double precision),
                    'reference_transform_offset_error', CAST(:transform_offset_err_{index} AS double precision),
                    'reference_color_term_error', CAST(:color_term_err_{index} AS double precision),
                    'reference_color_error', io.color_error,
                    'reference_transform_systematic_floor', CAST(:transform_floor_{index} AS double precision),
                    'reference_color_correction', ({correction}),
                    'reference_color_calibration_applied', {previous}.mag_cal IS NOT NULL,
                    'reference_color_assumed_pivot', {color_missing}
                )
            """.strip()
            overrides = {
                "mag_cal": (
                    f"CASE WHEN {previous}.mag_cal IS NULL THEN NULL "
                    f"ELSE {previous}.mag_cal + ({correction}) END"
                ),
                "mag_err": random_expr,
                "mag_err_random": random_expr,
                "mag_err_systematic": "io.systematic_error",
                "science_extra_flags": color_extra,
            }
            ctes.append(
                f"""
                {name} AS NOT MATERIALIZED (
                    SELECT
                        {_state_projection(previous, overrides)}
                    FROM {previous}
                    CROSS JOIN {color_map}
                    CROSS JOIN LATERAL jsonb_to_record(
                        COALESCE(
                            {color_map}.by_object -> ({previous}.object_id::text),
                            '{{}}'::jsonb
                        )
                    ) AS io(
                        reference_mag double precision,
                        reference_color_value double precision,
                        applied_color_value double precision,
                        color_missing boolean,
                        color_error double precision,
                        systematic_error double precision,
                        reference_catalog_source_id text,
                        color_outside_fit_range boolean
                    )
                    WHERE ({applied_color}) IS NOT NULL{catalog_filter}
                )
                """.strip()
            )
        previous = name

    final_flags = (
        f"{previous}.science_extra_flags || "
        "jsonb_build_object("
        "'science_provenance_normalized', TRUE, "
        "'science_base_calibration_version_id', CAST(:base_calibration_version_id AS bigint), "
        "'science_calibration_version_id', CAST(:target_calibration_version_id AS bigint))"
    )
    if floor_metrics is not None:
        floor = _positive_number(floor_metrics.get("residual_noise_floor_mag"))
        if floor is not None:
            params["residual_floor"] = float(floor)
            params["residual_floor_algorithm"] = floor_metrics.get("residual_noise_floor_algorithm")
            params["residual_floor_basis"] = floor_metrics.get("residual_noise_floor_error_basis")
            params["residual_frame_common_sigma"] = _finite_number(
                floor_metrics.get("residual_noise_frame_common_sigma_mag")
            )
            params["residual_frame_common_supported"] = floor_metrics.get(
                "residual_noise_frame_common_supported"
            )
            params["residual_frame_common_status"] = floor_metrics.get(
                "residual_noise_frame_common_support_status"
            )
            params["residual_floor_after_common"] = _finite_number(
                floor_metrics.get("residual_noise_floor_after_frame_common_mag")
            )
            params["residual_independent_floor"] = _finite_number(
                floor_metrics.get("residual_noise_independent_floor_mag")
            )
            detector = (
                f"COALESCE({previous}.base_random_error_physical_mag_err, "
                f"{previous}.mag_err_random, {previous}.mag_err)"
            )
            total_random = f"COALESCE({previous}.mag_err_random, {previous}.mag_err, {detector})"
            final_flags += f" || jsonb_build_object(\n"
            final_flags += "                    'residual_noise_floor_algorithm', CAST(:residual_floor_algorithm AS text),\n"
            final_flags += "                    'residual_noise_floor_error_basis', CAST(:residual_floor_basis AS text),\n"
            final_flags += "                    'residual_noise_floor_applied', FALSE,\n"
            final_flags += f"                    'mag_err_detector', {detector},\n"
            final_flags += "                    'mag_err_epoch_floor', CAST(:residual_floor AS double precision),\n"
            final_flags += f"                    'mag_err_effective_detector_only', SQRT(POWER({detector}, 2) + POWER(:residual_floor, 2)),\n"
            final_flags += f"                    'mag_err_effective_total', SQRT(POWER({total_random}, 2) + POWER(:residual_floor, 2)),\n"
            final_flags += f"                    'mag_err_effective', SQRT(POWER({total_random}, 2) + POWER(:residual_floor, 2)),\n"
            final_flags += "                    'residual_noise_frame_common_sigma_mag', CAST(:residual_frame_common_sigma AS double precision),\n"
            final_flags += "                    'residual_noise_frame_common_supported', CAST(:residual_frame_common_supported AS boolean),\n"
            final_flags += "                    'residual_noise_frame_common_support_status', CAST(:residual_frame_common_status AS text),\n"
            final_flags += "                    'residual_noise_floor_after_frame_common_mag', CAST(:residual_floor_after_common AS double precision),\n"
            final_flags += "                    'residual_noise_independent_floor_mag', CAST(:residual_independent_floor AS double precision))"

    scalar_columns = ", ".join(f"{previous}.{column}" for column in _RECONSTRUCTED_FIELDS)
    insert_columns = ", ".join(f'"{column}"' for column in _SCIENCE_WRITE_COLUMNS)
    select_values = ", ".join(
        [
            ":target_calibration_version_id",
            "NULL::bigint",
            f"{previous}.base_measurement_id",
            f"{previous}.object_id",
            f"{previous}.frame_id",
            scalar_columns,
            final_flags,
            ":materialization_source",
        ]
    )
    joined_ctes = ',\n        '.join(ctes)
    sql = f"""
        WITH
        {joined_ctes}
        INSERT INTO science_measurements ({insert_columns})
        SELECT {select_values}
        FROM {previous}
    """.strip()
    return sql, params



def _server_reconstruction_select_sql(insert_sql: str) -> str:
    """Return the reconstruction SELECT behind the server INSERT statement.

    Diagnostic EXPLAIN runs this SELECT separately so PostgreSQL reconstruction
    cost can be distinguished from heap/index/FK/WAL insertion cost without
    transferring the reconstructed rows to Python.
    """

    marker = "INSERT INTO science_measurements"
    marker_pos = insert_sql.find(marker)
    if marker_pos < 0:
        raise ValueError("Server reconstruction SQL does not contain science INSERT marker")
    select_pos = insert_sql.find("SELECT ", marker_pos)
    if select_pos < 0:
        raise ValueError("Server reconstruction SQL does not contain INSERT SELECT body")
    return insert_sql[:marker_pos] + insert_sql[select_pos:]


def _normalize_explain_json(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        value = json.loads(value)
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return value
    raise TypeError(f"Unexpected EXPLAIN JSON payload type: {type(value).__name__}")


def _plan_node_summary(node: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "Node Type",
        "Operation",
        "Relation Name",
        "Alias",
        "CTE Name",
        "Index Name",
        "Index Cond",
        "Filter",
        "Rows Removed by Filter",
        "Join Type",
        "Actual Startup Time",
        "Actual Total Time",
        "Actual Rows",
        "Actual Loops",
        "Shared Hit Blocks",
        "Shared Read Blocks",
        "Shared Dirtied Blocks",
        "Shared Written Blocks",
        "Local Hit Blocks",
        "Local Read Blocks",
        "Local Dirtied Blocks",
        "Local Written Blocks",
        "Temp Read Blocks",
        "Temp Written Blocks",
        "WAL Records",
        "WAL FPI",
        "WAL Bytes",
    )
    out = {key: node[key] for key in keys if key in node}
    total = node.get("Actual Total Time")
    loops = node.get("Actual Loops", 1)
    try:
        out["wall_weight_ms"] = float(total) * float(loops)
    except (TypeError, ValueError):
        pass
    return out


def _summarize_explain_json(document: list[dict[str, Any]], *, top_n: int = 12) -> dict[str, Any]:
    if not document or not isinstance(document[0], dict):
        return {}
    root_doc = document[0]
    root = root_doc.get("Plan") or {}
    nodes: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        nodes.append(_plan_node_summary(node))
        for child in node.get("Plans") or []:
            walk(child)

    walk(root)
    nodes.sort(key=lambda row: float(row.get("wall_weight_ms", -1.0)), reverse=True)
    triggers = []
    for trigger in root_doc.get("Triggers") or []:
        if isinstance(trigger, dict):
            triggers.append(dict(trigger))
    return {
        "planning_time_ms": root_doc.get("Planning Time"),
        "execution_time_ms": root_doc.get("Execution Time"),
        "settings": root_doc.get("Settings"),
        "jit": root_doc.get("JIT"),
        "root": _plan_node_summary(root) if isinstance(root, dict) else {},
        "top_nodes": nodes[: max(1, int(top_n))],
        "triggers": triggers,
    }


def _explain_analyze_json(
    session: Session,
    *,
    sql: str,
    params: dict[str, Any],
) -> tuple[list[dict[str, Any]], float]:
    """Execute EXPLAIN ANALYZE with buffer/WAL instrumentation and return JSON."""

    explain_sql = "EXPLAIN (ANALYZE, BUFFERS, WAL, VERBOSE, SETTINGS, FORMAT JSON) " + sql
    started = time.perf_counter()
    payload = session.execute(text(explain_sql), params).scalar_one()
    elapsed = float(time.perf_counter() - started)
    return _normalize_explain_json(payload), elapsed


def _science_storage_diagnostics(
    session: Session,
    *,
    calibration_version_id: int,
) -> dict[str, Any]:
    started = time.perf_counter()
    row = session.execute(
        text(
            """
            SELECT
                COUNT(*)::bigint AS row_count,
                AVG(pg_column_size(sm))::double precision AS avg_row_bytes,
                MIN(pg_column_size(sm))::bigint AS min_row_bytes,
                MAX(pg_column_size(sm))::bigint AS max_row_bytes,
                AVG(pg_column_size(sm.flags))::double precision AS avg_flags_bytes,
                MIN(pg_column_size(sm.flags))::bigint AS min_flags_bytes,
                MAX(pg_column_size(sm.flags))::bigint AS max_flags_bytes
            FROM science_measurements sm
            WHERE sm.calibration_version_id = :calibration_version_id
            """
        ),
        {"calibration_version_id": int(calibration_version_id)},
    ).mappings().one()
    relation = session.execute(
        text(
            """
            SELECT
                pg_relation_size('science_measurements'::regclass)::bigint AS heap_bytes,
                pg_indexes_size('science_measurements'::regclass)::bigint AS indexes_bytes,
                pg_total_relation_size('science_measurements'::regclass)::bigint AS total_bytes
            """
        )
    ).mappings().one()
    return {
        "row_count": int(row["row_count"] or 0),
        "avg_row_bytes": float(row["avg_row_bytes"] or 0.0),
        "min_row_bytes": int(row["min_row_bytes"] or 0),
        "max_row_bytes": int(row["max_row_bytes"] or 0),
        "avg_flags_bytes": float(row["avg_flags_bytes"] or 0.0),
        "min_flags_bytes": int(row["min_flags_bytes"] or 0),
        "max_flags_bytes": int(row["max_flags_bytes"] or 0),
        "heap_bytes": int(relation["heap_bytes"] or 0),
        "indexes_bytes": int(relation["indexes_bytes"] or 0),
        "total_bytes": int(relation["total_bytes"] or 0),
        "scan_wall_sec": float(time.perf_counter() - started),
    }

def _finite_number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _positive_number(value: Any) -> float | None:
    parsed = _finite_number(value)
    return parsed if parsed is not None and parsed > 0 else None


def _scoped_base_measurement_count(
    session: Session,
    *,
    calibration_version_id: int,
    frame_ids: Iterable[int] | None,
) -> int:
    stmt = select(func.count(Measurement.measurement_id)).where(
        Measurement.calibration_version_id == int(calibration_version_id)
    )
    normalized_frame_ids = sorted({int(value) for value in (frame_ids or [])})
    if normalized_frame_ids:
        stmt = stmt.where(Measurement.frame_id.in_(normalized_frame_ids))
    return int(session.scalar(stmt) or 0)


def _raise_if_empty_science_reconstruction(
    session: Session,
    *,
    base_calibration_version_id: int,
    frame_ids: Iterable[int] | None,
    reconstructed_rows: int,
    calibration_name: str,
) -> None:
    if int(reconstructed_rows) > 0:
        return
    base_rows = _scoped_base_measurement_count(
        session,
        calibration_version_id=int(base_calibration_version_id),
        frame_ids=frame_ids,
    )
    if base_rows <= 0:
        return
    raise ValueError(
        "Science materialization reconstructed zero rows from a non-empty BASE scope: "
        f"calibration={calibration_name!r}, base_rows={base_rows}. "
        "Refusing to report an empty SCIENCE materialization as OK."
    )


def _materialize_science_via_postgres_server_reconstruction(
    session: Session,
    *,
    plan: _ServerReconstructionPlan,
    diagnostics: bool = False,
) -> ScienceMaterializationResult:
    """Materialize final scalar science rows entirely inside PostgreSQL."""

    total_started = time.perf_counter()
    result = ScienceMaterializationResult(
        calibration_version_id=int(plan.target_calibration_version_id),
        calibration_name=str(plan.target_calibration_name),
        base_calibration_version_id=int(plan.base_calibration_version_id),
        write_strategy="POSTGRESQL_SERVER_INSERT_SELECT",
        reconstruction_strategy="POSTGRESQL_CTE_FRAME_MAP_COLOR_MAP_INLINE_V7",
        science_flags_strategy="COMPACT_NORMALIZED_SUMMARY_V2",
    )

    finalize_started = time.perf_counter()
    result.deleted_stale_rows = int(
        session.execute(
            delete(ScienceMeasurement).where(
                ScienceMeasurement.calibration_version_id
                == int(plan.target_calibration_version_id)
            )
        ).rowcount
        or 0
    )
    session.flush()
    result.finalize_sec += float(time.perf_counter() - finalize_started)

    sql, params = _build_postgres_server_reconstruction_sql(plan)
    inserted: int
    if diagnostics:
        result.diagnostics_enabled = True
        diagnostics_started = time.perf_counter()
        # Profile the INSERT first so the measurement sees the same cache/WAL
        # state as an ordinary materialization.  Running the SELECT diagnostic
        # first would warm reconstruction inputs and bias the insertion timing.
        insert_explain, insert_wall_sec = _explain_analyze_json(
            session, sql=sql, params=params
        )
        result.insert_select_sec = float(insert_wall_sec)
        select_sql = _server_reconstruction_select_sql(sql)
        select_explain, select_wall_sec = _explain_analyze_json(
            session, sql=select_sql, params=params
        )
        inserted = int(
            session.scalar(
                select(func.count(ScienceMeasurement.science_measurement_id)).where(
                    ScienceMeasurement.calibration_version_id
                    == int(plan.target_calibration_version_id)
                )
            )
            or 0
        )
        storage = _science_storage_diagnostics(
            session, calibration_version_id=int(plan.target_calibration_version_id)
        )
        result.diagnostics = {
            "mode": "EXPLAIN_ANALYZE_BUFFERS_WAL_V1",
            "select_without_insert_wall_sec": float(select_wall_sec),
            "insert_explain_wall_sec": float(insert_wall_sec),
            "select_summary": _summarize_explain_json(select_explain),
            "insert_summary": _summarize_explain_json(insert_explain),
            "storage": storage,
            "select_explain": select_explain,
            "insert_explain": insert_explain,
        }
        result.diagnostics_sec = float(time.perf_counter() - diagnostics_started)
    else:
        insert_started = time.perf_counter()
        execution = session.execute(text(sql), params)
        result.insert_select_sec = float(time.perf_counter() - insert_started)
        inserted = int(execution.rowcount or 0)
        if inserted < 0:
            inserted = int(
                session.scalar(
                    select(func.count(ScienceMeasurement.science_measurement_id)).where(
                        ScienceMeasurement.calibration_version_id
                        == int(plan.target_calibration_version_id)
                    )
                )
                or 0
            )
    result.reconstructed_rows = inserted
    result.inserted_or_updated = inserted
    result.insert_batches = 1 if inserted else 0
    _raise_if_empty_science_reconstruction(
        session,
        base_calibration_version_id=int(plan.base_calibration_version_id),
        frame_ids=plan.frame_ids,
        reconstructed_rows=inserted,
        calibration_name=str(plan.target_calibration_name),
    )

    finalize_started = time.perf_counter()
    session.flush()
    result.finalize_sec += float(time.perf_counter() - finalize_started)
    result.status = "OK"
    result.total_sec = float(time.perf_counter() - total_started)
    return result


@dataclass(slots=True)
class ScienceMaterializationResult:
    calibration_version_id: int
    calibration_name: str
    base_calibration_version_id: int
    reconstructed_rows: int = 0
    legacy_rows: int = 0
    inserted_or_updated: int = 0
    insert_batches: int = 0
    deleted_stale_rows: int = 0
    shadow_checked: bool = False
    shadow_comparison_source: str = "NONE"
    shadow_baseline_rows: int = 0
    missing_legacy_rows: int = 0
    extra_legacy_rows: int = 0
    scalar_mismatches: int = 0
    max_abs_delta: dict[str, float] = field(default_factory=dict)
    write_strategy: str = "PENDING"
    reconstruction_strategy: str = "PYTHON_STATE_RECONSTRUCTION"
    science_flags_strategy: str = "ACCUMULATED_RECONSTRUCTED_FLAGS"
    science_flags_source: str = "RECONSTRUCTED_EFFECTIVE_FLAGS"
    reconstruction_sec: float = 0.0
    shadow_compare_sec: float = 0.0
    staging_copy_sec: float = 0.0
    insert_select_sec: float = 0.0
    finalize_sec: float = 0.0
    diagnostics_enabled: bool = False
    diagnostics_sec: float = 0.0
    diagnostics: dict[str, Any] = field(default_factory=dict)
    total_sec: float = 0.0
    status: str = "PENDING"

    @property
    def mismatches(self) -> int:
        return int(self.missing_legacy_rows + self.extra_legacy_rows + self.scalar_mismatches)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "calibration_version_id": self.calibration_version_id,
            "calibration_name": self.calibration_name,
            "base_calibration_version_id": self.base_calibration_version_id,
            "reconstructed_rows": self.reconstructed_rows,
            "legacy_rows": self.legacy_rows,
            "inserted_or_updated": self.inserted_or_updated,
            "insert_batches": self.insert_batches,
            "deleted_stale_rows": self.deleted_stale_rows,
            "shadow_checked": self.shadow_checked,
            "shadow_comparison_source": self.shadow_comparison_source,
            "shadow_baseline_rows": self.shadow_baseline_rows,
            "missing_legacy_rows": self.missing_legacy_rows,
            "extra_legacy_rows": self.extra_legacy_rows,
            "scalar_mismatches": self.scalar_mismatches,
            "mismatches": self.mismatches,
            "max_abs_delta": dict(self.max_abs_delta),
            "write_strategy": self.write_strategy,
            "reconstruction_strategy": self.reconstruction_strategy,
            "science_flags_strategy": self.science_flags_strategy,
            "science_flags_source": self.science_flags_source,
            "reconstruction_sec": self.reconstruction_sec,
            "shadow_compare_sec": self.shadow_compare_sec,
            "staging_copy_sec": self.staging_copy_sec,
            "insert_select_sec": self.insert_select_sec,
            "finalize_sec": self.finalize_sec,
            "diagnostics_enabled": self.diagnostics_enabled,
            "diagnostics_sec": self.diagnostics_sec,
            "diagnostics": dict(self.diagnostics),
            "total_sec": self.total_sec,
        }


def materialize_science_calibration(
    session: Session,
    *,
    calibration_name: str | None = None,
    calibration_version_id: int | None = None,
    frame_ids: Iterable[int] | None = None,
    shadow_compare_legacy: bool = True,
    tolerance: float = 1e-10,
    insert_batch_size: int = DEFAULT_BULK_INSERT_BATCH_SIZE,
    diagnostics: bool = False,
) -> ScienceMaterializationResult:
    """Write final science photometry from base rows + normalized terms.

    PostgreSQL uses a streaming COPY into an unindexed temporary staging table,
    followed by one server-side INSERT .. SELECT into ``science_measurements``.
    This avoids expanding ~50k rows into hundreds of thousands of DBAPI bind
    parameters.  Non-PostgreSQL/test backends retain the previous safe batched
    INSERT .. ON CONFLICT fallback.

    In compatibility mode, a legacy final ``measurements`` layer can still be
    shadow-compared before the science product is written.
    """

    total_started = time.perf_counter()

    # D4d fast path: once intermediate layers are normalized-only, PostgreSQL
    # can reconstruct the retained final scalar science product directly from
    # immutable BASE rows + compact terms.  Compatibility/shadow runs keep the
    # Python reconstruction because they intentionally compare accumulated legacy
    # row state before writing.
    if not shadow_compare_legacy and _postgres_server_reconstruction_capable(session):
        target_cv = _resolve_cv(
            session,
            calibration_name=calibration_name,
            calibration_version_id=calibration_version_id,
        )
        plan = _build_server_reconstruction_plan(
            session,
            target_cv=target_cv,
            frame_ids=frame_ids,
        )
        if plan is not None:
            return _materialize_science_via_postgres_server_reconstruction(
                session, plan=plan, diagnostics=bool(diagnostics)
            )

    reconstruction_started = time.perf_counter()
    reconstructed = reconstruct_calibration_measurement_states(
        session,
        calibration_name=calibration_name,
        calibration_version_id=calibration_version_id,
        frame_ids=frame_ids,
    )
    reconstruction_sec = float(time.perf_counter() - reconstruction_started)

    target_cv = session.get(CalibrationVersion, reconstructed.target_calibration_version_id)
    if target_cv is None:
        raise ValueError("Target calibration version not found after reconstruction")
    result = ScienceMaterializationResult(
        calibration_version_id=reconstructed.target_calibration_version_id,
        calibration_name=reconstructed.target_calibration_name,
        base_calibration_version_id=reconstructed.base_calibration_version_id,
        reconstructed_rows=len(reconstructed.states),
        reconstruction_sec=reconstruction_sec,
        science_flags_strategy="BASE_DELTA_EFFECTIVE_FLAGS_V1",
    )
    _raise_if_empty_science_reconstruction(
        session,
        base_calibration_version_id=int(reconstructed.base_calibration_version_id),
        frame_ids=frame_ids,
        reconstructed_rows=len(reconstructed.states),
        calibration_name=str(reconstructed.target_calibration_name),
    )

    shadow_started = time.perf_counter()
    legacy = _load_legacy_measurements(
        session,
        calibration_version_id=reconstructed.target_calibration_version_id,
        frame_ids=frame_ids,
    )
    result.legacy_rows = len(legacy)
    if shadow_compare_legacy:
        result.shadow_checked = True
        shadow_baseline, shadow_source = _resolve_shadow_comparison_baseline(
            session,
            calibration_version_id=reconstructed.target_calibration_version_id,
            frame_ids=frame_ids,
            legacy=legacy,
        )
        result.shadow_comparison_source = shadow_source
        result.shadow_baseline_rows = len(shadow_baseline)
        if not shadow_baseline and reconstructed.states:
            result.shadow_compare_sec = float(time.perf_counter() - shadow_started)
            result.total_sec = float(time.perf_counter() - total_started)
            result.status = "SHADOW_BASELINE_MISSING"
            raise ValueError(
                "Refusing shadow-checked science materialization because neither "
                "target legacy measurements nor an existing science materialization "
                "is available as a comparison baseline"
            )
        _apply_shadow_comparison(
            result, reconstructed.states, shadow_baseline, tolerance=float(tolerance)
        )
        if not result.mismatches:
            _attach_shadow_effective_flags(
                reconstructed.states,
                shadow_baseline,
                shadow_source=shadow_source,
            )
            result.science_flags_source = "SHADOW_BASELINE_EFFECTIVE_FLAGS"
        if result.mismatches:
            result.shadow_compare_sec = float(time.perf_counter() - shadow_started)
            result.total_sec = float(time.perf_counter() - total_started)
            result.status = "SHADOW_MISMATCH"
            raise ValueError(
                "Refusing science materialization because reconstructed photometry "
                f"does not match {result.shadow_comparison_source.lower()}: "
                f"mismatches={result.mismatches}"
            )
    result.shadow_compare_sec = float(time.perf_counter() - shadow_started)

    ordered_keys = sorted(reconstructed.states)
    materialization_source = (
        "RECONSTRUCTED_NORMALIZED_SHADOW" if shadow_compare_legacy else "RECONSTRUCTED_NORMALIZED"
    )

    finalize_started = time.perf_counter()
    result.deleted_stale_rows = int(
        session.execute(
            delete(ScienceMeasurement).where(
                ScienceMeasurement.calibration_version_id == reconstructed.target_calibration_version_id
            )
        ).rowcount
        or 0
    )
    session.flush()
    result.finalize_sec += float(time.perf_counter() - finalize_started)

    if ordered_keys:
        if _postgres_copy_capable(session):
            result.write_strategy = "POSTGRESQL_COPY_STAGING"
            copy_sec, insert_sec, cleanup_sec = _write_science_rows_via_postgres_copy(
                session,
                calibration_version_id=reconstructed.target_calibration_version_id,
                ordered_keys=ordered_keys,
                states=reconstructed.states,
                legacy=legacy,
                materialization_source=materialization_source,
            )
            result.staging_copy_sec = copy_sec
            result.insert_select_sec = insert_sec
            result.finalize_sec += cleanup_sec
            result.insert_batches = 1
        else:
            result.write_strategy = "SQLALCHEMY_BATCH_UPSERT"
            insert_started = time.perf_counter()
            rows = [
                _science_row_dict(
                    calibration_version_id=reconstructed.target_calibration_version_id,
                    state=reconstructed.states[key],
                    legacy_measurement=legacy.get(key),
                    materialization_source=materialization_source,
                )
                for key in ordered_keys
            ]
            table = ScienceMeasurement.__table__
            for chunk in iter_sized_chunks(
                rows,
                n_columns=row_column_count(rows),
                requested=int(insert_batch_size),
            ):
                stmt = insert(table).values(list(chunk))
                stmt = stmt.on_conflict_do_update(
                    constraint="uq_science_measurements_version_object_frame",
                    set_={
                        "source_measurement_id": stmt.excluded.source_measurement_id,
                        "base_measurement_id": stmt.excluded.base_measurement_id,
                        "x": stmt.excluded.x,
                        "y": stmt.excluded.y,
                        "flux": stmt.excluded.flux,
                        "flux_err": stmt.excluded.flux_err,
                        "mag_inst": stmt.excluded.mag_inst,
                        "mag_cal": stmt.excluded.mag_cal,
                        "mag_err": stmt.excluded.mag_err,
                        "mag_err_random": stmt.excluded.mag_err_random,
                        "mag_err_systematic": stmt.excluded.mag_err_systematic,
                        "aperture_radius_pix": stmt.excluded.aperture_radius_pix,
                        "local_background": stmt.excluded.local_background,
                        "local_zp_correction": stmt.excluded.local_zp_correction,
                        "centroid_dx_pix": stmt.excluded.centroid_dx_pix,
                        "centroid_dy_pix": stmt.excluded.centroid_dy_pix,
                        "flags": stmt.excluded.flags,
                        "materialization_source": stmt.excluded.materialization_source,
                        "updated_at": func.now(),
                    },
                )
                session.execute(stmt)
                result.insert_batches += 1
            result.insert_select_sec = float(time.perf_counter() - insert_started)
        result.inserted_or_updated = len(ordered_keys)
    else:
        result.write_strategy = "NO_ROWS"

    finalize_started = time.perf_counter()
    session.flush()
    result.finalize_sec += float(time.perf_counter() - finalize_started)
    result.status = "OK"
    result.total_sec = float(time.perf_counter() - total_started)
    return result


def _postgres_server_reconstruction_capable(session: Session) -> bool:
    try:
        return str(session.connection().dialect.name).lower() == "postgresql"
    except Exception:  # noqa: BLE001 - compatibility sessions use the Python fallback.
        return False


def _postgres_copy_capable(session: Session) -> bool:
    """Return true only for psycopg3-style PostgreSQL DBAPI connections."""

    try:
        sa_connection = session.connection()
        if str(sa_connection.dialect.name).lower() != "postgresql":
            return False
        driver_connection = _driver_connection(sa_connection)
        cursor = driver_connection.cursor()
        try:
            return callable(getattr(cursor, "copy", None))
        finally:
            cursor.close()
    except Exception:  # noqa: BLE001 - capability detection must preserve fallback behavior.
        return False


def _driver_connection(sa_connection: Any) -> Any:
    proxied = sa_connection.connection
    return getattr(proxied, "driver_connection", proxied)


def _write_science_rows_via_postgres_copy(
    session: Session,
    *,
    calibration_version_id: int,
    ordered_keys: list[tuple[int, int]],
    states: dict[tuple[int, int], dict[str, Any]],
    legacy: dict[tuple[int, int], Any],
    materialization_source: str,
) -> tuple[float, float, float]:
    """Stream rows to a temp table and server-side insert them into science data."""

    sa_connection = session.connection()
    driver_connection = _driver_connection(sa_connection)
    stage_name = f"gg_science_stage_{uuid.uuid4().hex}"
    quoted_stage = f'"{stage_name}"'
    columns_sql = ", ".join(f'"{column}"' for column in _SCIENCE_WRITE_COLUMNS)

    cursor = driver_connection.cursor()
    try:
        copy_started = time.perf_counter()
        cursor.execute(
            f"CREATE TEMP TABLE {quoted_stage} ON COMMIT DROP AS "
            f"SELECT {columns_sql} FROM science_measurements WITH NO DATA"
        )
        with cursor.copy(
            f"COPY {quoted_stage} ({columns_sql}) FROM STDIN"
        ) as copy:
            for key in ordered_keys:
                copy.write_row(
                    _science_copy_tuple(
                        calibration_version_id=calibration_version_id,
                        state=states[key],
                        legacy_measurement=legacy.get(key),
                        materialization_source=materialization_source,
                    )
                )
        staging_copy_sec = float(time.perf_counter() - copy_started)

        insert_started = time.perf_counter()
        cursor.execute(
            f"INSERT INTO science_measurements ({columns_sql}) "
            f"SELECT {columns_sql} FROM {quoted_stage}"
        )
        insert_select_sec = float(time.perf_counter() - insert_started)

        # The table is ON COMMIT DROP, but explicit cleanup keeps long-running
        # batch transactions from accumulating temporary relations.
        cleanup_started = time.perf_counter()
        cursor.execute(f"DROP TABLE {quoted_stage}")
        cleanup_sec = float(time.perf_counter() - cleanup_started)
    finally:
        cursor.close()

    return staging_copy_sec, insert_select_sec, cleanup_sec


def _science_row_dict(
    *,
    calibration_version_id: int,
    state: dict[str, Any],
    legacy_measurement: Any | None,
    materialization_source: str,
) -> dict[str, Any]:
    # Keep the Python compatibility path storage-equivalent to the compact
    # science contract: immutable BASE flags stay on ``measurements`` and only
    # the overlay needed to reconstruct the effective public view is retained
    # here.  ``legacy_measurement`` is still used for source_measurement_id, but
    # its accumulated flags must not be copied into the final science row.
    preserve_shadow_effective = "_shadow_effective_flags" in state
    effective_flags = (
        state.get("_shadow_effective_flags")
        if preserve_shadow_effective
        else state.get("flags")
    )
    flags = compact_science_flags_against_base(
        state.get("_base_flags"),
        effective_flags,
    )
    # New/non-shadow materializations retain explicit normalized science provenance.
    # Historical shadow compaction must preserve the pre-existing public effective
    # flag mapping exactly, so it must not synthesize keys that were absent from
    # the shadow baseline.  materialization_source records the storage rewrite.
    if not preserve_shadow_effective:
        flags.update(
            {
                "science_provenance_normalized": True,
                "science_calibration_version_id": int(calibration_version_id),
            }
        )
        base_calibration_version_id = state.get("_base_calibration_version_id")
        if base_calibration_version_id is not None:
            flags["science_base_calibration_version_id"] = int(base_calibration_version_id)
    return {
        "calibration_version_id": int(calibration_version_id),
        "source_measurement_id": (
            int(legacy_measurement.measurement_id) if legacy_measurement is not None else None
        ),
        "base_measurement_id": state.get("base_measurement_id"),
        "object_id": int(state["object_id"]),
        "frame_id": int(state["frame_id"]),
        **{field_name: state.get(field_name) for field_name in _RECONSTRUCTED_FIELDS},
        "flags": flags,
        "materialization_source": materialization_source,
    }


def _science_copy_tuple(
    *,
    calibration_version_id: int,
    state: dict[str, Any],
    legacy_measurement: Any | None,
    materialization_source: str,
) -> tuple[Any, ...]:
    """Return one COPY row in exactly ``_SCIENCE_WRITE_COLUMNS`` order."""

    row = _science_row_dict(
        calibration_version_id=calibration_version_id,
        state=state,
        legacy_measurement=legacy_measurement,
        materialization_source=materialization_source,
    )
    # COPY's PostgreSQL JSONB input function accepts JSON text.  Serializing
    # explicitly avoids depending on driver-specific dict adapters.
    row["flags"] = json.dumps(
        row["flags"], ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    return tuple(row[column] for column in _SCIENCE_WRITE_COLUMNS)


def science_materialization_parity_report(
    session: Session,
    *,
    calibration_name: str | None = None,
    calibration_version_id: int | None = None,
    tolerance: float = 1e-10,
) -> dict[str, Any]:
    """Verify final SCIENCE storage against normalized reconstruction.

    Clean 0.7 schemas intentionally do not contain ``science_measurements``.
    Their durable final-state layer is the narrow ``science_photometry`` table,
    while immutable identity/extraction geometry remains in BASE ``measurements``.
    The parity gate therefore reconstructs the target product from BASE +
    normalized terms and compares that public scalar state with
    ``science_photometry`` joined back to its BASE row.

    The historical wide-table implementation is retained only as a fallback for
    frozen pre-0.7 schemas that do not have ``science_photometry``.
    """

    cv = _resolve_cv(
        session,
        calibration_name=calibration_name,
        calibration_version_id=calibration_version_id,
    )

    if _has_table(session, "science_photometry"):
        science_rows = _load_clean_science_photometry_states(
            session, calibration_version_id=int(cv.calibration_version_id)
        )
        reconstructed = reconstruct_calibration_measurement_states(
            session,
            calibration_version_id=int(cv.calibration_version_id),
        )
        result = ScienceMaterializationResult(
            calibration_version_id=int(cv.calibration_version_id),
            calibration_name=str(cv.name),
            base_calibration_version_id=int(reconstructed.base_calibration_version_id),
            reconstructed_rows=len(reconstructed.states),
            legacy_rows=0,
            shadow_checked=True,
            shadow_comparison_source="SCIENCE_PHOTOMETRY",
            shadow_baseline_rows=len(science_rows),
        )
        _apply_shadow_comparison(
            result,
            reconstructed.states,
            science_rows,
            tolerance=float(tolerance),
        )
        result.status = (
            "OK"
            if result.mismatches == 0 and science_rows
            else ("EMPTY" if not science_rows else "MISMATCH")
        )
        payload = result.to_dict()
        payload["science_photometry"] = len(science_rows)
        # Backward-compatible output key for existing CLI/report consumers.  It
        # denotes the durable science row count, not a physical legacy table.
        payload["science_measurements"] = len(science_rows)
        payload["science_storage_table"] = "science_photometry"
        payload["comparison_source"] = "SCIENCE_PHOTOMETRY_VS_RECONSTRUCTED_NORMALIZED"
        payload["missing_rows"] = result.missing_legacy_rows
        payload["extra_rows"] = result.extra_legacy_rows
        return payload

    # Frozen pre-0.7 compatibility path.
    science_rows = {
        (int(row.object_id), int(row.frame_id)): row
        for row in session.scalars(
            select(ScienceMeasurement)
            .where(
                ScienceMeasurement.calibration_version_id
                == int(cv.calibration_version_id)
            )
            .order_by(ScienceMeasurement.object_id, ScienceMeasurement.frame_id)
        ).all()
    }
    legacy = _load_legacy_measurements(
        session, calibration_version_id=int(cv.calibration_version_id)
    )
    result = ScienceMaterializationResult(
        calibration_version_id=int(cv.calibration_version_id),
        calibration_name=str(cv.name),
        base_calibration_version_id=0,
        reconstructed_rows=len(science_rows),
        legacy_rows=len(legacy),
        shadow_checked=True,
    )

    if legacy:
        result.shadow_comparison_source = "LEGACY_MEASUREMENTS"
        result.shadow_baseline_rows = len(legacy)
        left = {
            key: {
                field_name: getattr(row, field_name)
                for field_name in _RECONSTRUCTED_FIELDS
            }
            for key, row in science_rows.items()
        }
        _apply_shadow_comparison(result, left, legacy, tolerance=float(tolerance))
        comparison_source = "LEGACY_MEASUREMENTS"
    else:
        result.shadow_comparison_source = "EXISTING_SCIENCE_MEASUREMENTS"
        result.shadow_baseline_rows = len(science_rows)
        reconstructed = reconstruct_calibration_measurement_states(
            session,
            calibration_version_id=int(cv.calibration_version_id),
        )
        result.base_calibration_version_id = int(
            reconstructed.base_calibration_version_id
        )
        result.reconstructed_rows = len(reconstructed.states)
        _apply_shadow_comparison(
            result,
            reconstructed.states,
            science_rows,
            tolerance=float(tolerance),
        )
        comparison_source = "RECONSTRUCTED_NORMALIZED"

    result.status = (
        "OK"
        if result.mismatches == 0 and science_rows
        else ("EMPTY" if not science_rows else "MISMATCH")
    )
    payload = result.to_dict()
    payload["science_measurements"] = len(science_rows)
    payload["comparison_source"] = comparison_source
    payload["missing_rows"] = result.missing_legacy_rows
    payload["extra_rows"] = result.extra_legacy_rows
    return payload


def _has_table(session: Session, table_name: str) -> bool:
    """Return whether the current database physically contains ``table_name``.

    The check intentionally uses SQLAlchemy inspection rather than querying the
    relation so clean schemas never enter a failed PostgreSQL transaction merely
    because a frozen legacy table is absent.
    """

    bind = session.get_bind()
    return bool(inspect(bind).has_table(str(table_name), schema=None))


def _load_clean_science_photometry_states(
    session: Session,
    *,
    calibration_version_id: int,
) -> dict[tuple[int, int], SimpleNamespace]:
    """Load the public scalar state represented by narrow SCIENCE + BASE.

    Mutable calibration scalars come from ``science_photometry``; immutable
    extraction geometry comes from the referenced BASE measurement.  This is
    exactly the clean 0.7 durable representation and makes the parity check
    independent of the removed wide ``science_measurements`` table.
    """

    columns = [
        SciencePhotometry.base_measurement_id.label("base_measurement_id"),
        Measurement.object_id.label("object_id"),
        Measurement.frame_id.label("frame_id"),
    ]
    science_scalar_fields = set(SCIENCE_SCALAR_COLUMNS)
    for field_name in _RECONSTRUCTED_FIELDS:
        source = (
            getattr(SciencePhotometry, field_name)
            if field_name in science_scalar_fields
            else getattr(Measurement, field_name)
        )
        columns.append(source.label(field_name))

    stmt = (
        select(*columns)
        .select_from(SciencePhotometry)
        .join(
            Measurement,
            Measurement.measurement_id == SciencePhotometry.base_measurement_id,
        )
        .where(
            SciencePhotometry.calibration_version_id == int(calibration_version_id)
        )
        .order_by(Measurement.object_id, Measurement.frame_id)
    )
    rows = session.execute(stmt).mappings().all()
    result: dict[tuple[int, int], SimpleNamespace] = {}
    for row in rows:
        object_id = int(row["object_id"])
        frame_id = int(row["frame_id"])
        key = (object_id, frame_id)
        if key in result:
            raise RuntimeError(
                "Duplicate durable science identity while verifying parity: "
                f"calibration_version_id={calibration_version_id}, "
                f"object_id={object_id}, frame_id={frame_id}"
            )
        result[key] = SimpleNamespace(**dict(row))
    return result

def _resolve_shadow_comparison_baseline(
    session: Session,
    *,
    calibration_version_id: int,
    frame_ids: Iterable[int] | None,
    legacy: dict[tuple[int, int], Measurement],
) -> tuple[dict[tuple[int, int], Any], str]:
    if legacy:
        return legacy, "LEGACY_MEASUREMENTS"
    existing_science = _load_existing_science_measurements(
        session,
        calibration_version_id=int(calibration_version_id),
        frame_ids=frame_ids,
    )
    if existing_science:
        return existing_science, "EXISTING_SCIENCE_MEASUREMENTS"
    return {}, "NONE"


def _attach_shadow_effective_flags(
    reconstructed: dict[tuple[int, int], dict[str, Any]],
    shadow_baseline: dict[tuple[int, int], Any],
    *,
    shadow_source: str,
) -> None:
    """Attach the exact pre-write public flag view used for compact storage.

    Scalar reconstruction parity does not imply historical flag parity: normalized
    reconstruction intentionally does not model every legacy diagnostic/provenance
    key.  During a shadow-checked rewrite we therefore compact the *existing public
    flag view* against immutable BASE flags instead of substituting reconstructed
    flags.  This makes BASE || compact(SCIENCE) byte-semantically equivalent to the
    shadow baseline at the JSONB object level.
    """

    for key, state in reconstructed.items():
        baseline = shadow_baseline.get(key)
        if baseline is None:
            continue
        if shadow_source == "LEGACY_MEASUREMENTS":
            effective = dict(getattr(baseline, "flags", None) or {})
        elif shadow_source == "EXISTING_SCIENCE_MEASUREMENTS":
            baseline_base_id = getattr(baseline, "base_measurement_id", None)
            reconstructed_base_id = state.get("base_measurement_id")
            if (
                baseline_base_id is not None
                and reconstructed_base_id is not None
                and int(baseline_base_id) != int(reconstructed_base_id)
            ):
                raise ValueError(
                    "Refusing science flag compaction because shadow science BASE "
                    "identity differs from reconstructed BASE identity: "
                    f"object_id={key[0]}, frame_id={key[1]}, "
                    f"shadow_base_measurement_id={baseline_base_id}, "
                    f"reconstructed_base_measurement_id={reconstructed_base_id}"
                )
            effective = merge_effective_science_flags(
                state.get("_base_flags"),
                getattr(baseline, "flags", None),
            )
        else:
            raise ValueError(f"Unsupported shadow flag source: {shadow_source}")
        state["_shadow_effective_flags"] = effective


def _load_existing_science_measurements(
    session: Session,
    *,
    calibration_version_id: int,
    frame_ids: Iterable[int] | None = None,
) -> dict[tuple[int, int], ScienceMeasurement]:
    stmt = select(ScienceMeasurement).where(
        ScienceMeasurement.calibration_version_id == int(calibration_version_id)
    )
    normalized_frame_ids = sorted({int(value) for value in (frame_ids or [])})
    if normalized_frame_ids:
        stmt = stmt.where(ScienceMeasurement.frame_id.in_(normalized_frame_ids))
    rows = session.scalars(
        stmt.order_by(ScienceMeasurement.object_id, ScienceMeasurement.frame_id)
    ).all()
    return {(int(row.object_id), int(row.frame_id)): row for row in rows}


def _load_legacy_measurements(
    session: Session,
    *,
    calibration_version_id: int,
    frame_ids: Iterable[int] | None = None,
) -> dict[tuple[int, int], Measurement]:
    stmt = select(Measurement).where(
        Measurement.calibration_version_id == int(calibration_version_id)
    )
    normalized_frame_ids = sorted({int(value) for value in (frame_ids or [])})
    if normalized_frame_ids:
        stmt = stmt.where(Measurement.frame_id.in_(normalized_frame_ids))
    rows = session.scalars(stmt.order_by(Measurement.object_id, Measurement.frame_id)).all()
    return {(int(row.object_id), int(row.frame_id)): row for row in rows}


def _apply_shadow_comparison(
    result: ScienceMaterializationResult,
    reconstructed: dict[tuple[int, int], dict[str, Any]],
    legacy: dict[tuple[int, int], Any],
    *,
    tolerance: float,
) -> None:
    reconstructed_keys = set(reconstructed)
    legacy_keys = set(legacy)
    result.missing_legacy_rows = len(reconstructed_keys - legacy_keys)
    result.extra_legacy_rows = len(legacy_keys - reconstructed_keys)
    scalar_mismatches = 0
    max_abs_delta: dict[str, float] = {}
    for key in reconstructed_keys & legacy_keys:
        left = reconstructed[key]
        right = legacy[key]
        for field_name in _RECONSTRUCTED_FIELDS:
            lval = left.get(field_name)
            rval = getattr(right, field_name)
            delta = _absolute_delta(lval, rval)
            if delta is not None:
                max_abs_delta[field_name] = max(max_abs_delta.get(field_name, 0.0), delta)
            if not _equivalent(lval, rval, tolerance):
                scalar_mismatches += 1
    result.scalar_mismatches = scalar_mismatches
    result.max_abs_delta = max_abs_delta


def _resolve_cv(
    session: Session,
    *,
    calibration_name: str | None,
    calibration_version_id: int | None,
) -> CalibrationVersion:
    cv = None
    if calibration_version_id is not None:
        cv = session.get(CalibrationVersion, int(calibration_version_id))
    elif calibration_name is not None:
        cv = session.scalar(select(CalibrationVersion).where(CalibrationVersion.name == str(calibration_name)))
    if cv is None:
        raise ValueError("Calibration version not found")
    return cv


def _equivalent(left: Any, right: Any, tolerance: float) -> bool:
    if left is None and right is None:
        return True
    if left is None or right is None:
        return False
    try:
        lf = float(left)
        rf = float(right)
    except (TypeError, ValueError):
        return left == right
    if not math.isfinite(lf) or not math.isfinite(rf):
        return lf == rf
    return math.isclose(lf, rf, rel_tol=float(tolerance), abs_tol=float(tolerance))


def _absolute_delta(left: Any, right: Any) -> float | None:
    try:
        lf = float(left)
        rf = float(right)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(lf) or not math.isfinite(rf):
        return None
    return abs(lf - rf)
