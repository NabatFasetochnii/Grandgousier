from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import re
import struct
import sys
from types import ModuleType

import numpy as np
import pytest

from grandgousier.catalogs import gaia_query as gq


class _FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        content_type: str = "text/csv",
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ):
        self.content = body
        self.encoding = "utf-8"
        self.headers = headers or {"Content-Type": content_type}
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise gq.requests.HTTPError(f"HTTP {self.status_code}")


_CSV_SUCCESS = (
    b"source_id,ra,dec,phot_g_mean_mag\n"
    b"6917529027641081856,123.5,-22.25,15.1\n"
)
_VOTABLE_OK = b'''<?xml version="1.0" encoding="UTF-8"?>
<VOTABLE xmlns="http://www.ivoa.net/xml/VOTable/v1.3" version="1.3">
  <RESOURCE type="results">
    <INFO name="QUERY_STATUS" value="OK"/>
    <TABLE>
      <FIELD name="source_id" datatype="long"/>
      <FIELD name="ra" datatype="double"/>
      <FIELD name="dec" datatype="double"/>
      <FIELD name="phot_g_mean_mag" datatype="double"/>
      <DATA><TABLEDATA>
        <TR><TD>6917529027641081856</TD><TD>123.5</TD><TD>-22.25</TD><TD>15.1</TD></TR>
      </TABLEDATA></DATA>
    </TABLE>
    <INFO name="QUERY_STATUS" value="OK"/>
  </RESOURCE>
</VOTABLE>'''
_VOTABLE_BINARY_DATA = base64.b64encode(
    struct.pack(">qddd", 6917529027641081856, 123.5, -22.25, 15.1)
).decode("ascii")
_VOTABLE_BINARY = re.sub(
    rb"<DATA><TABLEDATA>.*?</TABLEDATA></DATA>",
    (
        "<DATA><BINARY><STREAM encoding='base64'>"
        f"{_VOTABLE_BINARY_DATA}</STREAM></BINARY></DATA>"
    ).encode("ascii"),
    _VOTABLE_OK,
    flags=re.DOTALL,
)
_VOTABLE_OVERFLOW = _VOTABLE_OK.replace(
    b'<INFO name="QUERY_STATUS" value="OK"/>',
    b'<INFO name="QUERY_STATUS" value="OVERFLOW">TAP row limit</INFO>',
    1,
)
_VOTABLE_MAXREC = _VOTABLE_OK.replace(
    b'<INFO name="QUERY_STATUS" value="OK"/>',
    b'<INFO name="QUERY_STATUS" value="MAXREC">TAP row limit</INFO>',
    1,
)


def _fake_post(monkeypatch, response_or_responses, seen=None):
    responses = list(response_or_responses) if isinstance(response_or_responses, list) else [response_or_responses]

    def fake_post(url, *, data, headers, timeout):
        if seen is not None:
            seen.append({"url": url, "data": data, "headers": headers, "timeout": timeout})
        if not responses:
            raise AssertionError("unexpected extra HTTP attempt")
        return responses.pop(0)

    monkeypatch.setattr(gq.requests, "post", fake_post)


def test_tap_csv_keeps_gaia_source_id_as_exact_text_and_sniffs_body(monkeypatch):
    seen = []
    _fake_post(monkeypatch, _FakeResponse(_CSV_SUCCESS, content_type="text/plain"), seen)
    client = gq._HttpCsvTapClient("https://example.invalid/tap", timeout_sec=17)
    job = client.launch_job("SELECT * FROM gaiadr3.gaia_source", maxrec=10)
    rows = job.get_results()

    assert seen[0]["url"] == "https://example.invalid/tap/sync"
    assert seen[0]["data"]["FORMAT"] == "csv"
    assert seen[0]["data"]["MAXREC"] == "10"
    assert seen[0]["headers"]["Accept"].startswith("text/csv")
    assert seen[0]["timeout"] == 17.0
    assert rows[0]["source_id"] == "6917529027641081856"
    assert rows[0]["ra"] == 123.5
    assert job.response.format == "csv"
    assert job.response.complete is True


