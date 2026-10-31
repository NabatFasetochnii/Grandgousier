from __future__ import annotations

from pathlib import Path
import warnings

import numpy as np
import pytest

import grandgousier.archive.master_reduction as master_reduction
from grandgousier.archive.master_reduction import (
    DQ_CALIBRATED_INVALID,
    DQ_FLAT_INVALID,
    DQ_RAW_SATURATED,
    MasterReductionTask,
    build_master_reduction_tasks,
    calibrate_master_arrays,
    compact_existing_master_output,
    physical_saturation_level,
    reduce_master_task,
    reduction_metrics_from_header,
    _is_wcs_keyword,
    validate_master_reduction_inputs,
)


def _task(tmp_path: Path) -> MasterReductionTask:
    return MasterReductionTask(
        processed_frame_id="131841",
        raw_id=375506,
        raw_remote_path="/master/imdata/rawdata/2018/09/07/375506.fit.gz",
        header_remote_path="/master/imdata/procdata/2018/09/07/375506_header.fits",
        dark_remote_path="/master/imdata/rawdata/2018/09/07/375453.fit.gz",
        flat_remote_path="/master/imdata/rawdata/2018/08/31/373624.fit.gz",
        raw_local_path=tmp_path / "raw.fit.gz",
        header_local_path=tmp_path / "header.fits",
        dark_local_path=tmp_path / "dark.fit.gz",
        flat_local_path=tmp_path / "flat.fit.gz",
        output_path=tmp_path / "processed" / "375506_processed.fits",
    )


def _headers() -> tuple[dict[str, object], dict[str, object], dict[str, object], dict[str, object]]:
    raw = {
        "IMAGETYP": "LIGHT",
        "ID_CAM": 204,
        "XBINNING": 1,
        "YBINNING": 1,
        "FILTER": "V",
        "EXPTIME": 50.0,
        "CCD-TEMP": -9.887,
        "BITPIX": 16,
        "BZERO": 32768,
        "BSCALE": 1,
    }
    dark = {
        "IMAGETYP": "SDARK",
        "ID_CAM": 204,
        "XBINNING": 1,
        "YBINNING": 1,
        "FILTER": "",
        "EXPTIME": 50.0,
        "CCD-TEMP": -10.0,
    }
    flat = {
        "IMAGETYP": "SFLAT",
        "ID_CAM": 204,
        "XBINNING": 1,
        "YBINNING": 1,
        "FILTER": "V",
        "EXPTIME": 5.0,
    }
    header = {
        "ID_CAM": 204,
        "XBINNING": 1,
        "YBINNING": 1,
        "FILTER": "V",
        "EXPTIME": 50.0,
        "IMAGEW": 2,
        "IMAGEH": 2,
        "CTYPE1": "RA---TAN-SIP",
        "CTYPE2": "DEC--TAN-SIP",
        "CRPIX1": 1.5,
        "CRPIX2": 1.5,
        "CRVAL1": 327.52,
        "CRVAL2": 72.18,
        "ALPHA": "21 50 25",
        "DELTA": "+72 11 00",
        "CD1_1": -0.0005,
        "CD1_2": 0.0,
        "CD2_1": 0.0,
        "CD2_2": 0.0005,
        "DARK": "/master/imdata/rawdata/2018/09/07/375453.fit.gz",
        "FLAT": "/master/imdata/rawdata/2018/08/31/373624.fit.gz",
    }
    return raw, dark, flat, header


