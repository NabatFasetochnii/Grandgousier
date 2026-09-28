"""Read-only, bounded export of immutable final SCIENCE products to Parquet."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"
OBSERVING_NIGHT_DEFINITION = "date(DATE-OBS_UTC - 12 hours); DATE-OBS is UTC"


def observing_night(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return (value.astimezone(UTC) - timedelta(hours=12)).date().isoformat()


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"Not JSON serializable: {type(value).__name__}")


def _measurement_flags_json(flags: dict[str, Any] | None) -> str:
    """Serialize portable measurement flags without database-local catalog source IDs."""
    def strip_catalog_source_ids(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: strip_catalog_source_ids(item)
                for key, item in value.items()
                if key != "catalog_source_id"
            }
        if isinstance(value, (list, tuple)):
            return [strip_catalog_source_ids(item) for item in value]
        return value

    return json.dumps(strip_catalog_source_ids(flags or {}), sort_keys=True, default=_json_default)


def _schemas():
    try:
        import pyarrow as pa
    except ImportError as exc:
        raise RuntimeError("Parquet export requires `pip install grandgousier[parquet]` (pyarrow>=15)") from exc
    string, f64, i64, timestamp = pa.string(), pa.float64(), pa.int64(), pa.timestamp("us", tz="UTC")
    return {
        "sources.parquet": pa.schema([
            pa.field("object_uid", string, False), pa.field("ra_deg", f64), pa.field("dec_deg", f64),
            pa.field("identity_scheme", string, False), pa.field("catalog_links_json", string, False),
        ]),
        "frames.parquet": pa.schema([
            pa.field("series_uid", string, False), pa.field("observation_uid", string), pa.field("frame_uid", string, False),
            pa.field("identity_scheme", string), pa.field("native_identity", string), pa.field("date_obs", timestamp),
            pa.field("observing_night", string), pa.field("filter_name", string), pa.field("instrument", string),
            pa.field("site", string), pa.field("tube", string), pa.field("camera", string), pa.field("exptime", f64),
            pa.field("frame_quality_flags_json", string, False), pa.field("frame_qc_json", string, False),
        ]),
        "photometry.parquet": pa.schema([
            pa.field("object_uid", string, False), pa.field("observation_uid", string),
            pa.field("frame_uid", string, False), pa.field("identity_scheme", string),
            pa.field("native_identity", string), pa.field("series_uid", string, False),
            pa.field("mag_inst", f64), pa.field("mag_cal", f64), pa.field("flux", f64), pa.field("flux_err", f64),
            pa.field("mag_err", f64), pa.field("mag_err_random", f64), pa.field("mag_err_systematic", f64),
            pa.field("mag_global", f64), pa.field("mag_global_err", f64),
            pa.field("mag_global_err_random", f64), pa.field("mag_global_err_systematic", f64),
            pa.field("interseries_offset_mag", f64), pa.field("interseries_offset_err", f64),
            pa.field("interseries_offset_status", string, False),
            pa.field("local_zp_correction", f64), pa.field("aperture_radius_pix", f64),
            pa.field("measurement_flags_json", string, False), pa.field("object_qc_flags_json", string, False),
            pa.field("quality_flags_json", string, False), pa.field("is_rejected", pa.bool_(), False),
            pa.field("reject_reasons_json", string, False),
            pa.field("observing_night", string), pa.field("calibration_name", string, False),
        ]),
        "series.parquet": pa.schema([
            pa.field("series_uid", string, False), pa.field("field_uid", string, False), pa.field("filter_name", string),
            pa.field("status", string), pa.field("frame_count", i64, False), pa.field("science_row_count", i64, False),
            pa.field("recipe_json", string, False), pa.field("calibration_name", string, False),
            pa.field("stage2_pipeline_status", string), pa.field("stage2_science_status", string),
            pa.field("calibration_role", string), pa.field("calibration_lifecycle_status", string),
            pa.field("stage3_generation_uid", string), pa.field("stage3_status", string),
            pa.field("stage3_offset_mag", f64), pa.field("interseries_offset_status", string, False),
            pa.field("stage3_analysis_status", string), pa.field("stage3_graph_status", string),
            pa.field("stage3_reliability_status", string), pa.field("stage3_reduced_chi2", f64),
            pa.field("object_qc_row_count", i64), pa.field("object_qc_flag_count", i64),
            pa.field("frame_qc_good_count", i64),
            pa.field("frame_qc_suspect_count", i64), pa.field("frame_qc_bad_count", i64),
            pa.field("qc_status", string, False),
        ]),
    }


def _write_table(rows: Iterable[dict[str, Any]], path: Path, schema, batch_size: int) -> int:
    import pyarrow as pa
    import pyarrow.parquet as pq

    writer = pq.ParquetWriter(path, schema, compression="zstd")
    count = 0
    batch: list[dict[str, Any]] = []
    try:
        for row in rows:
            batch.append(row)
            if len(batch) >= batch_size:
                writer.write_table(pa.Table.from_pylist(batch, schema=schema))
                count += len(batch)
                batch.clear()
        if batch:
            writer.write_table(pa.Table.from_pylist(batch, schema=schema))
            count += len(batch)
    finally:
        writer.close()
    return count


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _science_contract_row(row: dict[str, Any], *, reject_flags: set[str]) -> dict[str, Any]:
    """Apply the canonical light-curve product QC/rejection contract to one DB row."""
    from grandgousier.lightcurves.product import _science_row_from_db

    normalized = _science_row_from_db(row, reject_flags=reject_flags)
    return {
        "mag_global": normalized["mag_global"],
        "mag_global_err": normalized["mag_global_err"],
        "mag_global_err_random": normalized["mag_global_err_random"],
        "mag_global_err_systematic": normalized["mag_global_err_systematic"],
        "interseries_offset_mag": normalized["interseries_offset_mag"],
        "interseries_offset_err": row.get("interseries_offset_err"),
        "interseries_offset_status": normalized["interseries_offset_status"],
        "quality_flags": normalized["quality_flags"],
        "is_rejected": normalized["is_rejected"],
        "reject_reasons": normalized["reject_reasons_list"],
    }


def _stage3_values(row: dict[str, Any], *, requested: bool, reject_flags: set[str]) -> dict[str, Any]:
    """Return shared Stage-3 magnitude derivation, never label Stage-2 mag as global."""
    values = _science_contract_row(row, reject_flags=reject_flags)
    if not requested:
        values.update(mag_global=None, mag_global_err=None, mag_global_err_random=None,
                      mag_global_err_systematic=None, interseries_offset_mag=None,
                      interseries_offset_err=None, interseries_offset_status="NOT_REQUESTED")
    return values


def _validate_stage3_generation(generation: Any) -> None:
    """Only accept persisted redundant-graph generations for final global magnitudes."""
    statuses = (generation.analysis_status, generation.graph_status, generation.reliability_status)
    if statuses != ("READY_M1_M3", "OK", "REDUNDANT_GRAPH_OK"):
        raise ValueError(
            "Interseries generation is diagnostic-only; accepted global photometry requires "
            "analysis=READY_M1_M3, graph=OK, reliability=REDUNDANT_GRAPH_OK"
        )


def _frame_qc_summary(flags: dict[str, Any] | None) -> tuple[str, dict[str, int]]:
    """Summarize persisted photometric frame-QC flags, not metric rows."""
    from grandgousier.calibration.photometric_frame_qc import PHOTOMETRIC_FRAME_QC_FLAG

    flags = flags or {}
    record = flags.get(PHOTOMETRIC_FRAME_QC_FLAG)
    counts = {"GOOD": 0, "SUSPECT": 0, "BAD": 0}
    status = str(record.get("status") or "").upper() if isinstance(record, dict) else ""
    if status in counts:
        counts[status] = 1
    return json.dumps(record if isinstance(record, dict) else {}, sort_keys=True, default=_json_default), counts


def _frame_qc_counts(flags: dict[str, Any] | None) -> dict[str, int]:
    return _frame_qc_summary(flags)[1]


def _series_qc_status(counts: dict[str, int], frame_count: int) -> str:
    """Summarize persisted frame-QC classifications without numeric thresholds."""
    classified = sum(counts.get(status, 0) for status in ("GOOD", "SUSPECT", "BAD"))
    if classified < frame_count:
        return "UNASSESSED"
    if counts.get("SUSPECT", 0) + counts.get("BAD", 0) > 0:
        return "DEGRADED"
    return "GOOD"


def _frame_qc_counts_statement(series_ids, field_series_frame, frame):
    """Build per-series frame-QC counts using one shared JSONB status expression."""
    from sqlalchemy import func, select

    from grandgousier.calibration.photometric_frame_qc import PHOTOMETRIC_FRAME_QC_FLAG

    status = frame.quality_flags[PHOTOMETRIC_FRAME_QC_FLAG]["status"].astext
    return select(
        field_series_frame.series_id,
        status,
        func.count(field_series_frame.frame_id),
    ).join(frame, frame.frame_id == field_series_frame.frame_id).where(
        field_series_frame.series_id.in_(series_ids)
    ).group_by(field_series_frame.series_id, status)


def _effective_detection_support(policy: dict[str, Any] | None) -> bool:
    policy = policy or {}
    return bool(policy.get("require_detection_support_effective", policy.get("require_detection_support", True)))


def _effective_photometric_frame_qc(persisted_recipe: dict[str, Any] | None) -> bool:
    """Resolve effective frame-QC enablement from persisted recipe policy."""
    recipe = persisted_recipe or {}
    policy = recipe.get("quality_policy", {})
    if not isinstance(policy, dict):
        policy = {}
    enabled = policy.get(
        "enable_photometric_frame_qc_effective",
        policy.get("enable_photometric_frame_qc", recipe.get("enable_photometric_frame_qc", False)),
    )
    return bool(enabled)


def export_parquet_product(
    tables: dict[str, Iterable[dict[str, Any]]], out: str | Path, *, metadata: dict[str, Any], batch_size: int = 4096
) -> dict[str, Any]:
    """Write explicitly-schema'd tables from independent bounded iterators; refuse overwrite."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    target = Path(out)
    if not target.is_absolute():
        raise ValueError("--out must be an absolute path")
    if target.exists():
        raise FileExistsError(f"Output already exists: {target}")
    schemas = _schemas()
    if set(tables) != set(schemas):
        raise ValueError("Table iterators must exactly match export schemas")
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    try:
        counts: dict[str, int] = {}
        files: dict[str, dict[str, Any]] = {}
        identity_digest = hashlib.sha256()
        identity_digest.update(json.dumps(
            {name: str(schema) for name, schema in schemas.items()},
            sort_keys=True, separators=(",", ":")
        ).encode("utf-8"))
        identity_digest.update(b"\n")
        for filename, schema in schemas.items():
            path = temp / filename
            def identified_rows(rows, filename=filename):
                for row in rows:
                    identity = {key: row.get(key) for key in
                        ("object_uid", "observation_uid", "frame_uid", "identity_scheme",
                         "native_identity", "series_uid", "field_uid") if key in row}
                    identity_digest.update(filename.encode("ascii") + b"\0")
                    identity_digest.update(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8"))
                    identity_digest.update(b"\n")
                    yield row
            counts[filename.removesuffix(".parquet")] = _write_table(identified_rows(tables[filename]), path, schema, batch_size)
            files[filename] = {"sha256": _sha256(path), "bytes": path.stat().st_size}
        manifest = {
            "manifest_type": "DATASET_MANIFEST_NOT_STEP_LOG", "schema_version": SCHEMA_VERSION,
            "generated_at_utc": datetime.now(UTC).isoformat(), "observing_night_definition": OBSERVING_NIGHT_DEFINITION,
            "row_counts": counts, "identity_set_sha256": identity_digest.hexdigest(), "files": files, **metadata,
        }
        (temp / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2, default=_json_default), encoding="utf-8")
        if target.exists():
            raise FileExistsError(f"Output already exists: {target}")
        os.rename(temp, target)
        return manifest
    except BaseException:
        shutil.rmtree(temp, ignore_errors=True)
        raise