def test_aip_votable_ok_keeps_64_bit_source_id_as_text(monkeypatch):
    _fake_post(monkeypatch, _FakeResponse(_VOTABLE_OK, content_type="text/plain"))
    job = gq._HttpCsvTapClient("https://example.invalid/tap").launch_job("SELECT ...")

    rows = job.get_results()
    assert job.response.format == "votable"
    assert job.response.query_status == "OK"
    assert job.response.complete is True
    assert rows[0]["source_id"] == "6917529027641081856"
    assert rows[0]["ra"] == 123.5


def test_votable_binary_keeps_64_bit_source_id_as_text():
    response = gq._parse_tap_response(
        _VOTABLE_BINARY,
        content_type="application/x-votable+xml",
        encoding="utf-8",
    )

    assert response.complete is True
    assert response.rows[0]["source_id"] == "6917529027641081856"


def test_votable_query_status_error_is_informative_without_body_or_secret(monkeypatch):
    body = b'''<?xml version="1.0"?>
    <VOTABLE><RESOURCE><INFO name="QUERY_STATUS" value="ERROR">
      Invalid ADQL near source_id; token=do-not-leak; authorization=Bearer also-do-not-leak
    </INFO></RESOURCE><SECRET>full-body-marker</SECRET></VOTABLE>'''
    _fake_post(monkeypatch, _FakeResponse(body, content_type="application/x-votable+xml"))

    with pytest.raises(gq._TapQueryStatusError) as excinfo:
        gq._HttpCsvTapClient("https://example.invalid/tap").launch_job("SELECT ...")

    message = str(excinfo.value)
    assert "QUERY_STATUS=ERROR" in message
    assert "Invalid ADQL" in message
    assert "do-not-leak" not in message
    assert "also-do-not-leak" not in message
    assert "full-body-marker" not in message
    assert "<VOTABLE" not in message


def test_votable_overflow_is_incomplete_and_provider_falls_back(monkeypatch):
    seen = []
    _fake_post(
        monkeypatch,
        [
            _FakeResponse(_VOTABLE_OVERFLOW, content_type="application/x-votable+xml"),
            _FakeResponse(_CSV_SUCCESS, content_type="text/csv"),
        ],
        seen,
    )
    providers = [
        gq._GaiaTapProvider("first", gq._HttpCsvTapClient("https://first.invalid/tap")),
        gq._GaiaTapProvider("second", gq._HttpCsvTapClient("https://second.invalid/tap")),
    ]

    rows, provider_name, failures = gq._query_gaia_cone(
        providers,
        center_ra_deg=10.0,
        center_dec_deg=20.0,
        radius_deg=0.25,
        mag_limit=19.0,
        max_rows=2000,
    )

    assert [attempt["url"] for attempt in seen] == [
        "https://first.invalid/tap/sync",
        "https://second.invalid/tap/sync",
    ]
    assert provider_name == "second"
    assert failures["first"].startswith("_TapIncompleteResponseError:")
    assert rows[0]["source_id"] == "6917529027641081856"


def test_votable_overflow_exposes_completeness_without_exposing_partial_rows():
    response = gq._parse_tap_response(
        _VOTABLE_OVERFLOW,
        content_type="application/x-votable+xml",
        encoding="utf-8",
    )

    assert response.format == "votable"
    assert response.query_status == "OVERFLOW"
    assert response.complete is False
    with pytest.raises(gq._TapIncompleteResponseError, match="incomplete"):
        gq._StaticTapJob(response).get_results()


def test_votable_maxrec_status_is_incomplete():
    response = gq._parse_tap_response(
        _VOTABLE_MAXREC,
        content_type="application/x-votable+xml",
        encoding="utf-8",
    )

    assert response.complete is False
    assert response.query_status == "MAXREC"


def test_csv_reaching_transport_cap_is_not_treated_as_complete(monkeypatch):
    _fake_post(monkeypatch, _FakeResponse(_CSV_SUCCESS, content_type="text/csv"))
    job = gq._HttpCsvTapClient("https://example.invalid/tap").launch_job(
        "SELECT ...", maxrec=1
    )

    assert job.response.complete is False
    assert job.response.query_status == "MAXREC_REACHED"
    with pytest.raises(gq._TapIncompleteResponseError, match="MAXREC_REACHED"):
        job.get_results()


