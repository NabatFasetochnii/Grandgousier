from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

from grandgousier.calibration.preliminary import (
    LightcurveRow,
    lightcurve_rows_to_dicts,
    load_lightcurve_rows,
)
from grandgousier.lightcurves import candidates, global_object
from grandgousier.visualization import lightcurve_plot


class _RecordingAxes:
    def __init__(self):
        self.title = None
        self.title_pad = None
        self.text_calls = []
        self.transAxes = object()

    def set_title(self, title, **kwargs):
        self.title = title
        self.title_pad = kwargs.get("pad")

    def text(self, *args, **kwargs):
        self.text_calls.append((args, kwargs))

    def set_xlabel(self, *_args, **_kwargs):
        pass

    def set_ylabel(self, *_args, **_kwargs):
        pass

    def grid(self, *_args, **_kwargs):
        pass

    def plot(self, *_args, **_kwargs):
        pass

    def errorbar(self, *_args, **_kwargs):
        pass

    def invert_yaxis(self):
        pass

    def legend(self, *_args, **_kwargs):
        pass

    def vlines(self, *_args, **_kwargs):
        pass

    def axvline(self, *_args, **_kwargs):
        pass

    def get_xaxis_transform(self):
        return object()


class _RecordingFigure:
    def __init__(self):
        self.suptitle_text = None
        self.tight_layout_kwargs = None
        self.saved = False

    def suptitle(self, title, **_kwargs):
        self.suptitle_text = title

    def tight_layout(self, **kwargs):
        self.tight_layout_kwargs = kwargs

    def savefig(self, *_args, **_kwargs):
        self.saved = True


def _install_fake_matplotlib(monkeypatch):
    figure = _RecordingFigure()
    axes = _RecordingAxes()
    matplotlib = ModuleType("matplotlib")
    matplotlib.use = lambda *_args, **_kwargs: None
    pyplot = ModuleType("matplotlib.pyplot")
    pyplot.subplots = lambda **_kwargs: (figure, axes)
    pyplot.close = lambda *_args, **_kwargs: None
    matplotlib.pyplot = pyplot
    monkeypatch.setitem(sys.modules, "matplotlib", matplotlib)
    monkeypatch.setitem(sys.modules, "matplotlib.pyplot", pyplot)
    return figure, axes


def _expected_anchor() -> str:
    return (
        "Stored coordinates (deg): RA=12.345679, Dec=-45.678912"
        " | object_uid=internal-uid-full"
        " | source_id=Gaia DR3:702296666944246784"
    )


class _MappingsResult:
    def __init__(self, rows):
        self.rows = list(rows)

    def mappings(self):
        return self

    def all(self):
        return list(self.rows)

    def first(self):
        return self.rows[0] if self.rows else None


def test_lightcurve_row_keeps_stored_object_identity_without_changing_csv_fields():
    measurement = SimpleNamespace(
        measurement_id=10,
        object_id=42,
        frame_id=7,
        flags={"catalog_name": "Gaia DR3", "source_id": "702296666944246784"},
        x=1.0,
        y=2.0,
        flux=100.0,
        flux_err=1.0,
        mag_inst=12.0,
        mag_cal=11.9,
        mag_err=0.01,
        local_zp_correction=0.0,
    )
    frame = SimpleNamespace(
        frame_id=7,
        date_obs=None,
        mjd=60000.0,
        instrument_unit_id=3,
    )
    obj = SimpleNamespace(
        object_id=42,
        object_uid="internal-uid-full",
        ra_deg=12.3456789,
        dec_deg=-45.6789123,
    )
    calibration = SimpleNamespace(name="diagnostic")
    instrument = SimpleNamespace(filter_name="R")

    class FakeSession:
        def execute(self, _statement):
            return SimpleNamespace(all=lambda: [(measurement, frame, obj, calibration, instrument)])

    rows = load_lightcurve_rows(FakeSession(), object_id=42)

    assert len(rows) == 1
    assert rows[0].object_uid == "internal-uid-full"
    assert rows[0].ra_deg == 12.3456789
    assert rows[0].dec_deg == -45.6789123
    assert "object_uid" not in lightcurve_rows_to_dicts(rows)[0]
    assert "ra_deg" not in lightcurve_rows_to_dicts(rows)[0]
    assert "dec_deg" not in lightcurve_rows_to_dicts(rows)[0]
    assert "object_uid" not in lightcurve_plot._default_title(rows)