def test_build_reduction_tasks_maps_cache_and_output(tmp_path: Path) -> None:
    rows = [
        {
            "processed_frame_id": "131841",
            "raw_path": "/master/imdata/rawdata/2018/09/07/375506.fit.gz",
            "header_path": "/master/imdata/procdata/2018/09/07/375506_header.fits",
            "dark_path": "/master/imdata/rawdata/2018/09/07/375453.fit.gz",
            "flat_path": "/master/imdata/rawdata/2018/08/31/373624.fit.gz",
        }
    ]
    tasks = build_master_reduction_tasks(rows, cache_root=tmp_path / "cache", output_root=tmp_path / "out")
    assert len(tasks) == 1
    task = tasks[0]
    assert task.raw_local_path == tmp_path / "cache/master/imdata/rawdata/2018/09/07/375506.fit.gz"
    assert task.header_local_path == tmp_path / "cache/master/imdata/procdata/2018/09/07/375506_header.fits"
    assert task.output_path == tmp_path / "out/2018/09/07/375506_processed.fits"


def test_build_reduction_tasks_requires_all_four_components(tmp_path: Path) -> None:
    rows = [
        {
            "processed_frame_id": "131841",
            "raw_path": "/master/imdata/rawdata/2018/09/07/375506.fit.gz",
            "header_path": "",
            "dark_path": "/master/imdata/rawdata/2018/09/07/375453.fit.gz",
            "flat_path": "/master/imdata/rawdata/2018/08/31/373624.fit.gz",
        }
    ]
    with pytest.raises(ValueError, match="missing header"):
        build_master_reduction_tasks(rows, cache_root=tmp_path)


def test_build_reduction_tasks_rejects_conflicting_calibrations(tmp_path: Path) -> None:
    base = {
        "processed_frame_id": "131841",
        "raw_path": "/master/imdata/rawdata/2018/09/07/375506.fit.gz",
        "header_path": "/master/imdata/procdata/2018/09/07/375506_header.fits",
        "dark_path": "/master/imdata/rawdata/2018/09/07/375453.fit.gz",
        "flat_path": "/master/imdata/rawdata/2018/08/31/373624.fit.gz",
    }
    other = dict(base)
    other["dark_path"] = "/master/imdata/rawdata/2018/09/07/999999.fit.gz"
    with pytest.raises(ValueError, match="Conflicting calibration"):
        build_master_reduction_tasks([base, other], cache_root=tmp_path)


def test_calibrate_master_arrays_is_raw_minus_dark_divided_by_flat() -> None:
    raw = np.array([[100.0, 110.0], [120.0, 130.0]])
    dark = np.array([[10.0, 10.0], [20.0, 20.0]])
    flat = np.array([[1.0, 0.5], [2.0, 1.0]])
    calibrated, dq, stats = calibrate_master_arrays(raw, dark, flat)
    np.testing.assert_allclose(calibrated, [[90.0, 200.0], [50.0, 110.0]])
    assert calibrated.dtype == np.float32
    assert not np.any(dq)
    assert dq.dtype == np.uint8
    assert stats["n_pixels"] == 4


def test_calibrate_master_arrays_masks_invalid_flat_and_flags_saturation() -> None:
    raw = np.array([[65535.0, 10.0], [20.0, 30.0]])
    dark = np.zeros((2, 2))
    flat = np.array([[1.0, 0.0], [np.nan, 1.0]])
    calibrated, dq, stats = calibrate_master_arrays(raw, dark, flat, saturation_level=65535.0)
    assert calibrated[0, 0] == pytest.approx(65535.0)
    assert np.isnan(calibrated[0, 1])
    assert np.isnan(calibrated[1, 0])
    assert dq[0, 0] & DQ_RAW_SATURATED
    assert dq[0, 1] & DQ_FLAT_INVALID
    assert dq[0, 1] & DQ_CALIBRATED_INVALID
    assert stats["n_invalid_flat"] == 2
    assert stats["n_saturated"] == 1



def test_wcs_keyword_detection_removes_raw_linear_and_sip_cards() -> None:
    for key in ("CTYPE1", "CRPIX2", "CRVAL1", "CDELT1", "CROTA2", "CUNIT1", "CD1_1", "PC2_2", "A_ORDER", "A_1_2", "BP_2_0"):
        assert _is_wcs_keyword(key)
    assert not _is_wcs_keyword("DATE-OBS")
    assert not _is_wcs_keyword("ID_CAM")