@pytest.mark.parametrize(
    ("body", "content_type", "message"),
    [
        (b"<VOTABLE><RESOURCE>", "application/xml", "malformed"),
        (b"<?xml version='1.0'?><ProxyError>marker</ProxyError>", "text/plain", "not a VOTable"),
        (b"<!doctype html><html><body>proxy marker</body></html>", "text/html", "HTML"),
        (b"proxy error marker", "text/plain", "unknown or unsupported"),
    ],
)
def test_unknown_html_and_malformed_responses_are_rejected_without_body(
    monkeypatch, body, content_type, message
):
    _fake_post(monkeypatch, _FakeResponse(body, content_type=content_type))

    with pytest.raises(RuntimeError, match=re.escape(message)) as excinfo:
        gq._HttpCsvTapClient("https://example.invalid/tap").launch_job("SELECT ...")

    assert "marker" not in str(excinfo.value)
    assert "<" not in str(excinfo.value)


def test_http_proxy_error_does_not_publish_response_body(monkeypatch):
    _fake_post(
        monkeypatch,
        _FakeResponse(
            b"proxy-secret-body-marker",
            content_type="text/html",
            status_code=502,
        ),
    )

    with pytest.raises(RuntimeError, match="status 502") as excinfo:
        gq._HttpCsvTapClient("https://example.invalid/tap").launch_job("SELECT ...")

    assert "proxy-secret-body-marker" not in str(excinfo.value)


def test_provider_failure_summary_does_not_echo_arbitrary_exception_body():
    diagnostic = gq._format_provider_failure(RuntimeError("proxy-secret-body-marker"))

    assert diagnostic == "RuntimeError: provider request or response failed"
    assert "proxy-secret-body-marker" not in diagnostic


def test_gaia_adql_selects_only_minimal_columns_without_hidden_limit_or_ordering():
    adql = gq._build_gaia_cone_adql(
        table_name="gaiadr3.gaia_source",
        center_ra_deg=10.0,
        center_dec_deg=20.0,
        radius_deg=0.25,
        mag_limit=19.0,
        max_rows=17,
    )
    select = re.search(r"SELECT(.*?)FROM", adql, flags=re.IGNORECASE | re.DOTALL)
    assert select is not None
    assert [column.strip().casefold() for column in select.group(1).split(",")] == [
        "source_id",
        "ra",
        "dec",
        "phot_g_mean_mag",
    ]
    lowered = adql.casefold()
    for unwanted in (
        "top",
        "order by",
        "ref_epoch",
        "pmra",
        "pmdec",
        "ruwe",
        "duplicated_source",
        "phot_bp_mean_mag",
        "phot_rp_mean_mag",
    ):
        assert unwanted not in lowered


def test_gaia_row_conversion_persists_only_minimal_g_source_state():
    row = gq._TapCsvRow(
        source_id="6917529027641081856",
        ra=123.5,
        dec=-22.25,
        phot_g_mean_mag=15.1,
        ref_epoch=1991.25,
        pmra=12.0,
        pmdec=-4.0,
        phot_bp_mean_mag=15.4,
        phot_rp_mean_mag=14.8,
        ruwe=1.03,
        duplicated_source=True,
    )

    source = gq._gaia_row_to_catalog_source(row, catalog_name="gaia_dr3")

    assert source["source_id"] == "6917529027641081856"
    assert source["ra_deg"] == 123.5
    assert source["dec_deg"] == -22.25
    assert source["epoch"] == 2016.0
    assert source["pmra_masyr"] is None
    assert source["pmdec_masyr"] is None
    assert source["ref_mags"] == {"G": 15.1}
    assert source["colors"] == {}
    assert source["source_flags"] == {}
    assert source["raw_json"] == {}


def test_provider_chain_uses_http_mirrors_before_esac(monkeypatch):
    class _FakeGaia:
        pass

    astroquery_module = ModuleType("astroquery")
    astroquery_module.__path__ = []
    gaia_module = ModuleType("astroquery.gaia")
    gaia_module.Gaia = _FakeGaia()
    astroquery_module.gaia = gaia_module
    monkeypatch.setitem(sys.modules, "astroquery", astroquery_module)
    monkeypatch.setitem(sys.modules, "astroquery.gaia", gaia_module)

    providers = gq._build_gaia_tap_providers(23)
    assert providers[0].name == "gaia_aip_csv"
    assert isinstance(providers[0].client, gq._HttpCsvTapClient)
    assert providers[1].name == "gaia_ari_csv"
    assert isinstance(providers[1].client, gq._HttpCsvTapClient)
    assert providers[-1].name == "gaia_esac"