def test_generic_plot_command_passes_shared_identity_subtitle(monkeypatch, capsys):
    from grandgousier import cli
    from grandgousier.calibration import preliminary

    rows = [
        LightcurveRow(
            measurement_id=10,
            frame_id=7,
            object_id=42,
            source_id="702296666944246784",
            catalog_name="Gaia DR3",
            date_obs=None,
            mjd=60000.0,
            filter_name="R",
            x=1.0,
            y=2.0,
            flux=100.0,
            flux_err=1.0,
            mag_inst=12.0,
            mag_cal=11.9,
            mag_err=0.01,
            local_zp_correction=0.0,
            flags={},
            object_uid="internal-uid-full",
            ra_deg=12.3456789,
            dec_deg=-45.6789123,
        )
    ]
    monkeypatch.setattr(preliminary, "load_lightcurve_rows", lambda *_args, **_kwargs: rows)

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(cli, "make_session_factory", lambda _url: FakeSession)
    seen = {}

    def fake_plot(plot_rows, path, **kwargs):
        seen.update(rows=plot_rows, path=path, kwargs=kwargs)
        return SimpleNamespace(
            output_path=str(path), n_rows=1, n_points=1, x_key="mjd_zero", y_key="mag_cal",
            inverted_y=True, x_offset=60000.0, time_warning=None,
        )

    monkeypatch.setattr(lightcurve_plot, "export_lightcurve_plot", fake_plot)
    args = SimpleNamespace(
        source_id="702296666944246784", object_id=None, database_url="fake",
        calibration_name="diagnostic", catalog_name="Gaia DR3", instrument_unit_id=None,
        limit=None, out="not-written.png", x="mjd_zero", y="mag_cal", title="Custom title",
        dpi=150, no_invert_y=False, no_errors=False, show_stats=False, observatory="KAO",
        observatory_lat_deg=None, observatory_lon_deg=None, observatory_height_m=None,
    )

    assert cli.cmd_export_lightcurve_plot(args) == 0
    capsys.readouterr()
    assert seen["rows"] is rows
    assert seen["kwargs"]["title"] == "Custom title"
    assert seen["kwargs"]["subtitle"] == _expected_anchor()


def test_generic_shared_plot_api_renders_subtitle_beneath_existing_title(monkeypatch, tmp_path):
    figure, axes = _install_fake_matplotlib(monkeypatch)
    row = SimpleNamespace(
        mjd=60000.0, mag_cal=12.0, mag_err=0.01, source_id="source", catalog_name="catalog",
        object_id=42, filter_name="R", calibration_name="ens", frame_id=7, measurement_id=10,
        flux=None, flux_err=None, mag_inst=None, local_zp_correction=None, x=None, y=None,
        ra_deg=12.3456789, dec_deg=-45.6789123,
    )

    lightcurve_plot.export_lightcurve_plot(
        [row],
        tmp_path / "not-rendered.png",
        title="Existing generic title",
        subtitle=_expected_anchor(),
    )

    assert figure.suptitle_text == "Existing generic title"
    assert axes.title == _expected_anchor()
    assert axes.title_pad is not None
    assert figure.tight_layout_kwargs["rect"] == (0.0, 0.0, 1.0, 0.9)
    assert figure.saved is True