def test_physical_saturation_level_supports_fits_unsigned_short_convention() -> None:
    assert physical_saturation_level({"BITPIX": 16, "BZERO": 32768, "BSCALE": 1}) == 65535.0


def test_input_validation_accepts_real_master_contract(tmp_path: Path) -> None:
    raw_header, dark_header, flat_header, master_header = _headers()
    delta = validate_master_reduction_inputs(
        task=_task(tmp_path),
        raw_data=np.zeros((2, 2)),
        dark_data=np.zeros((2, 2)),
        flat_data=np.ones((2, 2)),
        raw_header=raw_header,
        dark_header=dark_header,
        flat_header=flat_header,
        master_header=master_header,
    )
    assert delta == pytest.approx(0.113)


def test_input_validation_refuses_dark_exposure_scaling(tmp_path: Path) -> None:
    raw_header, dark_header, flat_header, master_header = _headers()
    dark_header["EXPTIME"] = 25.0
    with pytest.raises(ValueError, match="automatic dark scaling is intentionally disabled"):
        validate_master_reduction_inputs(
            task=_task(tmp_path),
            raw_data=np.zeros((2, 2)),
            dark_data=np.zeros((2, 2)),
            flat_data=np.ones((2, 2)),
            raw_header=raw_header,
            dark_header=dark_header,
            flat_header=flat_header,
            master_header=master_header,
        )


def test_input_validation_refuses_flat_filter_mismatch(tmp_path: Path) -> None:
    raw_header, dark_header, flat_header, master_header = _headers()
    flat_header["FILTER"] = "R"
    with pytest.raises(ValueError, match="flat FILTER mismatch"):
        validate_master_reduction_inputs(
            task=_task(tmp_path),
            raw_data=np.zeros((2, 2)),
            dark_data=np.zeros((2, 2)),
            flat_data=np.ones((2, 2)),
            raw_header=raw_header,
            dark_header=dark_header,
            flat_header=flat_header,
            master_header=master_header,
        )


def test_reduction_metrics_from_header_requires_complete_metric_set() -> None:
    header = {
        "GGNPIX": 100,
        "GGNIFLAT": 2,
        "GGNSAT": 3,
        "GGMEDADU": 73.25,
        "GGDTEMP": 0.2,
    }
    metrics = reduction_metrics_from_header(header)
    assert metrics == {
        "n_pixels": 100,
        "n_invalid_flat": 2,
        "n_saturated": 3,
        "calibrated_median_adu": 73.25,
        "dark_temperature_delta_c": 0.2,
    }
    del header["GGNSAT"]
    assert reduction_metrics_from_header(header) is None


def test_skip_existing_reads_header_metrics_without_opening_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task = _task(tmp_path)
    task.output_path.parent.mkdir(parents=True, exist_ok=True)
    task.output_path.write_bytes(b"existing")

    header = {
        "GGSTAGE": "1B",
        "GGALG": "MASTER_DARK_FLAT_V1",
        "GGRAW": task.raw_remote_path,
        "GGHDR": task.header_remote_path,
        "DARK": task.dark_remote_path,
        "FLAT": task.flat_remote_path,
        "GGNPIX": 16793604,
        "GGNIFLAT": 0,
        "GGNSAT": 359,
        "GGMEDADU": 73.743469,
        "GGDTEMP": 0.113,
    }

    class FakeFits:
        @staticmethod
        def getheader(path, ext=0):
            assert Path(path) == task.output_path
            assert ext == 0
            return header

        @staticmethod
        def open(*args, **kwargs):
            raise AssertionError("skip path must not read image HDUs")

    monkeypatch.setattr(master_reduction, "_require_astropy_fits", lambda: FakeFits)
    result = reduce_master_task(task)
    assert result.status == "SKIPPED_EXISTING"
    assert result.n_pixels == 16793604
    assert result.n_invalid_flat == 0
    assert result.n_saturated == 359
    assert result.calibrated_median_adu == pytest.approx(73.743469)
    assert result.dark_temperature_delta_c == pytest.approx(0.113)