def _working_row(source_id, ra, dec, g_mag):
    return gq._TapCsvRow(
        source_id=source_id,
        ra=ra,
        dec=dec,
        phot_g_mean_mag=g_mag,
    )


class _AsyncTapJob:
    def __init__(self, rows, *, query_status="OK", complete=True):
        self.response = gq._TapResponse(
            rows=list(rows),
            format="votable",
            complete=complete,
            query_status=query_status,
        )

    async def get_results(self):
        if not self.response.complete:
            raise gq._TapIncompleteResponseError(
                "TAP response is incomplete",
                query_status=self.response.query_status,
            )
        return self.response.rows


class _ScriptedAsyncTapClient:
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []

    async def launch_job_async(self, query, *, maxrec, dump_to_file, output_format):
        self.calls.append(
            {
                "query": query,
                "maxrec": maxrec,
                "dump_to_file": dump_to_file,
                "output_format": output_format,
            }
        )
        if not self.steps:
            raise AssertionError("unexpected extra async TAP query")
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    def launch_job(self, *args, **kwargs):
        raise AssertionError("new working-catalog API must use async TAP")

    def commit(self):
        raise AssertionError("working-catalog fetch must not commit")

    def execute(self, *args, **kwargs):
        raise AssertionError("working-catalog fetch must not execute SQL")


def _working_provider(client, name="fake"):
    return gq._GaiaTapProvider(name=name, client=client)


def test_gaia_working_catalog_is_compact_deduplicated_and_source_id_sorted():
    catalog = gq.GaiaWorkingCatalog(
        [
            _working_row("10", 20.0, -2.0, 15.0),
            _working_row("2", 30.0, 3.0, 14.0),
            _working_row("10", 19.0, -2.0, 15.0),
        ]
    )

    assert not dataclasses.is_dataclass(catalog)
    assert catalog.source_ids.dtype.kind == "U"
    assert catalog.source_ids.tolist() == ["2", "10"]
    assert catalog.ra_deg.tolist() == [30.0, 19.0]
    assert catalog.dec_deg.dtype == np.dtype(np.float64)
    assert catalog.g_mag.dtype == np.dtype(np.float64)
    assert not catalog.source_ids.flags.writeable
    assert not catalog.ra_deg.flags.writeable
    with pytest.raises(TypeError):
        json.dumps(catalog)


def test_gaia_working_catalog_nearest_match_is_radius_bounded_and_wrap_safe():
    catalog = gq.GaiaWorkingCatalog(
        [
            _working_row("1", 359.9, 0.0, 15.0),
            _working_row("2", 10.0, 0.0, 16.0),
        ]
    )

    indices, separations = catalog.match_nearest(
        np.array([0.1, 10.5]),
        np.array([0.0, 0.0]),
        radius_arcsec=1200.0,
    )

    assert indices.tolist() == [0, -1]
    assert separations[0] == pytest.approx(720.0, abs=1e-6)
    assert np.isinf(separations[1])
    with pytest.raises(ValueError, match="max_positions"):
        catalog.match_nearest([0.0, 1.0], [0.0, 0.0], radius_arcsec=1, max_positions=1)


def test_gaia_working_catalog_clear_and_context_manager_close_lifecycle():
    catalog = gq.GaiaWorkingCatalog([_working_row("1", 1.0, 2.0, 3.0)])
    catalog.clear()
    assert len(catalog) == 0
    assert catalog.source_ids.size == 0

    with gq.GaiaWorkingCatalog([_working_row("2", 4.0, 5.0, 6.0)]) as opened:
        assert len(opened) == 1

    with pytest.raises(RuntimeError, match="closed"):
        len(opened)
    with pytest.raises(RuntimeError, match="closed"):
        _ = opened.source_ids
    with pytest.raises(RuntimeError, match="closed"):
        opened.clear()