def test_m5_export_uses_filter_specific_catalog_source_for_plot_subtitles(monkeypatch, tmp_path):
    generation_r = global_object.M5Generation(
        interseries_generation_id=1,
        generation_uid="m4_r",
        field_id=33,
        instrument_unit_id=8,
        filter_name="R",
        catalog_name="Gaia DR3",
        reference_series_id=77,
        reliability_status="REDUNDANT_GRAPH_OK",
        graph_dof=1,
        series_sources=((77, 967),),
    )
    generation_v = global_object.M5Generation(
        interseries_generation_id=2,
        generation_uid="m4_v",
        field_id=33,
        instrument_unit_id=8,
        filter_name="V",
        catalog_name="Pan-STARRS",
        reference_series_id=78,
        reliability_status="REDUNDANT_GRAPH_OK",
        graph_dof=1,
        series_sources=((78, 968),),
    )
    monkeypatch.setattr(
        global_object,
        "resolve_m5_generations",
        lambda *_args, **_kwargs: [generation_r, generation_v],
    )
    monkeypatch.setattr(
        global_object, "inspect_m5_series_coverage",
        lambda *_args, **_kwargs: [
            global_object.SeriesCoverage(
                77, "R", 967, 967, 1, "READY_EXACT_M4_SOURCE"
            ),
            global_object.SeriesCoverage(
                78, "V", 968, 968, 1, "READY_EXACT_M4_SOURCE"
            ),
        ],
    )
    monkeypatch.setattr(
        global_object, "_load_plot_source_stats",
        lambda *_args, **_kwargs: [
            {
                "filter_name": "R",
                "series_id": 77,
                "n_points": 1,
                "mjd_min": 60000.0,
                "mjd_max": 60000.0,
            },
            {
                "filter_name": "V",
                "series_id": 78,
                "n_points": 1,
                "mjd_min": 60001.0,
                "mjd_max": 60001.0,
            },
        ],
    )
    monkeypatch.setattr(global_object, "_to_m5_row", lambda row, **_kwargs: dict(row))
    monkeypatch.setattr(
        global_object, "_iter_exact_m4_rows",
        lambda *_args, **_kwargs: iter(
            [
                {
                    "object_id": 42,
                    "catalog_name": "Gaia DR3",
                    "source_id": "702296666944246784",
                    "filter_name": "R",
                    "is_rejected": False,
                    "reject_reasons": None,
                    "series_id": 77,
                    "mjd": 60000.0,
                    "date_obs": None,
                    "mag_global": 12.0,
                    "mag_err_random": 0.02,
                    "mag_err_series_zp": 0.003,
                    "interseries_generation_id": 1,
                    "interseries_reliability": "REDUNDANT_GRAPH_OK",
                },
                {
                    "object_id": 42,
                    "catalog_name": "Pan-STARRS",
                    "source_id": "PS-42",
                    "filter_name": "V",
                    "is_rejected": False,
                    "reject_reasons": None,
                    "series_id": 78,
                    "mjd": 60001.0,
                    "date_obs": None,
                    "mag_global": 12.1,
                    "mag_err_random": 0.02,
                    "mag_err_series_zp": 0.003,
                    "interseries_generation_id": 2,
                    "interseries_reliability": "REDUNDANT_GRAPH_OK",
                },
            ]
        ),
    )

    class FakeSession:
        def __init__(self):
            self.calls = 0

        def execute(self, statement, _params):
            self.calls += 1
            if self.calls == 1:
                return _MappingsResult([{"field_id": 33, "field_name": "field"}])
            if self.calls == 2:
                assert "object_uid, ra_deg, dec_deg" in str(statement)
                return _MappingsResult([{
                    "object_id": 42, "object_uid": "internal-uid-full",
                    "ra_deg": 12.3456789, "dec_deg": -45.6789123,
                }])
            raise AssertionError("unexpected database query")

    seen = {}

    def fake_plot(rows, path, **kwargs):
        seen[rows[0]["filter_name"]] = {"rows": rows, "kwargs": kwargs}
        path.write_bytes(b"synthetic plot placeholder")
        return SimpleNamespace(
            n_points=len(rows), time_axis_mode="continuous", n_time_breaks=0,
            plot_style="series_detailed",
        )

    monkeypatch.setattr(global_object, "export_global_lightcurve_plot", fake_plot)
    global_object.export_global_object_light_curve(
        FakeSession(), object_id=42, field_id=33, out_root=tmp_path / "m5", plots=True,
    )

    assert seen["R"]["kwargs"]["title"] == "Object 42 - R; M4 REDUNDANT_GRAPH_OK"
    assert seen["R"]["kwargs"]["subtitle"] == _expected_anchor()
    assert seen["V"]["kwargs"]["subtitle"] == (
        "Stored coordinates (deg): RA=12.345679, Dec=-45.678912"
        " | object_uid=internal-uid-full"
        " | source_id=Pan-STARRS:PS-42"
    )


