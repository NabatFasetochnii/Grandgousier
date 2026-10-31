from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from astropy.io import fits

from grandgousier.archive import master_physical_archive
from grandgousier.archive.stage1_adapters import (
    build_archive_source_runtime,
    build_product_store,
)
from grandgousier.archive.stage1_contract import ArchiveRequest, AssetRef
from grandgousier.archive.stage1_execution import (
    STAGE1_PROVENANCE_SCHEMA,
    execute_stage1_plan,
)
from grandgousier.archive.stage1_planner import build_stage1_plan
from grandgousier.cli import build_parser


def _write_header(path: Path, values: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fits.PrimaryHDU(header=fits.Header(values)).writeto(path)


def _make_imdata_tree(
    root: Path,
    *,
    count: int = 1,
    camera: int = 204,
    dark_locator: str | None = None,
    reverse_creation: bool = False,
) -> list[Path]:
    raw_dir = root / "rawdata" / "2018" / "09" / "07"
    header_dir = root / "procdata" / "2018" / "09" / "07"
    dark_locator_default = "/master/imdata/rawdata/2018/09/07/9001.fit"
    flat_locator = "/master/imdata/rawdata/2018/09/07/9002.fit"
    raw_paths: list[Path] = []
    indexes = range(count - 1, -1, -1) if reverse_creation else range(count)
    for index in indexes:
        raw_id = 1001 + index
        raw_path = raw_dir / f"{raw_id}.fit"
        _write_header(
            raw_path,
            {
                "UNIQNUM": f"{camera}_{15814 + index}",
                "DATE-OBS": f"2018-09-07T17:{index:02d}:00Z",
                "ID_CAM": camera,
                "FILTER": "V",
                "IMAGETYP": "LIGHT",
                "OBJNAME": "MASTER TEST FIELD",
            },
        )
        _write_header(
            header_dir / f"{raw_id}_header.fits",
            {
                "DARK": dark_locator or dark_locator_default,
                "FLAT": flat_locator,
            },
        )
        raw_paths.append(raw_path)

    for raw_id, image_type in ((9001, "SDARK"), (9002, "SFLAT")):
        _write_header(
            raw_dir / f"{raw_id}.fit",
            {
                "UNIQNUM": f"{camera}_{raw_id}",
                "DATE-OBS": "2018-09-07T16:00:00Z",
                "ID_CAM": camera,
                "FILTER": "V",
                "IMAGETYP": image_type,
                "OBJNAME": "SYNTHETIC CALIBRATION ASSET",
            },
        )
        _write_header(
            header_dir / f"{raw_id}_header.fits",
            {"DARK": dark_locator_default, "FLAT": flat_locator},
        )
    return raw_paths


def test_probe_reports_metadata_identity_assets_and_profile_capability(tmp_path: Path) -> None:
    root = tmp_path / "imdata"
    raw_path = _make_imdata_tree(root)[0]

    report = master_physical_archive.probe_master_physical_archive(root)

    assert report["schema"] == "grandgousier.master.physical_archive_probe.v1"
    assert report["status"] == "PHYSICAL_INVENTORY_COMPLETE"
    assert report["inventory_complete"] is True
    assert report["assessment_scope"] == "LOCAL_PHYSICAL_INVENTORY_ONLY"
    assert report["stage2_science_readiness"] == "NOT_ASSESSED"
    assert report["calibration_compatibility"] == "NOT_ASSESSED"
    assert report["calibration_locator_contract"] == "UNRESOLVED_WITHOUT_REAL_ARCHIVE_EVIDENCE"
    assert report["execution_performed"] is False
    assert report["master_sql_required"] == "NO"
    assert report["network_required"] == "NO"
    assert report["discovered_raw_files"] == report["scanned_raw_files"] == 3
    assert report["discovered_raw_files_count_kind"] == "EXACT"
    item = report["items"][0]
    assert item["raw_absolute_path"] == str(raw_path.resolve())
    assert item["p4_identity"] == {
        "scheme": "master_uniqnum_v1",
        "native_identity": "204_15814",
        "status": "READY",
        "detail": None,
    }
    assert item["date_obs"] == "2018-09-07T17:00:00+00:00"
    assert item["camera"] == {
        "value": "204",
        "value_status": "PRESENT",
        "profile_support": "UNDECLARED",
        "audit_context_203_204": "MATCHES_AUDIT_CONTEXT_ONLY",
    }
    assert item["filter"] == {
        "raw": "V",
        "normalized": "V",
        "photometric_capability": "ENABLED",
    }
    assert item["image_type"]["normalized"] == "science"
    assert item["technical_stage1_eligibility"] == "ELIGIBLE_TO_ATTEMPT"
    assert report["items"][1]["image_type"]["normalized"] == "dark"
    assert report["items"][2]["image_type"]["normalized"] == "flat"
    assert [asset["role"] for asset in item["assets"]] == [
        "science_raw", "processing_header", "dark", "flat"
    ]
    assert all(asset["status"] == "RESOLVED" for asset in item["assets"])
    assert item["assets"][0]["absolute_path"] not in {
        item["assets"][2]["absolute_path"],
        item["assets"][3]["absolute_path"],
    }
    assert item["assets"][2]["absolute_path"] != item["assets"][3]["absolute_path"]


def test_probe_caps_raw_header_reads_before_inspecting_more_than_limit(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "imdata"
    _make_imdata_tree(root, count=4, reverse_creation=True)
    original_getheader = fits.getheader
    original_scandir = master_physical_archive.os.scandir
    reads: list[str] = []

    def counted_getheader(path, *args, **kwargs):
        reads.append(Path(path).name)
        return original_getheader(path, *args, **kwargs)

    def reverse_scandir(path):
        with original_scandir(path) as entries:
            snapshot = list(entries)

        @contextmanager
        def reversed_entries():
            yield iter(reversed(snapshot))

        return reversed_entries()

    monkeypatch.setattr(master_physical_archive.fits, "getheader", counted_getheader)
    monkeypatch.setattr(master_physical_archive.os, "scandir", reverse_scandir)
    monkeypatch.setattr(
        master_physical_archive.fits,
        "getdata",
        lambda *_args, **_kwargs: pytest.fail("the probe must not read FITS pixel arrays"),
    )

    report = master_physical_archive.probe_master_physical_archive(
        root, max_files_scanned=2
    )

    assert report["status"] == "PHYSICAL_INVENTORY_TRUNCATED"
    assert report["truncated"] is True
    assert report["discovered_raw_files"] == 3
    assert report["discovered_raw_files_count_kind"] == "LOWER_BOUND"
    assert report["scanned_raw_files"] == 2
    assert len([name for name in reads if name.endswith(".fit")]) == 2
    assert len([name for name in reads if name.endswith("_header.fits")]) == 2
    assert {item["p4_identity"]["native_identity"] for item in report["items"]} == {
        "204_15814", "204_15815"
    }


def test_probe_continues_after_unreadable_raw_header(tmp_path: Path) -> None:
    root = tmp_path / "imdata"
    raw_paths = _make_imdata_tree(root, count=3)
    raw_paths[1].write_bytes(b"not a FITS header")

    report = master_physical_archive.probe_master_physical_archive(root)

    assert report["status"] == "PHYSICAL_INVENTORY_INCOMPLETE"
    assert report["discovered_raw_files"] == 5
    assert report["discovered_raw_files_count_kind"] == "EXACT"
    rows_by_key = {row["raw_relative_physical_key"]: row for row in report["items"]}
    assert rows_by_key["rawdata/2018/09/07/1002.fit"]["errors"][0]["code"] == (
        "RAW_HEADER_READ_ERROR"
    )
    assert rows_by_key["rawdata/2018/09/07/1003.fit"]["p4_identity"]["native_identity"] == (
        "204_15816"
    )
    assert any(error["code"] == "RAW_HEADER_READ_ERROR" for error in report["errors"])


def test_probe_records_directory_scan_errors_and_marks_discovery_count_lower_bound(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "imdata"
    _make_imdata_tree(root)
    blocked_dir = root / "rawdata" / "2018" / "09" / "08"
    blocked_dir.mkdir(parents=True)
    original_scandir = master_physical_archive.os.scandir

    def guarded_scandir(path):
        if Path(path).resolve() == blocked_dir.resolve():
            raise PermissionError("synthetic unreadable directory")
        return original_scandir(path)

    monkeypatch.setattr(master_physical_archive.os, "scandir", guarded_scandir)
    report = master_physical_archive.probe_master_physical_archive(root)

    assert report["status"] == "PHYSICAL_INVENTORY_INCOMPLETE"
    assert report["discovered_raw_files"] == 3
    assert report["discovered_raw_files_count_kind"] == "LOWER_BOUND"
    assert report["discovery_errors"] == [
        {
            "code": "DIRECTORY_SCAN_ERROR",
            "path": str(blocked_dir),
            "error_type": "PermissionError",
        }
    ]


def test_processing_header_locator_mapping_is_canonical_and_date_aware() -> None:
    assert master_physical_archive._infer_master_header_locator(
        "/master/imdata/rawdata/2018/09/07/1001.fit"
    ) == "/master/imdata/procdata/2018/09/07/1001_header.fits"
    assert master_physical_archive._infer_master_header_locator(
        "/master/imdata/rawdata/2018/13/07/1001.fit"
    ) is None
    assert master_physical_archive._infer_master_header_locator(
        "/master/imdata/rawdata/2018/09/07/subdir/1001.fit"
    ) is None


def test_probe_refuses_malformed_processing_asset_locator(tmp_path: Path) -> None:
    root = tmp_path / "imdata"
    _make_imdata_tree(root, dark_locator=r"C:\outside\dark.fit")

    report = master_physical_archive.probe_master_physical_archive(root)

    assert report["status"] == "PHYSICAL_INVENTORY_INCOMPLETE"
    item = report["items"][0]
    dark = next(asset for asset in item["assets"] if asset["role"] == "dark")
    assert dark["status"] == "MALFORMED_LOCATOR"
    assert any(error["code"] == "MALFORMED_LOCATOR" for error in item["errors"])
    assert item["technical_stage1_eligibility"] == "BLOCKED"


def test_probe_refuses_processing_asset_symlink_that_resolves_outside_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "imdata"
    outside = tmp_path / "outside.fit"
    _write_header(outside, {"UNIQNUM": "outside"})
    escaped_locator = "/master/imdata/rawdata/2018/09/07/9999.fit"
    symlink_path = root / "rawdata" / "2018" / "09" / "07" / "9999.fit"
    symlink_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        symlink_path.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {type(exc).__name__}")
    _make_imdata_tree(root, dark_locator=escaped_locator)

    report = master_physical_archive.probe_master_physical_archive(
        root, max_files_scanned=1
    )

    dark = next(asset for asset in report["items"][0]["assets"] if asset["role"] == "dark")
    assert dark["status"] == "LOCATOR_ESCAPES_ROOT"
    assert report["items"][0]["technical_stage1_eligibility"] == "BLOCKED"


def test_probe_rejects_invalid_limit_and_cli_has_no_execute_option(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_files_scanned"):
        master_physical_archive.probe_master_physical_archive(tmp_path, max_files_scanned=0)

    parser = build_parser()
    args = parser.parse_args(
        ["probe-master-physical-archive", "--imdata-root", str(tmp_path)]
    )
    assert args.max_files == master_physical_archive.DEFAULT_MAX_MASTER_PHYSICAL_FILES
    assert args.func.__name__ == "cmd_probe_master_physical_archive"
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(
            ["probe-master-physical-archive", "--imdata-root", str(tmp_path), "--execute"]
        )
    assert exc_info.value.code == 2


def test_probe_cli_returns_json_without_database_session(tmp_path: Path, monkeypatch, capsys) -> None:
    from grandgousier import cli

    root = tmp_path / "imdata"
    _make_imdata_tree(root)
    monkeypatch.setattr(cli, "get_settings", lambda **_kwargs: SimpleNamespace(log_level="WARNING"))
    monkeypatch.setattr(cli, "configure_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli,
        "make_session_factory",
        lambda *_args, **_kwargs: pytest.fail("physical archive probe must not open a DB session"),
    )

    assert cli.main(["probe-master-physical-archive", "--imdata-root", str(root)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "PHYSICAL_INVENTORY_COMPLETE"


_SOURCE_ID = "synthetic-master-physical"
_RAW_KEY = "rawdata/2018/09/07/375506.fit.gz"
_RAW_DATE_OBS = "2018-09-07T17:04:22.902"
_DARK_LOCATOR = "/master/imdata/rawdata/2018/09/07/375453.fit.gz"
_FLAT_LOCATOR = "/master/imdata/rawdata/2018/08/31/373624.fit.gz"


def _write_tiny_image(
    path: Path,
    values: dict[str, object],
    *,
    shape: tuple[int, int] = (2, 3),
    pixel_offset: float = 0.0,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = fits.Header()
    for key, value in values.items():
        if value is not None:
            header[key] = value
    height, width = shape
    data = [
        [float(row * width + column + 1 + pixel_offset) for column in range(width)]
        for row in range(height)
    ]
    fits.PrimaryHDU(data=data, header=header).writeto(path)


def _write_physical_bundle(
    root: Path,
    *,
    raw_id: int = 375506,
    uniqnum: str = "204_15814",
    raw_updates: dict[str, object] | None = None,
    processing_updates: dict[str, object] | None = None,
    dark_updates: dict[str, object] | None = None,
    flat_updates: dict[str, object] | None = None,
    raw_shape: tuple[int, int] = (2, 3),
    dark_shape: tuple[int, int] = (2, 3),
    flat_shape: tuple[int, int] = (2, 3),
    sidecar_identity: bool = True,
    sidecar_primary_data: bool = False,
) -> tuple[str, dict[str, Path]]:
    raw_key = f"rawdata/2018/09/07/{raw_id}.fit.gz"
    raw_path = root / raw_key
    processing_path = root / "procdata" / "2018" / "09" / "07" / f"{raw_id}_header.fits"
    dark_path = root / "rawdata" / "2018" / "09" / "07" / "375453.fit.gz"
    flat_path = root / "rawdata" / "2018" / "08" / "31" / "373624.fit.gz"
    raw_values: dict[str, object] = {
        "UNIQNUM": uniqnum,
        "DATE-OBS": _RAW_DATE_OBS,
        "ID_CAM": 204,
        "FILTER": "V",
        "IMAGETYP": "LIGHT",
        "EXPTIME": 50.0,
        "XBINNING": 1,
        "YBINNING": 1,
        "CCD-TEMP": -10.0,
        "OBJNAME": "SYNTHETIC FIELD",
        "CRVAL1": 327.5,
        "CRVAL2": 72.1,
    }
    raw_values.update(raw_updates or {})
    processing_values: dict[str, object] = {
        "DARK": _DARK_LOCATOR,
        "FLAT": _FLAT_LOCATOR,
        "ID_CAM": 204,
        "FILTER": "V",
        "EXPTIME": 50.0,
        "XBINNING": 1,
        "YBINNING": 1,
        "IMAGEW": raw_shape[1],
        "IMAGEH": raw_shape[0],
        "CTYPE1": "RA---TAN-SIP",
        "CTYPE2": "DEC--TAN-SIP",
        "CRPIX1": 1.5,
        "CRPIX2": 1.0,
        "CRVAL1": 327.5,
        "CRVAL2": 72.1,
        "CD1_1": -0.0005,
        "CD1_2": 0.0,
        "CD2_1": 0.0,
        "CD2_2": 0.0005,
    }
    if sidecar_identity:
        processing_values["UNIQNUM"] = uniqnum
        processing_values["DATE-OBS"] = _RAW_DATE_OBS
    for key, value in (processing_updates or {}).items():
        if value is None:
            processing_values.pop(key, None)
        else:
            processing_values[key] = value
    dark_values: dict[str, object] = {
        "IMAGETYP": "SDARK",
        "ID_CAM": 204,
        "FILTER": "V",
        "EXPTIME": 50.0,
        "XBINNING": 1,
        "YBINNING": 1,
        "CCD-TEMP": -10.0,
    }
    dark_values.update(dark_updates or {})
    flat_values: dict[str, object] = {
        "IMAGETYP": "SFLAT",
        "ID_CAM": 204,
        "FILTER": "V",
        "EXPTIME": 20.0,
        "XBINNING": 1,
        "YBINNING": 1,
    }
    flat_values.update(flat_updates or {})
    _write_tiny_image(raw_path, raw_values, shape=raw_shape, pixel_offset=10.0)
    _write_tiny_image(dark_path, dark_values, shape=dark_shape)
    _write_tiny_image(flat_path, flat_values, shape=flat_shape)
    processing_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_header = fits.Header()
    for key, value in processing_values.items():
        sidecar_header[key] = value
    if sidecar_primary_data:
        fits.PrimaryHDU(data=[[1.0, 2.0], [3.0, 4.0]], header=sidecar_header).writeto(
            processing_path
        )
    else:
        fits.PrimaryHDU(header=sidecar_header).writeto(processing_path)
    return raw_key, {
        "science_raw": raw_path,
        "processing_header": processing_path,
        "dark": dark_path,
        "flat": flat_path,
    }


def _physical_payload(
    imdata_root: Path,
    raw_keys: list[str],
    product_root: Path,
) -> dict[str, object]:
    return {
        "archives": {
            _SOURCE_ID: {
                "adapter": "master-physical",
                "instrument_profile": "MASTER",
                "metadata": {
                    "backend": "physical-imdata-headers",
                    "imdata_root": str(imdata_root),
                    "raw_relative_physical_keys": raw_keys,
                },
                "assets": {"backend": "master-physical"},
            }
        },
        "products": {
            "backend": "filesystem",
            "store_id": "synthetic",
            "root": str(product_root),
        },
    }


def _physical_plan(
    imdata_root: Path,
    raw_keys: list[str],
    product_root: Path,
):
    payload = _physical_payload(imdata_root, raw_keys, product_root)
    runtime = build_archive_source_runtime(payload, _SOURCE_ID)
    plan = build_stage1_plan(
        request=ArchiveRequest(source_id=_SOURCE_ID),
        source=runtime.descriptor,
        metadata_provider=runtime.metadata_provider,
        asset_provider=runtime.asset_provider,
        product_store=build_product_store(payload),
    )
    return runtime, plan


def test_master_physical_source_builds_real_stage1_plan_with_canonical_roles(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "external" / "imdata"
    raw_key, _paths = _write_physical_bundle(root)
    monkeypatch.setattr(
        master_physical_archive.fits,
        "getdata",
        lambda *_args, **_kwargs: pytest.fail("source preflight must not read pixel arrays"),
    )

    runtime, plan = _physical_plan(root, [raw_key], tmp_path / "products")

    assert runtime.descriptor.adapter == "master-physical"
    assert plan.status == "READY"
    assert plan.discovered_records == plan.ready_items == 1
    record = plan.items[0].observation
    assert record.identity_status == "READY"
    assert record.native_observation_id == record.p4_native_identity == "204_15814"
    assert record.metadata["raw_relative_physical_key"] == raw_key
    assert record.metadata["raw_id"] == "375506"
    assert record.native_observation_id != raw_key
    assert record.camera == "204"
    assert record.filter_name == "V"
    assert record.exptime == 50.0
    assert record.observed_at == "2018-09-07T17:04:22.902000+00:00"
    assert [asset.role for asset in record.assets] == [
        "science_raw", "processing_header", "dark", "flat"
    ]
    assert all(asset.backend == "master-physical" for asset in record.assets)
    assert all(asset.locator.startswith("/master/imdata/") for asset in record.assets)
    assert [asset.locator for asset in record.assets] == [
        f"/master/imdata/{_RAW_KEY}",
        "/master/imdata/procdata/2018/09/07/375506_header.fits",
        _DARK_LOCATOR,
        _FLAT_LOCATOR,
    ]
    raw_header = fits.getheader(root / _RAW_KEY)
    assert raw_header["UNIQNUM"] == "204_15814"
    assert raw_header["DATE-OBS"] == _RAW_DATE_OBS
    assert raw_header["ID_CAM"] == 204
    assert raw_header["FILTER"] == "V"
    assert raw_header["EXPTIME"] == 50.0
    for ref in record.assets:
        assert ref.metadata["raw_relative_physical_key"] == raw_key
        assert runtime.asset_provider.materialize(ref).path.is_file()
    assert not (tmp_path / "products").exists()


def test_master_physical_bundle_executes_one_tiny_stage1_reduction_under_tmp_path(
    tmp_path: Path,
) -> None:
    root = tmp_path / "external" / "imdata"
    product_root = tmp_path / "products"
    raw_key, _paths = _write_physical_bundle(root)
    runtime, plan = _physical_plan(root, [raw_key], product_root)
    payload = _physical_payload(root, [raw_key], product_root)
    product_store = build_product_store(payload)
    assert root.resolve().is_relative_to(tmp_path.resolve())
    assert product_root.resolve().is_relative_to(tmp_path.resolve())

    report = execute_stage1_plan(
        plan,
        asset_provider=runtime.asset_provider,
        product_store=product_store,
        overwrite=False,
    )

    assert report.status == "READY"
    result = report.results[0]
    assert result.status == "REDUCED"
    product_path = Path(result.product_path)
    provenance_path = Path(result.provenance_path)
    assert product_path.is_file()
    assert provenance_path.is_file()
    assert product_path.resolve().is_relative_to(tmp_path.resolve())
    assert provenance_path.resolve().is_relative_to(tmp_path.resolve())

    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    assert provenance["schema"] == STAGE1_PROVENANCE_SCHEMA
    assert provenance["execution_status"] == "REDUCED"
    assert provenance["observation"]["native_observation_id"] == "204_15814"
    assert provenance["fingerprint"] == result.fingerprint
    assert provenance["product"]["checksum_sha256"] == result.product_checksum_sha256
    input_locators = {
        entry["ref"]["role"]: entry["ref"]["locator"]
        for entry in provenance["inputs"]
    }
    assert input_locators == {
        "science_raw": f"/master/imdata/{_RAW_KEY}",
        "processing_header": "/master/imdata/procdata/2018/09/07/375506_header.fits",
        "dark": _DARK_LOCATOR,
        "flat": _FLAT_LOCATOR,
    }
    with fits.open(product_path, memmap=False) as hdul:
        assert hdul[0].data.shape == (2, 3)
        assert float(hdul[0].data[0, 0]) == pytest.approx(10.0)
        assert float(hdul[0].data[1, 2]) == pytest.approx(10.0 / 6.0)
        assert hdul[1].name == "DQ"
        assert set(hdul[1].data.flat) == {0}
        assert hdul[0].header["GGRAW"] == f"/master/imdata/{_RAW_KEY}"
        assert hdul[0].header["GGHDR"] == input_locators["processing_header"]
        assert hdul[0].header["DARK"] == _DARK_LOCATOR
        assert hdul[0].header["FLAT"] == _FLAT_LOCATOR


def test_master_physical_optional_sidecar_identity_cards_and_profile_aliases(
    tmp_path: Path,
) -> None:
    root = tmp_path / "external" / "imdata"
    raw_key, _paths = _write_physical_bundle(root, sidecar_identity=False)

    runtime, plan = _physical_plan(root, [raw_key], tmp_path / "products")

    assert plan.status == "READY"
    assert plan.items[0].observation.identity_status == "READY"
    assert runtime.metadata_provider.profile.normalize_image_type("SDARK") == "dark"
    assert runtime.metadata_provider.profile.normalize_image_type("SFLAT") == "flat"
    assert runtime.metadata_provider.profile.normalize_filter("V") == "V"


@pytest.mark.parametrize(
    ("damage", "expected_code"),
    [
        ("dark-type", "ROLE_MISMATCH"),
        ("flat-type", "ROLE_MISMATCH"),
        ("camera", "REDUCER_INPUT_VALIDATION_FAILED"),
        ("filter", "REDUCER_INPUT_VALIDATION_FAILED"),
        ("geometry", "REDUCER_INPUT_VALIDATION_FAILED"),
        ("flat-camera", "REDUCER_INPUT_VALIDATION_FAILED"),
        ("flat-filter", "REDUCER_INPUT_VALIDATION_FAILED"),
        ("flat-geometry", "REDUCER_INPUT_VALIDATION_FAILED"),
        ("missing-exposure", "REQUIRED_HEADER_CARD_MISSING"),
        ("missing-wcs", "REDUCER_INPUT_VALIDATION_FAILED"),
    ],
)
def test_master_physical_reducer_header_mismatches_block_plan_but_keep_identity(
    tmp_path: Path, damage: str, expected_code: str
) -> None:
    root = tmp_path / "external" / "imdata"
    kwargs: dict[str, object] = {}
    if damage == "dark-type":
        kwargs["dark_updates"] = {"IMAGETYP": "DARK"}
    elif damage == "flat-type":
        kwargs["flat_updates"] = {"IMAGETYP": "FLAT"}
    elif damage == "camera":
        kwargs["dark_updates"] = {"ID_CAM": 203}
    elif damage == "filter":
        kwargs["processing_updates"] = {"FILTER": "R"}
    elif damage == "flat-camera":
        kwargs["flat_updates"] = {"ID_CAM": 203}
    elif damage == "flat-filter":
        kwargs["flat_updates"] = {"FILTER": "R"}
    elif damage == "flat-geometry":
        kwargs["flat_shape"] = (2, 2)
    elif damage == "missing-exposure":
        kwargs["raw_updates"] = {"EXPTIME": None}
    elif damage == "missing-wcs":
        kwargs["processing_updates"] = {"CTYPE1": None}
    else:
        kwargs["dark_shape"] = (2, 2)
    raw_key, _paths = _write_physical_bundle(root, **kwargs)

    _runtime, plan = _physical_plan(root, [raw_key], tmp_path / "products")

    item = plan.items[0]
    assert plan.status == item.status == "BLOCKED"
    assert item.observation.identity_status == "READY"
    assert any(expected_code in issue for issue in item.issues)


@pytest.mark.parametrize(
    ("mismatch", "expected_code"),
    [
        ("uniqnum", "SIDECAR_UNIQNUM_MISMATCH"),
        ("date", "SIDECAR_DATE_OBS_MISMATCH"),
    ],
)
def test_master_physical_raw_sidecar_identity_mismatch_blocks_plan_without_identity_fallback(
    tmp_path: Path, mismatch: str, expected_code: str
) -> None:
    root = tmp_path / "external" / "imdata"
    update = {"UNIQNUM": "204_15815"} if mismatch == "uniqnum" else {
        "DATE-OBS": "2018-09-07T17:00:01Z"
    }
    raw_key, _paths = _write_physical_bundle(root, processing_updates=update)

    _runtime, plan = _physical_plan(root, [raw_key], tmp_path / "products")

    item = plan.items[0]
    assert item.status == "BLOCKED"
    assert item.observation.identity_status == "READY"
    assert item.observation.native_observation_id == "204_15814"
    assert expected_code in str(item.observation.metadata["stage1_blockers"])
    assert any(expected_code in issue for issue in item.issues)


def test_master_physical_invalid_raw_date_blocks_technical_readiness_not_p4_identity(
    tmp_path: Path,
) -> None:
    root = tmp_path / "external" / "imdata"
    raw_key, _paths = _write_physical_bundle(
        root,
        raw_updates={"DATE-OBS": "not-a-date"},
        sidecar_identity=False,
    )

    _runtime, plan = _physical_plan(root, [raw_key], tmp_path / "products")

    item = plan.items[0]
    assert item.status == "BLOCKED"
    assert item.observation.identity_status == "READY"
    assert item.observation.native_observation_id == "204_15814"
    assert item.observation.observed_at is None
    assert any("INVALID_RAW_DATE_OBS" in issue for issue in item.issues)


@pytest.mark.parametrize(
    "missing",
    ["raw", "processing_header", "dark", "dark-reference", "flat", "flat-reference"],
)
def test_master_physical_missing_files_or_references_reach_blocked_plan(
    tmp_path: Path, missing: str
) -> None:
    root = tmp_path / "external" / "imdata"
    processing_updates = (
        {"DARK": None}
        if missing == "dark-reference"
        else {"FLAT": None}
        if missing == "flat-reference"
        else None
    )
    raw_key, paths = _write_physical_bundle(root, processing_updates=processing_updates)
    if missing not in {"dark-reference", "flat-reference"}:
        paths["science_raw" if missing == "raw" else missing].unlink()

    _runtime, plan = _physical_plan(root, [raw_key], tmp_path / "products")

    item = plan.items[0]
    assert plan.status == item.status == "BLOCKED"
    if missing == "raw":
        assert item.observation.identity_status == "MISSING"
        assert any("ASSET_NOT_FOUND" in issue for issue in item.issues)
    elif missing in {"dark-reference", "flat-reference"}:
        missing_role = "dark" if missing == "dark-reference" else "flat"
        assert missing_role in item.missing_asset_roles
        assert any("MISSING_CALIBRATION_REFERENCE" in issue for issue in item.issues)
        assert item.observation.identity_status == "READY"
    else:
        assert item.observation.identity_status == "READY"
        assert any("ASSET_NOT_FOUND" in issue for issue in item.issues)


def test_master_physical_rejects_impossible_date_in_referenced_canonical_flat_locator(
    tmp_path: Path,
) -> None:
    root = tmp_path / "external" / "imdata"
    invalid_locator = "/master/imdata/rawdata/2018/13/07/373624.fit.gz"
    raw_key, _paths = _write_physical_bundle(
        root,
        processing_updates={"FLAT": invalid_locator},
    )
    _write_tiny_image(
        root / "rawdata" / "2018" / "13" / "07" / "373624.fit.gz",
        {
            "IMAGETYP": "SFLAT",
            "ID_CAM": 204,
            "FILTER": "V",
            "XBINNING": 1,
            "YBINNING": 1,
        },
    )

    _runtime, plan = _physical_plan(root, [raw_key], tmp_path / "products")

    item = plan.items[0]
    assert plan.status == item.status == "BLOCKED"
    assert item.observation.identity_status == "READY"
    assert "flat" in item.missing_asset_roles
    assert any(
        blocker["code"] == "INVALID_ARCHIVE_DATE"
        for blocker in item.observation.metadata["stage1_blockers"]
    )


def test_master_physical_processing_header_must_have_no_primary_data(tmp_path: Path) -> None:
    root = tmp_path / "external" / "imdata"
    raw_key, _paths = _write_physical_bundle(root, sidecar_primary_data=True)

    _runtime, plan = _physical_plan(root, [raw_key], tmp_path / "products")

    assert plan.status == "BLOCKED"
    assert any("ROLE_MISMATCH" in issue for issue in plan.items[0].issues)


def test_master_physical_rejects_noncanonical_and_traversal_paths(tmp_path: Path) -> None:
    root = tmp_path / "external" / "imdata"
    raw_key, _paths = _write_physical_bundle(
        root,
        processing_updates={"DARK": "/master/imdata/rawdata/2018/09/07/../9001.fit.gz"},
    )
    _runtime, plan = _physical_plan(root, [raw_key], tmp_path / "products")
    assert plan.status == "BLOCKED"
    assert "dark" in plan.items[0].missing_asset_roles
    assert any("NONCANONICAL_LOCATOR" in issue for issue in plan.items[0].issues)

    payload = _physical_payload(root, ["rawdata/2018/09/07/../1001.fit.gz"], tmp_path / "products")
    with pytest.raises(ValueError, match="physical key"):
        build_archive_source_runtime(payload, _SOURCE_ID)

    traversal_ref = AssetRef(
        source_id=_SOURCE_ID,
        role="dark",
        backend="master-physical",
        locator="/master/imdata/rawdata/2018/09/07/../9001.fit.gz",
    )
    for locator in (
        traversal_ref.locator,
        r"C:\outside\dark.fit",
        "master/imdata/rawdata/2018/09/07/9001.fit.gz",
    ):
        ref = AssetRef(
            source_id=_SOURCE_ID,
            role="dark",
            backend="master-physical",
            locator=locator,
        )
        with pytest.raises(ValueError, match="canonical"):
            _runtime.asset_provider.materialize(ref)


def test_master_physical_rejects_symlink_escape(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "external" / "imdata"
    raw_key, paths = _write_physical_bundle(root)
    outside = tmp_path / "outside.fit.gz"
    _write_tiny_image(
        outside,
        {"IMAGETYP": "SDARK", "ID_CAM": 204, "XBINNING": 1, "YBINNING": 1},
    )
    paths["dark"].unlink()
    try:
        paths["dark"].symlink_to(outside)
    except OSError:
        # Model the resolved target of a symlink on Windows hosts that disallow
        # unprivileged symlink creation; the same containment branch is exercised.
        original_resolve = Path.resolve
        outside_resolved = outside.resolve()

        def resolve_with_escape(path: Path, *, strict: bool = False) -> Path:
            if path == paths["dark"]:
                return outside_resolved
            return original_resolve(path, strict=strict)

        monkeypatch.setattr(Path, "resolve", resolve_with_escape)

    _runtime, plan = _physical_plan(root, [raw_key], tmp_path / "products")

    assert plan.status == "BLOCKED"
    assert any("LOCATOR_ESCAPES_ROOT" in issue for issue in plan.items[0].issues)


def test_master_physical_provider_rejects_a_locator_whose_file_role_disagrees(
    tmp_path: Path,
) -> None:
    root = tmp_path / "external" / "imdata"
    raw_key, _paths = _write_physical_bundle(
        root,
        processing_updates={"DARK": _FLAT_LOCATOR},
    )
    runtime, plan = _physical_plan(root, [raw_key], tmp_path / "products")
    dark_ref = next(ref for ref in plan.items[0].observation.assets if ref.role == "dark")

    assert plan.status == "BLOCKED"
    with pytest.raises(ValueError, match="requires FITS IMAGETYP=SDARK"):
        runtime.asset_provider.materialize(dark_ref)


def test_master_physical_planning_is_key_bounded_header_only_and_db_free(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "external" / "imdata"
    selected_key, _paths = _write_physical_bundle(root)
    _write_tiny_image(
        root / "rawdata" / "2018" / "09" / "07" / "2002.fit.gz",
        {
            "UNIQNUM": "204_15815",
            "DATE-OBS": "2018-09-07T17:01:00Z",
            "ID_CAM": 204,
            "FILTER": "V",
            "IMAGETYP": "LIGHT",
            "EXPTIME": 50.0,
            "XBINNING": 1,
            "YBINNING": 1,
        },
    )
    monkeypatch.setattr(
        master_physical_archive.fits,
        "getdata",
        lambda *_args, **_kwargs: pytest.fail("physical Stage-1 planning read a pixel array"),
    )
    monkeypatch.setattr(
        master_physical_archive,
        "_discover_raw_fits",
        lambda *_args, **_kwargs: pytest.fail("physical Stage-1 planning discovered the archive"),
    )
    monkeypatch.setattr(
        master_physical_archive.os,
        "scandir",
        lambda *_args, **_kwargs: pytest.fail("physical Stage-1 planning scanned directories"),
    )

    runtime, plan = _physical_plan(root, [selected_key], tmp_path / "products")

    assert runtime.descriptor.metadata_backend == "physical-imdata-headers"
    assert plan.discovered_records == plan.ready_items == 1
    assert plan.items[0].observation.native_observation_id == "204_15814"


def test_master_physical_explicit_key_cap_precedes_any_fits_read(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "external" / "imdata"
    selected_key, _paths = _write_physical_bundle(root)
    key_limit = master_physical_archive.DEFAULT_MAX_MASTER_PHYSICAL_FILES
    raw_keys = [selected_key] + [
        f"rawdata/2018/09/07/{400000 + index}.fit.gz" for index in range(key_limit)
    ]
    header_reads: list[str] = []
    original_getheader = fits.getheader

    def counted_getheader(path, *args, **kwargs):
        header_reads.append(str(path))
        return original_getheader(path, *args, **kwargs)

    monkeypatch.setattr(master_physical_archive.fits, "getheader", counted_getheader)
    payload = _physical_payload(root, raw_keys, tmp_path / "products")

    with pytest.raises(ValueError, match="at most 100"):
        runtime = build_archive_source_runtime(payload, _SOURCE_ID)
        list(runtime.metadata_provider.search(ArchiveRequest(source_id=_SOURCE_ID)))

    assert header_reads == []


def test_master_physical_stage1_plan_cli_opens_no_database_session(
    tmp_path: Path, monkeypatch
) -> None:
    from grandgousier import cli

    root = tmp_path / "external" / "imdata"
    raw_key, _paths = _write_physical_bundle(root)
    payload = _physical_payload(root, [raw_key], tmp_path / "products")
    config_path = tmp_path / "physical-config.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(
        cli,
        "make_session_factory",
        lambda *_args, **_kwargs: pytest.fail("stage1-plan must not create a DB session"),
    )
    monkeypatch.setattr(
        master_physical_archive.fits,
        "getdata",
        lambda *_args, **_kwargs: pytest.fail("stage1-plan must not read FITS pixel arrays"),
    )
    monkeypatch.setattr(
        master_physical_archive,
        "_discover_raw_fits",
        lambda *_args, **_kwargs: pytest.fail("stage1-plan must not discover archive files"),
    )

    plan_path = tmp_path / "plan.json"
    csv_path = tmp_path / "plan.csv"
    result = cli.main(
        [
            "stage1-plan",
            "--config",
            str(config_path),
            "--source",
            _SOURCE_ID,
            "--out",
            str(plan_path),
            "--csv-out",
            str(csv_path),
            "--format",
            "json",
        ]
    )

    assert result == 0
    assert json.loads(plan_path.read_text(encoding="utf-8"))["status"] == "READY"
    assert csv_path.is_file()