def test_fetch_gaia_working_catalog_is_read_only_json_safe_and_uses_async_tap(monkeypatch):
    def forbid_side_effect(*args, **kwargs):
        raise AssertionError("read-only Gaia working-catalog fetch attempted persistence")

    monkeypatch.setattr(gq.Session, "commit", forbid_side_effect)
    monkeypatch.setattr(gq.Session, "execute", forbid_side_effect)
    monkeypatch.setattr(gq, "_upsert_catalog_sources", forbid_side_effect)
    client = _ScriptedAsyncTapClient(
        [_AsyncTapJob([_working_row("6917529027641081856", 12.0, -3.0, 15.1)])]
    )
    catalog, diagnostics = asyncio.run(
        gq.fetch_gaia_working_catalog(
            [(12.0, -3.0, 0.5)],
            max_radius_deg=1.0,
            providers=[_working_provider(client)],
        )
    )

    assert catalog.source_ids.tolist() == ["6917529027641081856"]
    assert len(client.calls) == 1
    assert client.calls[0]["maxrec"] == 200_000
    assert client.calls[0]["dump_to_file"] is False
    assert client.calls[0]["output_format"] == "votable"
    assert "launch_job_async" in type(client).__dict__
    query = client.calls[0]["query"]
    assert "SELECT source_id, ra, dec, phot_g_mean_mag" in query
    assert "TOP" not in query.upper()
    assert diagnostics["logical_queries"] == 1
    assert diagnostics["physical_parts"] == 1
    assert diagnostics["rows_received"] == 1
    assert json.loads(json.dumps(diagnostics))["unique_sources"] == 1


def _spy_on_default_working_provider(monkeypatch, client):
    build_calls = []

    def build_provider(timeout_sec):
        build_calls.append(timeout_sec)
        return [_working_provider(client)]

    monkeypatch.setattr(gq, "_build_gaia_working_catalog_providers", build_provider)
    return build_calls


@pytest.mark.parametrize(
    ("cones", "max_radius_deg", "message"),
    [
        ([(12.0, -3.0, 1.01)], 1.0, "individual Gaia cone exceeds"),
        (
            [(0.0, 0.0, 0.25), (20.0, 0.0, 0.25)],
            5.0,
            "aggregate Gaia field footprint exceeds",
        ),
        (
            [(0.0, 0.0, 0.1), (180.0, 0.0, 0.1)],
            180.0,
            "geometry is indeterminate",
        ),
    ],
    ids=["oversized-individual", "disconnected-aggregate", "indeterminate-antipodal"],
)
def test_fetch_rejects_unsafe_field_geometry_before_provider_creation(
    monkeypatch, cones, max_radius_deg, message
):
    client = _ScriptedAsyncTapClient([])
    build_calls = _spy_on_default_working_provider(monkeypatch, client)

    with pytest.raises(ValueError, match=message):
        asyncio.run(
            gq.fetch_gaia_working_catalog(
                cones,
                max_radius_deg=max_radius_deg,
            )
        )

    assert build_calls == []
    assert client.calls == []


@pytest.mark.parametrize(
    ("max_radius_deg", "error_type"),
    [
        (0.0, ValueError),
        (-1.0, ValueError),
        (float("nan"), ValueError),
        (float("inf"), ValueError),
        (True, TypeError),
    ],
)
def test_fetch_rejects_nonpositive_or_nonfinite_radius_cap_before_provider_use(
    max_radius_deg, error_type
):
    client = _ScriptedAsyncTapClient([])

    with pytest.raises(error_type, match="max_radius_deg must be finite and positive"):
        asyncio.run(
            gq.fetch_gaia_working_catalog(
                [(12.0, -3.0, 0.5)],
                max_radius_deg=max_radius_deg,
                providers=[_working_provider(client)],
            )
        )

    assert client.calls == []


def test_fetch_requires_explicit_radius_cap():
    with pytest.raises(TypeError, match="max_radius_deg"):
        gq.fetch_gaia_working_catalog([(12.0, -3.0, 0.5)])