def export_series(session, series_ids: list[int], out: str | Path, *, logical_db_id: str, calibration_version_id: int | None = None, interseries_generation_id: int | None = None, batch_size: int = 4096) -> dict[str, Any]:
    """Run export inside a PostgreSQL READ ONLY transaction; no ORM-wide materialization."""
    from sqlalchemy import func, select
    from sqlalchemy.dialects.postgresql import aggregate_order_by

    from grandgousier.db.models import (
        CalibrationProductAlias,
        CalibrationVersion,
        CatalogSource,
        Field,
        FieldSeries,
        FieldSeriesFrame,
        Frame,
        FrameQc,
        InterseriesCalibrationGeneration,
        InterseriesCalibrationSeries,
        Measurement,
        Object,
        ObjectCatalogLink,
        ObjectQc,
        Observation,
        PipelineRun,
        SciencePhotometry,
    )

    if Path(out).exists():
        raise FileExistsError(f"Output already exists: {out}")
    if session.bind.dialect.name != "postgresql":
        raise RuntimeError("Parquet database export requires PostgreSQL so READ ONLY can be asserted")
    session.execute(__import__("sqlalchemy").text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
    series_rows = list(session.execute(select(FieldSeries, Field).join(Field, Field.field_id == FieldSeries.field_id).where(FieldSeries.series_id.in_(series_ids))).all())
    if len(series_rows) != len(set(series_ids)):
        raise ValueError("One or more requested series do not exist")
    # Select alias heads in bulk; multiple current SCIENCE aliases in a series
    # are intentionally an ambiguity, regardless of their logical alias name.
    heads_stmt = select(CalibrationVersion, FieldSeries.series_id).join(
        CalibrationProductAlias,
        CalibrationProductAlias.head_calibration_version_id == CalibrationVersion.calibration_version_id,
    ).join(FieldSeries, FieldSeries.series_id == CalibrationVersion.series_id).where(
        FieldSeries.series_id.in_(series_ids), CalibrationVersion.product_role == "SCIENCE",
        CalibrationVersion.lifecycle_status == "COMPLETE_IMMUTABLE", CalibrationVersion.finalized_at.is_not(None),
    )
    if calibration_version_id is not None:
        heads_stmt = heads_stmt.where(CalibrationVersion.calibration_version_id == calibration_version_id)
    heads_by_series: dict[int, list[Any]] = {}
    for cv, sid in session.execute(heads_stmt):
        if cv.series_id != sid:
            raise ValueError("Calibration alias series scope mismatch")
        heads_by_series.setdefault(sid, []).append(cv)
    selected: dict[int, Any] = {}
    for series, _field in series_rows:
        heads = heads_by_series.get(series.series_id, [])
        if len(heads) != 1:
            raise ValueError(f"Series {series.series_id} has {len(heads)} final SCIENCE heads; specify --calibration-version-id to disambiguate")
        selected[series.series_id] = heads[0]

    series_ids = sorted(selected)
    cv_ids = [selected[sid].calibration_version_id for sid in series_ids]
    series_by_cv = {cv.calibration_version_id: sid for sid, cv in selected.items()}
    cv_by_id = {cv.calibration_version_id: cv for cv in selected.values()}
    stage3 = {}
    generation = None
    if interseries_generation_id is not None:
        generation = session.get(InterseriesCalibrationGeneration, interseries_generation_id)
        if generation is None or generation.lifecycle_status != "COMPLETE_IMMUTABLE":
            raise ValueError("Requested interseries generation is missing or not COMPLETE_IMMUTABLE")
        _validate_stage3_generation(generation)
        rows = list(session.scalars(select(InterseriesCalibrationSeries).where(
            InterseriesCalibrationSeries.interseries_generation_id == interseries_generation_id,
            InterseriesCalibrationSeries.series_id.in_(series_ids))).all())
        by_sid = {}
        for row in rows:
            if row.series_id in by_sid:
                raise ValueError(f"Ambiguous interseries row for series {row.series_id}")
            if row.science_calibration_version_id != selected[row.series_id].calibration_version_id:
                raise ValueError(f"Interseries SCIENCE calibration mismatch for series {row.series_id}")
            by_sid[row.series_id] = row
        if set(by_sid) != set(series_ids):
            raise ValueError("Interseries generation does not contain exactly one row for every requested series")
        if any(selected[sid].series_id != by_sid[sid].series_id for sid in series_ids):
            raise ValueError("Interseries generation scope mismatch")
        series_by_id = {series.series_id: series for series, _field in series_rows}
        if any(series_by_id[sid].field_id != generation.field_id or
               series_by_id[sid].filter_name != generation.filter_name or
               series_by_id[sid].instrument_unit_id != generation.instrument_unit_id for sid in series_ids):
            raise ValueError("Interseries generation field/filter scope mismatch")
        stage3 = by_sid
    runs = {run.pipeline_run_id: run for run in session.scalars(select(PipelineRun).where(
        PipelineRun.pipeline_run_id.in_([cv.pipeline_run_id for cv in selected.values() if cv.pipeline_run_id is not None]))).all()}
    def frame_iterator():
        qc_object = func.jsonb_build_object(
            "catalog_name", FrameQc.catalog_name, "algorithm", FrameQc.algorithm,
            "n_detections", FrameQc.n_detections, "n_catalog_sources", FrameQc.n_catalog_sources,
            "n_matched", FrameQc.n_matched, "zp_n", FrameQc.zp_n,
            "astrom_rms_arcsec", FrameQc.astrom_rms_arcsec, "dx_median_pix", FrameQc.dx_median_pix,
            "dy_median_pix", FrameQc.dy_median_pix, "sep_median_arcsec", FrameQc.sep_median_arcsec,
            "sep_p95_arcsec", FrameQc.sep_p95_arcsec, "zp_median", FrameQc.zp_median,
            "zp_mad", FrameQc.zp_mad, "details", FrameQc.details)
        qc_rows = select(FrameQc.frame_id.label("frame_id"), func.jsonb_agg(
            aggregate_order_by(qc_object, FrameQc.catalog_name)).label("rows")
        ).where(FrameQc.frame_id.in_(select(FieldSeriesFrame.frame_id).where(
            FieldSeriesFrame.series_id.in_(series_ids)))).group_by(FrameQc.frame_id).subquery()
        stmt = select(FieldSeries.series_uid, Frame, Observation.observation_uid, Observation.identity_scheme,
            Observation.native_identity, qc_rows.c.rows).join(FieldSeriesFrame,
            FieldSeriesFrame.series_id == FieldSeries.series_id).join(Frame,
            Frame.frame_id == FieldSeriesFrame.frame_id).outerjoin(Observation,
            Observation.observation_id == Frame.observation_id).outerjoin(qc_rows,
            qc_rows.c.frame_id == Frame.frame_id).where(FieldSeries.series_id.in_(series_ids)).order_by(
            FieldSeries.series_id, FieldSeriesFrame.order_index, Frame.frame_id)
        for uid, frame, observation_uid, identity_scheme, native_identity, aggregated_qc in session.execute(stmt).yield_per(batch_size):
            dt = frame.date_obs
            raw_qc = aggregated_qc or []
            yield {"series_uid": uid, "observation_uid": observation_uid, "frame_uid": frame.frame_uid,
                "identity_scheme": identity_scheme, "native_identity": native_identity,
                "date_obs": dt, "observing_night": observing_night(dt), "filter_name": frame.filter_name, "instrument": frame.instrument,
                "site": frame.site, "tube": frame.tube, "camera": frame.camera, "exptime": frame.exptime,
                "frame_quality_flags_json": json.dumps(frame.quality_flags or {}, sort_keys=True, default=_json_default),
                "frame_qc_json": json.dumps({"processing_status": getattr(frame.processing_status, "value", frame.processing_status),
                    "quality_flags": frame.quality_flags or {}, "photometric_frame_qc": _frame_qc_summary(frame.quality_flags)[0],
                    "frame_qc": raw_qc}, sort_keys=True, default=_json_default)}
    def source_iterator():
        stmt = select(Object.object_uid, Object.ra_deg, Object.dec_deg, Object.identity_scheme, CatalogSource.catalog_name, CatalogSource.source_id).join(Measurement, Measurement.object_id == Object.object_id).join(SciencePhotometry, SciencePhotometry.base_measurement_id == Measurement.measurement_id).join(FieldSeriesFrame, FieldSeriesFrame.frame_id == Measurement.frame_id).join(CalibrationVersion, CalibrationVersion.calibration_version_id == SciencePhotometry.calibration_version_id).join(ObjectCatalogLink, ObjectCatalogLink.object_id == Object.object_id, isouter=True).join(CatalogSource, CatalogSource.catalog_source_id == ObjectCatalogLink.catalog_source_id, isouter=True).where(FieldSeriesFrame.series_id.in_(series_ids), SciencePhotometry.calibration_version_id.in_(cv_ids), CalibrationVersion.series_id == FieldSeriesFrame.series_id).distinct().order_by(Object.object_uid, CatalogSource.catalog_name, CatalogSource.source_id)
        current = None
        links: list[dict[str, Any]] = []
        coords = None
        for uid, ra, dec, identity, catalog, source in session.execute(stmt).yield_per(batch_size):
            if current is not None and current != uid:
                yield {"object_uid": current, "ra_deg": coords[0], "dec_deg": coords[1], "identity_scheme": coords[2], "catalog_links_json": json.dumps(links, sort_keys=True)}
                links = []
            current, coords = uid, (ra, dec, identity)
            if catalog is not None:
                links.append({"catalog_name": catalog, "source_id": source})
        if current is not None:
            yield {"object_uid": current, "ra_deg": coords[0], "dec_deg": coords[1], "identity_scheme": coords[2], "catalog_links_json": json.dumps(links, sort_keys=True)}
    def photometry_iterator():
        stmt = select(Object.object_uid, Observation.observation_uid, Frame.frame_uid,
            Observation.identity_scheme, Observation.native_identity, FieldSeries.series_uid, SciencePhotometry,
            Measurement, CalibrationVersion.name, Frame.date_obs, Frame.quality_flags).join(Measurement, Measurement.measurement_id == SciencePhotometry.base_measurement_id).join(Object, Object.object_id == Measurement.object_id).join(Frame, Frame.frame_id == Measurement.frame_id).join(CalibrationVersion, CalibrationVersion.calibration_version_id == SciencePhotometry.calibration_version_id).join(FieldSeriesFrame, FieldSeriesFrame.frame_id == Frame.frame_id).join(FieldSeries, FieldSeries.series_id == FieldSeriesFrame.series_id).where(SciencePhotometry.calibration_version_id.in_(cv_ids)).order_by(FieldSeries.series_id, Measurement.measurement_id)
        stmt = stmt.where(CalibrationVersion.series_id == FieldSeries.series_id).outerjoin(Observation, Observation.observation_id == Frame.observation_id)
        from grandgousier.calibration.photometric_frame_qc import BAD_FRAME_REJECT_FLAG
        from grandgousier.lightcurves.product import light_curve_reject_flags
        result = session.execute(stmt).yield_per(batch_size)
        while True:
            chunk = result.fetchmany(batch_size)
            if not chunk:
                break
            keys = {(int(row[7].object_id), int(row[6].calibration_version_id)) for row in chunk}
            object_ids = {key[0] for key in keys}
            cv_chunk = {key[1] for key in keys}
            qc_stmt = select(ObjectQc.object_id, ObjectQc.calibration_version_id,
                ObjectQc.catalog_name, ObjectQc.instrument_unit_id, ObjectQc.algorithm, ObjectQc.flags).where(
                ObjectQc.object_id.in_(object_ids), ObjectQc.calibration_version_id.in_(cv_chunk))
            qc_map = {}
            for oid, cvid, catalog, instrument_id, algorithm, flags in session.execute(qc_stmt):
                key = (int(oid), int(cvid))
                cv = cv_by_id.get(int(cvid))
                if cv is None:
                    continue
                run = runs.get(cv.pipeline_run_id)
                recipe = run.recipe_json if run is not None and isinstance(run.recipe_json, dict) else {}
                expected_catalog = recipe.get("catalog_name")
                expected_instrument = recipe.get("instrument_unit_id")
                expected_algorithm = str(recipe.get("object_qc_algorithm", "object_qc_v1"))
                if (algorithm == expected_algorithm and (expected_catalog is None or catalog == expected_catalog)
                        and (expected_instrument is None or instrument_id == expected_instrument)):
                    if key in qc_map:
                        raise ValueError(f"QC_AMBIGUOUS: multiple ObjectQc candidates for object={oid}, cv={cvid}")
                    qc_map[key] = flags or {}
            for uid, obs_uid, frame_uid, identity_scheme, native_identity, series_uid, science, base, name, dt, frame_flags in chunk:
                qc = qc_map.get((int(base.object_id), int(science.calibration_version_id)), {})
                sid = series_by_cv[science.calibration_version_id]
                cv = selected[sid]
                recipe = cv.parameters or {}
                run = runs.get(cv.pipeline_run_id)
                persisted_recipe = run.recipe_json if run is not None else {}
                policy = persisted_recipe.get("quality_policy", {}) if isinstance(persisted_recipe, dict) else {}
                require_support = _effective_detection_support(policy if isinstance(policy, dict) else None)
                qc_enabled = _effective_photometric_frame_qc(persisted_recipe)
                extra_flags = BAD_FRAME_REJECT_FLAG if qc_enabled else None
                rejects = light_curve_reject_flags(require_detection_support=require_support, extra_flags=extra_flags)
                interseries = stage3.get(sid)
                derived = _stage3_values({"measurement_flags": base.flags or {}, "object_qc_flags": qc,
                    "frame_quality_flags": frame_flags or {}, "mag_cal": science.mag_cal,
                    "mag_err": science.mag_err, "mag_err_random": science.mag_err_random,
                    "mag_err_systematic": science.mag_err_systematic,
                    "interseries_offset_mag": interseries.offset_mag if interseries else None,
                    "interseries_offset_err": interseries.offset_err_mag if interseries else None,
                    "interseries_offset_status": "OFFSET_APPLIED" if interseries else None},
                    requested=interseries is not None, reject_flags=rejects)
                yield {"object_uid": uid, "observation_uid": obs_uid, "frame_uid": frame_uid,
                    "identity_scheme": identity_scheme, "native_identity": native_identity, "series_uid": series_uid,
                    "mag_inst": science.mag_inst, "mag_cal": science.mag_cal, "flux": science.flux, "flux_err": science.flux_err,
                    "mag_err": science.mag_err, "mag_err_random": science.mag_err_random, "mag_err_systematic": science.mag_err_systematic,
                    "mag_global": derived["mag_global"], "mag_global_err": derived["mag_global_err"],
                    "mag_global_err_random": derived["mag_global_err_random"],
                    "mag_global_err_systematic": derived["mag_global_err_systematic"],
                    "interseries_offset_mag": derived["interseries_offset_mag"],
                    "interseries_offset_err": derived["interseries_offset_err"],
                    "interseries_offset_status": derived["interseries_offset_status"],
                    "local_zp_correction": science.local_zp_correction, "aperture_radius_pix": base.aperture_radius_pix,
                    "measurement_flags_json": _measurement_flags_json(base.flags),
                    "object_qc_flags_json": json.dumps(qc, sort_keys=True, default=_json_default),
                    "quality_flags_json": json.dumps(derived["quality_flags"], sort_keys=True, default=_json_default),
                    "is_rejected": derived["is_rejected"], "reject_reasons_json": json.dumps(derived["reject_reasons"]),
                    "observing_night": observing_night(dt), "calibration_name": name}
    def series_iterator():
        science_counts = dict(session.execute(select(
            SciencePhotometry.calibration_version_id, func.count(SciencePhotometry.base_measurement_id)
        ).where(SciencePhotometry.calibration_version_id.in_(cv_ids)).group_by(
            SciencePhotometry.calibration_version_id)).tuples().all())
        frame_counts_by_series = dict(session.execute(select(
            FieldSeriesFrame.series_id, func.count(FieldSeriesFrame.frame_id)
        ).where(FieldSeriesFrame.series_id.in_(series_ids)).group_by(FieldSeriesFrame.series_id)).tuples().all())
        frame_qc_rows = session.execute(
            _frame_qc_counts_statement(series_ids, FieldSeriesFrame, Frame)
        )
        frame_qc_by_series = {sid: {"GOOD": 0, "SUSPECT": 0, "BAD": 0} for sid in series_ids}
        for sid, status, count in frame_qc_rows:
            if status and status.upper() in frame_qc_by_series[sid]:
                frame_qc_by_series[sid][status.upper()] += count
        qc_rows = session.execute(select(
            ObjectQc.calibration_version_id, func.count(ObjectQc.object_qc_id),
            func.count(ObjectQc.object_qc_id).filter(ObjectQc.flags != {}),
        ).where(ObjectQc.calibration_version_id.in_(cv_ids)).group_by(ObjectQc.calibration_version_id))
        qc_by_cv = {cvid: (row_count, flag_count) for cvid, row_count, flag_count in qc_rows}
        for series, field in sorted(series_rows, key=lambda pair: pair[0].series_id):
            cv = selected[series.series_id]
            run = runs.get(cv.pipeline_run_id)
            count = science_counts.get(cv.calibration_version_id, 0)
            frames = frame_counts_by_series.get(series.series_id, 0)
            frame_counts = frame_qc_by_series[series.series_id]
            qc_row_count, qc_flag_count = qc_by_cv.get(cv.calibration_version_id, (0, 0))
            yield {"series_uid": series.series_uid, "field_uid": field.field_uid, "filter_name": series.filter_name, "status": series.status,
                "frame_count": frames, "science_row_count": count, "recipe_json": json.dumps(cv.parameters or {}, sort_keys=True, default=_json_default),
                "calibration_name": cv.name, "stage2_pipeline_status": run.status if run else None,
                "stage2_science_status": (run.summary_json or {}).get("science_status") if run else None,
                "calibration_role": cv.product_role, "calibration_lifecycle_status": cv.lifecycle_status,
                "stage3_generation_uid": generation.generation_uid if generation else None,
                "stage3_status": generation.lifecycle_status if generation else "NOT_REQUESTED",
                "stage3_offset_mag": stage3[series.series_id].offset_mag if generation else None,
                "stage3_analysis_status": generation.analysis_status if generation else None,
                "stage3_graph_status": generation.graph_status if generation else None,
                "stage3_reliability_status": generation.reliability_status if generation else None,
                "stage3_reduced_chi2": generation.reduced_chi2 if generation else None,
                "interseries_offset_status": "OFFSET_APPLIED" if generation else "NOT_REQUESTED",
                 "object_qc_row_count": qc_row_count, "object_qc_flag_count": qc_flag_count,
                 "frame_qc_good_count": frame_counts["GOOD"], "frame_qc_suspect_count": frame_counts["SUSPECT"],
                 "frame_qc_bad_count": frame_counts["BAD"],
                 "qc_status": _series_qc_status(frame_counts, frames)}
    tables = {"sources.parquet": source_iterator(), "frames.parquet": frame_iterator(), "photometry.parquet": photometry_iterator(), "series.parquet": series_iterator()}
    return export_parquet_product(tables, out, metadata={"database_logical_id": logical_db_id, "series_uids": [s.series_uid for s, _ in series_rows],
        "calibration_versions": {selected[sid].name: {"generation": selected[sid].generation_no,
            "definition_fingerprint": selected[sid].definition_fingerprint_sha256, "input_fingerprint": selected[sid].input_fingerprint_sha256,
            "provenance_fingerprint": selected[sid].provenance_fingerprint_sha256} for sid in series_ids}}, batch_size=batch_size)
