from __future__ import annotations

from grandgousier.catalogs import gaia_query as gq


class _FakeResponse:
    def __init__(self, body: bytes, *, content_type: str = "text/csv"):
        self.content = body
        self.encoding = "utf-8"
        self.headers = {"Content-Type": content_type}

    def raise_for_status(self):
        return None


def test_tap_csv_keeps_gaia_source_id_as_exact_text(monkeypatch):
    body = (
        b"source_id,ra,dec,ref_epoch,pmra,pmdec,phot_g_mean_mag,"
        b"phot_bp_mean_mag,phot_rp_mean_mag,ruwe,duplicated_source\n"
        b"6917529027641081856,123.5,-22.25,2016.0,1.2,-3.4,15.1,15.5,14.7,1.03,false\n"
    )
    seen = {}

    def fake_post(url, *, data, headers, timeout):
        seen.update(url=url, data=data, headers=headers, timeout=timeout)
        return _FakeResponse(body)

    monkeypatch.setattr(gq.requests, "post", fake_post)
    client = gq._HttpCsvTapClient("https://example.invalid/tap", timeout_sec=17)
    rows = client.launch_job("SELECT TOP 1 * FROM gaiadr3.gaia_source", maxrec=1).get_results()

    assert seen["url"] == "https://example.invalid/tap/sync"
    assert seen["data"]["FORMAT"] == "csv"
    assert seen["headers"]["Accept"].startswith("text/csv")
    assert seen["timeout"] == 17.0
    assert rows[0]["source_id"] == "6917529027641081856"
    assert rows[0]["ra"] == 123.5
    assert rows[0]["duplicated_source"] is False


def test_tap_csv_xml_error_is_rejected(monkeypatch):
    def fake_post(url, *, data, headers, timeout):
        return _FakeResponse(b'<?xml version="1.0"?><VOTABLE><INFO value="ERROR"/></VOTABLE>', content_type="application/x-votable+xml")

    monkeypatch.setattr(gq.requests, "post", fake_post)
    client = gq._HttpCsvTapClient("https://example.invalid/tap")
    try:
        client.launch_job("SELECT 1", maxrec=1)
    except RuntimeError as exc:
        assert "XML instead of CSV" in str(exc)
    else:
        raise AssertionError("expected XML response rejection")


def test_provider_chain_uses_http_csv_mirrors_before_esac(monkeypatch):
    class _FakeGaia:
        pass

    # _build_gaia_tap_providers imports Gaia at call time.  The real astroquery
    # dependency is present in production; this test only verifies our ordering
    # and transport class when import succeeds in the test environment.
    providers = gq._build_gaia_tap_providers(23)
    assert providers[0].name == "gaia_aip_csv"
    assert isinstance(providers[0].client, gq._HttpCsvTapClient)
    assert providers[1].name == "gaia_ari_csv"
    assert isinstance(providers[1].client, gq._HttpCsvTapClient)
    assert providers[-1].name == "gaia_esac"