def test_http_async_tap_transport_submits_maxrec_and_reads_results_in_memory(monkeypatch):
    post_calls = []
    get_calls = []

    def fake_post(url, *, data, headers, timeout, allow_redirects):
        post_calls.append(
            {
                "url": url,
                "data": data,
                "headers": headers,
                "timeout": timeout,
                "allow_redirects": allow_redirects,
            }
        )
        return _FakeResponse(
            b"",
            status_code=303,
            headers={"Location": "https://tap.invalid/async/job-1"},
        )

    responses = [
        _FakeResponse(b"COMPLETED", content_type="text/plain"),
        _FakeResponse(_VOTABLE_OK, content_type="application/x-votable+xml"),
    ]

    def fake_get(url, *, headers, timeout):
        get_calls.append({"url": url, "headers": headers, "timeout": timeout})
        if not responses:
            raise AssertionError("unexpected extra async TAP GET")
        return responses.pop(0)

    monkeypatch.setattr(gq.requests, "post", fake_post)
    monkeypatch.setattr(gq.requests, "get", fake_get)
    job = gq._HttpAsyncTapClient("https://tap.invalid/tap").launch_job_async(
        "SELECT source_id, ra, dec, phot_g_mean_mag FROM gaiadr3.gaia_source",
        maxrec=123_456,
        dump_to_file=False,
        output_format="votable",
    )

    assert post_calls[0]["url"] == "https://tap.invalid/tap/async"
    assert post_calls[0]["data"]["PHASE"] == "RUN"
    assert post_calls[0]["data"]["MAXREC"] == "123456"
    assert post_calls[0]["data"]["FORMAT"] == "votable"
    assert post_calls[0]["allow_redirects"] is False
    assert [call["url"] for call in get_calls] == [
        "https://tap.invalid/async/job-1/phase",
        "https://tap.invalid/async/job-1/results/result",
    ]
    rows = job.get_results()
    assert rows[0]["source_id"] == "6917529027641081856"
    assert job.response.complete is True


@pytest.mark.parametrize("overflow_status", ["OVERFLOW", "MAXREC"])
def test_async_overflow_or_maxrec_splits_to_disjoint_tiles_and_deduplicates(overflow_status):
    client = _ScriptedAsyncTapClient(
        [_AsyncTapJob([], query_status=overflow_status, complete=False)]
        + [
            _AsyncTapJob([_working_row("7", 0.0, 0.0, 15.0)])
            for _ in range(4)
        ]
    )
    catalog, diagnostics = asyncio.run(
        gq.fetch_gaia_working_catalog(
            [(0.0, 0.0, 1.0)],
            max_radius_deg=1.0,
            providers=[_working_provider(client)],
            maxrec=1000,
        )
    )

    assert len(client.calls) == 5
    assert diagnostics["logical_queries"] == 1
    assert diagnostics["physical_parts"] == 5
    assert diagnostics["overflow_splits"] == 1
    assert diagnostics["overflow_events"][0]["query_status"] == overflow_status
    assert catalog.source_ids.tolist() == ["7"]
    assert diagnostics["rows_received"] == 4

    tile_bounds = []
    for call in client.calls[1:]:
        ra = re.search(r"ra >= ([^ ]+) AND\s+ra (?:<|<=) ([^ ]+)", call["query"])
        dec = re.search(r"dec >= ([^ ]+) AND\s+dec (?:<|<=) ([^ ]+)", call["query"])
        assert ra is not None and dec is not None
        tile_bounds.append(tuple(float(value) for value in (*ra.groups(), *dec.groups())))
    assert len(set(tile_bounds)) == 4
    assert {bounds[0:2] for bounds in tile_bounds} == {(0.0, 180.0), (180.0, 360.0)}
    assert {bounds[2:4] for bounds in tile_bounds} == {(-90.0, 0.0), (0.0, 90.0)}


def test_spatial_subdivision_bounds_handle_ra_wrap_and_polar_cones():
    root_children = gq._subdivide_sky_tile((0.0, 360.0, -90.0, 90.0))
    wrap_tiles = [
        tile
        for tile in root_children
        if gq._sky_tile_intersects_cones(tile, [(359.9, 0.0, 0.2)])
    ]
    polar_tiles = [
        tile
        for tile in root_children
        if gq._sky_tile_intersects_cones(tile, [(10.0, 89.9, 0.3)])
    ]

    assert len(wrap_tiles) == 4
    assert len(polar_tiles) == 2


def test_provider_failure_does_not_trigger_spatial_subdivision_or_leak_error_text():
    failed_client = _ScriptedAsyncTapClient([RuntimeError("provider-body-marker")])
    fallback_client = _ScriptedAsyncTapClient(
        [_AsyncTapJob([_working_row("9", 12.0, 15.0, 15.0)])]
    )
    catalog, diagnostics = asyncio.run(
        gq.fetch_gaia_working_catalog(
            [(12.0, 15.0, 0.25)],
            max_radius_deg=0.5,
            providers=[
                _working_provider(failed_client, "first"),
                _working_provider(fallback_client, "second"),
            ],
        )
    )

    assert len(failed_client.calls) == len(fallback_client.calls) == 1
    assert diagnostics["physical_parts"] == 1
    assert diagnostics["provider_attempts"] == 2
    assert diagnostics["overflow_splits"] == 0
    assert diagnostics["provider_failures"][0]["provider"] == "first"
    assert "provider-body-marker" not in json.dumps(diagnostics)
    assert catalog.source_ids.tolist() == ["9"]