def test_skip_existing_legacy_output_stays_skip_without_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = _task(tmp_path)
    task.output_path.parent.mkdir(parents=True, exist_ok=True)
    task.output_path.write_bytes(b"existing")

    class FakeFits:
        @staticmethod
        def getheader(path, ext=0):
            return {
                "GGSTAGE": "1B",
                "GGALG": "MASTER_DARK_FLAT_V1",
                "GGRAW": task.raw_remote_path,
                "GGHDR": task.header_remote_path,
                "DARK": task.dark_remote_path,
                "FLAT": task.flat_remote_path,
            }

    monkeypatch.setattr(master_reduction, "_require_astropy_fits", lambda: FakeFits)
    result = reduce_master_task(task)
    assert result.status == "SKIPPED_EXISTING"
    assert result.calibrated_median_adu is None
    assert result.n_pixels is None


def test_skip_existing_rejects_provenance_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task = _task(tmp_path)
    task.output_path.parent.mkdir(parents=True, exist_ok=True)
    task.output_path.write_bytes(b"existing")

    class FakeFits:
        @staticmethod
        def getheader(path, ext=0):
            return {
                "GGSTAGE": "1B",
                "GGALG": "MASTER_DARK_FLAT_V1",
                "GGRAW": "/master/imdata/rawdata/2018/09/07/999999.fit.gz",
                "GGHDR": task.header_remote_path,
                "DARK": task.dark_remote_path,
                "FLAT": task.flat_remote_path,
            }

    monkeypatch.setattr(master_reduction, "_require_astropy_fits", lambda: FakeFits)
    result = reduce_master_task(task)
    assert result.status == "FAILED"
    assert "GGRAW mismatch" in str(result.error)