def test_global_shared_plot_api_renders_optional_subtitle(monkeypatch, tmp_path):
    figure, axes = _install_fake_matplotlib(monkeypatch)
    lightcurve_plot.export_global_lightcurve_plot(
        [{"mjd": 60000.0, "mag_global": 12.0, "series_id": 1}],
        tmp_path / "not-rendered.png",
        title="Existing M5 title",
        subtitle=_expected_anchor(),
    )

    assert figure.suptitle_text == "Existing M5 title"
    assert axes.title == _expected_anchor()
    assert figure.tight_layout_kwargs["rect"] == (0.0, 0.0, 1.0, 0.9)


def test_candidate_plot_subtitle_uses_catalog_source_and_available_coordinate_fallback(
    monkeypatch, tmp_path
):
    figure, axes = _install_fake_matplotlib(monkeypatch)
    candidates._export_candidate_light_curve_plot(
        [{"mjd": 60000.0, "mag_cal": 12.0, "mag_err": 0.01, "is_rejected": False}],
        tmp_path / "not-rendered.png",
        candidate_row={
            "object_id": 42,
            "source_id": "702296666944246784",
            "catalog_name": "Gaia DR3",
            "ra_deg": 12.3456789,
            "dec_deg": None,
            "candidate_origin": "intrinsic_variability",
        },
        light_curve_summary={"rows_total": 1, "rows_accepted": 1},
    )

    assert figure.suptitle_text == "object 42 / 702296666944246784 / intrinsic_variability"
    assert axes.title == (
        "Catalog coordinates (deg): RA=12.345679"
        " | source_id=Gaia DR3:702296666944246784"
    )
    assert "object_uid" not in axes.title
    assert "Dec=" not in axes.title


def test_identity_subtitle_keeps_remaining_anchors_when_coordinates_are_missing():
    subtitle = lightcurve_plot.format_lightcurve_identity_subtitle(
        object_uid="portable-full-uid",
        ra_deg=None,
        dec_deg=None,
        catalog_name="Gaia DR3",
        source_id="source-7",
    )

    assert subtitle == "object_uid=portable-full-uid | source_id=Gaia DR3:source-7"


def test_identity_subtitle_formats_positive_declination_with_explicit_plus_sign():
    subtitle = lightcurve_plot.format_lightcurve_identity_subtitle(dec_deg=23.456789)

    assert subtitle == "Stored coordinates (deg): Dec=+23.456789"


def test_m5_plot_anchor_omits_ambiguous_source_pairs():
    catalog_name, source_id = global_object._unique_m5_plot_source_pair(
        {("Gaia DR3", "source-1"), ("Gaia DR3", "source-2")}
    )
    subtitle = lightcurve_plot.format_lightcurve_identity_subtitle(
        object_uid="internal-uid-full",
        ra_deg=12.3456789,
        dec_deg=-45.6789123,
        catalog_name=catalog_name,
        source_id=source_id,
    )

    assert subtitle == (
        "Stored coordinates (deg): RA=12.345679, Dec=-45.678912"
        " | object_uid=internal-uid-full"
    )