def test_later_spatial_part_failure_discards_all_partial_rows():
    client = _ScriptedAsyncTapClient(
        [
            _AsyncTapJob([], query_status="OVERFLOW", complete=False),
            _AsyncTapJob([_working_row("12345678901234567890", 0.0, 0.0, 15.0)]),
            RuntimeError("late-provider-body-marker"),
        ]
    )

    with pytest.raises(gq.GaiaWorkingCatalogFetchError) as excinfo:
        asyncio.run(
            gq.fetch_gaia_working_catalog(
                [(0.0, 0.0, 1.0)],
                max_radius_deg=1.0,
                providers=[_working_provider(client)],
                maxrec=1000,
            )
        )

    assert len(client.calls) == 3
    assert excinfo.value.diagnostics["rows_received"] == 1
    assert excinfo.value.diagnostics["partial_rows_discarded"] is True
    assert "12345678901234567890" not in str(excinfo.value)
    assert "12345678901234567890" not in json.dumps(excinfo.value.diagnostics)
    assert "late-provider-body-marker" not in json.dumps(excinfo.value.diagnostics)


def _exhaustive_working_catalog_match(catalog, query_ra, query_dec, radius_arcsec):
    indices = np.full(len(query_ra), -1, dtype=np.int64)
    separations = np.full(len(query_ra), np.inf, dtype=np.float64)
    for query_index, (ra, dec) in enumerate(zip(query_ra, query_dec)):
        delta_ra = np.deg2rad(np.remainder(catalog.ra_deg - ra + 180.0, 360.0) - 180.0)
        query_dec_rad = np.deg2rad(dec)
        catalog_dec_rad = np.deg2rad(catalog.dec_deg)
        query_cos_dec = 0.0 if abs(dec) == 90.0 else np.cos(query_dec_rad)
        catalog_cos_dec = np.cos(catalog_dec_rad)
        catalog_cos_dec[np.abs(catalog.dec_deg) == 90.0] = 0.0
        haversine = np.sin((catalog_dec_rad - query_dec_rad) / 2.0) ** 2 + (
            query_cos_dec * catalog_cos_dec * np.sin(delta_ra / 2.0) ** 2
        )
        np.clip(haversine, 0.0, 1.0, out=haversine)
        distances = 2.0 * np.arctan2(np.sqrt(haversine), np.sqrt(1.0 - haversine))
        distances *= 180.0 * 3600.0 / np.pi
        nearest = int(np.argmin(distances))
        if distances[nearest] <= radius_arcsec + 1e-9:
            indices[query_index] = nearest
            separations[query_index] = distances[nearest]
    return indices, separations


def test_working_catalog_neighbor_search_matches_exhaustive_random_oracle():
    rng = np.random.default_rng(104_729)
    source_count = 73
    source_ra = rng.uniform(0.0, 360.0, source_count)
    source_dec = np.rad2deg(np.arcsin(rng.uniform(-1.0, 1.0, source_count)))
    rows = [
        _working_row(str(source_count - index), ra, dec, 15.0)
        for index, (ra, dec) in enumerate(zip(source_ra, source_dec))
    ]
    catalog = gq.GaiaWorkingCatalog(rows)
    query_ra = np.concatenate((rng.uniform(-720.0, 720.0, 24), [359.999, 0.001, 45.0]))
    query_dec = np.concatenate((rng.uniform(-90.0, 90.0, 24), [0.0, 0.0, 89.999]))

    for radius_arcsec in (0.01, 12_000.0, 500_000.0):
        actual_indices, actual_separations = catalog.match_nearest(
            query_ra,
            query_dec,
            radius_arcsec=radius_arcsec,
        )
        expected_indices, expected_separations = _exhaustive_working_catalog_match(
            catalog,
            query_ra,
            query_dec,
            radius_arcsec,
        )
        assert actual_indices.tolist() == expected_indices.tolist()
        np.testing.assert_allclose(
            actual_separations,
            expected_separations,
            rtol=1e-10,
            atol=1e-8,
        )