def test_compact_existing_disables_memmap_for_legacy_uint16_dq(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task = _task(tmp_path)
    task.output_path.parent.mkdir(parents=True, exist_ok=True)
    task.output_path.write_bytes(b"legacy-processed")

    primary_header = {
        "GGSTAGE": "1B",
        "GGALG": "MASTER_DARK_FLAT_V1",
        "GGRAW": task.raw_remote_path,
        "GGHDR": task.header_remote_path,
        "DARK": task.dark_remote_path,
        "FLAT": task.flat_remote_path,
        "GGNPIX": 4,
        "GGNIFLAT": 0,
        "GGNSAT": 1,
        "GGMEDADU": 73.0,
        "GGDTEMP": 0.1,
        "GGDQSCH": "MASTER_DQ_V1",
        "GGDQDT": "UINT16",
    }

    class FakeHDU:
        def __init__(self, data=None, header=None, name=""):
            self.data = data
            self.header = dict(header or {})
            self.name = name

    class OpenedHDUList(list):
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class OutputHDUList(list):
        def writeto(self, path, **kwargs):
            Path(path).write_bytes(b"compacted")

    class FakeFits:
        @staticmethod
        def getheader(path, ext=0):
            assert Path(path) == task.output_path
            return dict(primary_header)

        @staticmethod
        def open(path, **kwargs):
            assert Path(path) == task.output_path
            assert kwargs.get("memmap") is False
            assert kwargs.get("uint") is True
            return OpenedHDUList([
                FakeHDU(data=np.ones((2, 2), dtype=np.float32), header=primary_header),
                FakeHDU(data=np.array([[0, 1], [0, 8]], dtype=np.uint16), header={"BZERO": 32768, "BSCALE": 1}, name="DQ"),
            ])

        @staticmethod
        def PrimaryHDU(data=None, header=None):
            return FakeHDU(data=data, header=header)

        @staticmethod
        def ImageHDU(data=None, header=None, name=""):
            return FakeHDU(data=data, header=header, name=name)

        HDUList = OutputHDUList

    monkeypatch.setattr(master_reduction, "_require_astropy_fits", lambda: FakeFits)
    result = compact_existing_master_output(task)
    assert result.status == "COMPACTED_EXISTING"
    assert task.output_path.read_bytes() == b"compacted"


def test_reduce_master_task_end_to_end_with_astropy(tmp_path: Path) -> None:
    fits = pytest.importorskip("astropy.io.fits")
    task = _task(tmp_path)
    task.raw_local_path.parent.mkdir(parents=True, exist_ok=True)

    raw_header, dark_header, flat_header, master_header = _headers()
    master_header.update(
        {
            "CTYPE1": "RA---TAN-SIP",
            "CTYPE2": "DEC--TAN-SIP",
            "CRVAL1": 327.52,
            "CRVAL2": 72.18,
            "CRPIX1": 1.5,
            "CRPIX2": 1.5,
            "CD1_1": -0.0005,
            "CD1_2": 0.0,
            "CD2_1": 0.0,
            "CD2_2": 0.0005,
            "DATE": "2019-07-05T10:13:30",
        }
    )

    fits.PrimaryHDU(data=np.array([[100, 110], [120, 130]], dtype=np.uint16), header=fits.Header(raw_header)).writeto(task.raw_local_path)
    fits.PrimaryHDU(data=np.array([[10, 10], [20, 20]], dtype=np.uint16), header=fits.Header(dark_header)).writeto(task.dark_local_path)
    fits.PrimaryHDU(data=np.array([[1.0, 0.5], [2.0, 1.0]], dtype=np.float32), header=fits.Header(flat_header)).writeto(task.flat_local_path)
    fits.PrimaryHDU(header=fits.Header(master_header)).writeto(task.header_local_path)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = reduce_master_task(task)
    assert result.status == "REDUCED"
    assert not any("Card is too long" in str(item.message) for item in caught)
    with fits.open(task.output_path) as hdul:
        np.testing.assert_allclose(hdul[0].data, [[90.0, 200.0], [50.0, 110.0]])
        assert hdul[0].data.dtype.kind == "f"
        assert hdul[0].header["CTYPE1"] == "RA---TAN-SIP"
        assert hdul[0].header["ALPHA"] == "21 50 25"
        assert hdul[0].header["DELTA"] == "+72 11 00"
        assert hdul[0].header["GGHDR"] == task.header_remote_path
        assert hdul[0].header["GGMSMETA"] is True
        assert hdul[0].header["GGALG"] == "MASTER_DARK_FLAT_V1"
        assert hdul[0].header["GGDRKSC"] is False
        assert hdul[0].header["GGFLNRM"] is False
        assert hdul[0].header["GGNPIX"] == 4
        assert hdul[0].header["GGNIFLAT"] == 0
        assert hdul[0].header["GGNSAT"] == 0
        assert hdul[0].header["GGMEDADU"] == pytest.approx(100.0)
        assert hdul[0].header["GGDTEMP"] == pytest.approx(0.113)
        assert hdul[0].header["GGDQSCH"] == "MASTER_DQ_V2"
        assert hdul[0].header["GGDQDT"] == "UINT8"
        assert hdul[0].header.comments["DARK"] == ""
        assert hdul[0].header.comments["FLAT"] == ""
        assert hdul[0].header.comments["GGRAW"] == ""
        assert hdul[0].header.comments["GGHDR"] == ""
        assert hdul[0].header.comments["GGMSMETA"] == "MASTER metadata copied"
        assert hdul[1].name == "DQ"
        assert hdul[1].data.dtype == np.dtype(np.uint8)

    skipped = reduce_master_task(task)
    assert skipped.status == "SKIPPED_EXISTING"
    assert skipped.n_pixels == result.n_pixels
    assert skipped.n_invalid_flat == result.n_invalid_flat
    assert skipped.n_saturated == result.n_saturated
    assert skipped.calibrated_median_adu == pytest.approx(result.calibrated_median_adu)
    assert skipped.dark_temperature_delta_c == pytest.approx(result.dark_temperature_delta_c)