def test_working_catalog_neighbor_search_ties_wrap_poles_and_radius_boundary():
    tied_catalog = gq.GaiaWorkingCatalog(
        [
            _working_row("20", 359.0, 0.0, 15.0),
            _working_row("3", 1.0, 0.0, 15.0),
            _working_row("12", 200.0, 90.0, 15.0),
            _working_row("5", 10.0, 90.0, 15.0),
            _working_row("2", 10.0, 89.999, 15.0),
        ]
    )
    query_ra = np.asarray([0.0, 123.0, 0.0])
    query_dec = np.asarray([0.0, 90.0, 0.0])
    actual_indices, actual_separations = tied_catalog.match_nearest(
        query_ra,
        query_dec,
        radius_arcsec=7_200.0,
    )
    expected_indices, expected_separations = _exhaustive_working_catalog_match(
        tied_catalog,
        query_ra,
        query_dec,
        7_200.0,
    )

    assert actual_indices.tolist() == expected_indices.tolist()
    assert tied_catalog.source_ids[actual_indices[0]] == "3"
    assert tied_catalog.source_ids[actual_indices[1]] == "5"
    assert actual_separations == pytest.approx(expected_separations, abs=1e-8)

    boundary_catalog = gq.GaiaWorkingCatalog([_working_row("1", 1.0 / 3600.0, 0.0, 15.0)])
    boundary_indices, boundary_separations = boundary_catalog.match_nearest(
        [0.0],
        [0.0],
        radius_arcsec=1.0,
    )
    outside_indices, outside_separations = boundary_catalog.match_nearest(
        [0.0],
        [0.0],
        radius_arcsec=0.99,
    )
    assert boundary_indices.tolist() == [0]
    assert boundary_separations[0] == pytest.approx(1.0, abs=1e-8)
    assert outside_indices.tolist() == [-1]
    assert np.isinf(outside_separations[0])


def test_working_catalog_neighbor_search_scales_to_field_catalog():
    rng = np.random.default_rng(65_537)
    source_count = 84_000
    query_count = 2_000
    source_ra = rng.uniform(0.0, 360.0, source_count)
    source_dec = np.rad2deg(np.arcsin(rng.uniform(-1.0, 1.0, source_count)))
    catalog = gq.GaiaWorkingCatalog(
        _working_row(str(index + 1), ra, dec, 15.0)
        for index, (ra, dec) in enumerate(zip(source_ra, source_dec))
    )
    query_ra = rng.uniform(0.0, 360.0, query_count)
    query_dec = np.rad2deg(np.arcsin(rng.uniform(-1.0, 1.0, query_count)))
    query_ra[:10] = source_ra[:10]
    query_dec[:10] = source_dec[:10]

    indices, separations = catalog.match_nearest(
        query_ra,
        query_dec,
        radius_arcsec=0.1,
    )

    assert len(catalog) == 84_000
    assert len(query_ra) == 2_000
    assert len(query_ra) * len(catalog) > 100_000_000
    assert indices[:10].tolist() == list(range(10))
    assert separations[:10].tolist() == pytest.approx([0.0] * 10, abs=1e-8)
    assert np.all(indices[10:] == -1)
    assert np.all(np.isinf(separations[10:]))


def test_working_catalog_candidate_work_limit_and_spatial_cache_lifecycle():
    catalog = gq.GaiaWorkingCatalog(
        [_working_row(str(index + 1), index * 0.001, 0.0, 15.0) for index in range(8)]
    )
    with pytest.raises(ValueError, match="max_pair_comparisons"):
        catalog.match_nearest(
            [0.0],
            [0.0],
            radius_arcsec=60.0,
            max_pair_comparisons=2,
        )

    catalog.match_nearest([0.0], [0.0], radius_arcsec=60.0)
    assert catalog._spatial_index is not None
    catalog.clear()
    assert catalog._spatial_index is None

    catalog = gq.GaiaWorkingCatalog([_working_row("1", 1.0, 2.0, 15.0)])
    catalog.match_nearest([1.0], [2.0], radius_arcsec=1.0)
    assert catalog._spatial_index is not None
    catalog.close()
    assert catalog._spatial_index is None
