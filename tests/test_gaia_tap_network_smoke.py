from __future__ import annotations

import json

import pytest

from grandgousier import cli
from grandgousier.catalogs import gaia_query as gq
from grandgousier.catalogs import gaia_tap_smoke as smoke


class _FakeTapClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def launch_job_async(
        self,
        query,
        *,
        maxrec,
        dump_to_file=False,
        output_format="votable",
    ):
        self.calls.append(
            {
                "query": query,
                "maxrec": maxrec,
                "dump_to_file": dump_to_file,
                "output_format": output_format,
            }
        )
        if not self.responses:
            raise AssertionError("unexpected extra fake TAP request")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        response_format, query_status, source_id = response
        rows = []
        if source_id is not None:
            rows.append(
                gq._TapCsvRow(
                    source_id=source_id,
                    ra=279.23473479,
                    dec=38.78368896,
                    phot_g_mean_mag=0.03,
                )
            )
        return gq._StaticTapJob(
            gq._TapResponse(
                rows=rows,
                format=response_format,
                complete=True,
                query_status=query_status,
            )
        )


def _install_fake_providers(monkeypatch, aip, ari, esa):
    clients = {"gaia_aip_async": aip, "gaia_ari_async": ari, "gaia_esac_async": esa}
    builder_calls = []

    def build_providers(timeout_sec):
        builder_calls.append(timeout_sec)
        return [
            gq._GaiaTapProvider(name=name, client=client)
            for name, client in clients.items()
        ]

    monkeypatch.setattr(gq, "_build_gaia_working_catalog_providers", build_providers)
    return clients, builder_calls


def _success(format_name="votable", query_status="OK", source_id="6917529027641081856"):
    return format_name, query_status, source_id


def test_default_cli_is_blocked_without_settings_or_network(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("opt-in-disabled smoke must not run")

    monkeypatch.setattr(smoke, "_run_enabled_smoke", forbidden)
    monkeypatch.setattr(cli, "get_settings", forbidden)

    exit_code = cli.main(["gaia-tap-network-smoke"])
    output = capsys.readouterr()
    summary = json.loads(output.out)

    assert exit_code != 0
    assert summary["status"] == "BLOCKED_OPT_IN_REQUIRED"
    assert summary["network_requests"] == 0
    assert "PASS" not in output.out
    assert output.err == ""


def test_opt_in_queries_providers_independently_and_exercises_fallback(
    monkeypatch, capsys
):
    aip = _FakeTapClient([_success("votable", "OK"), _success("votable", "OK")])
    ari = _FakeTapClient([_success("csv", None)])
    esa = _FakeTapClient([_success("votable", "OK")])
    clients, builder_calls = _install_fake_providers(monkeypatch, aip, ari, esa)

    def forbidden_settings(*args, **kwargs):
        raise AssertionError("standalone Gaia smoke must not load DB settings")

    monkeypatch.setattr(cli, "get_settings", forbidden_settings)
    monkeypatch.setattr(gq.Session, "execute", forbidden_settings)
    monkeypatch.setattr(gq.Session, "commit", forbidden_settings)
    monkeypatch.setattr(gq, "_upsert_catalog_sources", forbidden_settings)

    exit_code = cli.main(["gaia-tap-network-smoke", "--allow-network"])
    output = capsys.readouterr()
    summary = json.loads(output.out)

    assert exit_code == 0
    assert summary["status"] == "PASS"
    assert [item["provider"] for item in summary["providers"]] == [
        "gaia_aip_async",
        "gaia_ari_async",
        "gaia_esac_async",
    ]
    assert [item["status"] for item in summary["providers"]] == ["OK", "OK", "OK"]
    assert [len(client.calls) for client in clients.values()] == [2, 1, 1]
    assert summary["fallback"]["controlled_first_provider_failed"] is True
    assert summary["fallback"]["real_provider_attempted"] is True
    assert summary["fallback"]["status"] == "OK"
    assert summary["fallback"]["response_format"] == "VOTABLE"
    assert builder_calls == [smoke._TIMEOUT_SEC]
    assert all(call["maxrec"] == smoke._MAXREC for client in clients.values() for call in client.calls)
    assert all(not call["dump_to_file"] for client in clients.values() for call in client.calls)
    assert all(call["output_format"] == "votable" for client in clients.values() for call in client.calls)
    assert summary["limits"]["max_physical_parts"] == 1
    assert summary["temporary_workspace"] is True
    assert summary["database_access"] == "NONE"
    assert output.err == ""


@pytest.mark.parametrize(
    (
        "provider_name",
        "response_format",
        "query_status",
        "expected_format",
        "expected_status",
        "valid",
    ),
    [
        ("gaia_aip_async", "csv", None, "CSV", "NOT_PRESENT", False),
        ("gaia_aip_async", "votable", "OK", "VOTABLE", "OK", True),
        ("gaia_aip_async", "votable", "ERROR", "VOTABLE", "MISSING_OR_NOT_OK", False),
        ("gaia_aip_async", "votable", None, "VOTABLE", "MISSING_OR_NOT_OK", False),
        ("gaia_ari_async", "csv", None, "CSV", "NOT_PRESENT", True),
        ("gaia_esac_async", "votable", "OK", "VOTABLE", "OK", True),
        ("gaia_ari_async", "json", "OK", "UNSUPPORTED", "UNAVAILABLE", False),
    ],
)
def test_response_contract_enforces_provider_specific_formats(
    provider_name, response_format, query_status, expected_format, expected_status, valid
):
    result = smoke._response_contract(
        smoke._ObservedResponse(response_format, query_status),
        provider_name=provider_name,
    )

    assert result == {
        "format": expected_format,
        "query_status": expected_status,
        "valid": valid,
    }


def test_aip_csv_fails_smoke_without_leaking_source_ids(monkeypatch, capsys):
    source_id_marker = "9876543210123456789"
    aip = _FakeTapClient([_success("csv", None, source_id_marker), _success("csv", None)])
    ari = _FakeTapClient([_success("votable", "OK")])
    esa = _FakeTapClient([_success("csv", None)])
    _install_fake_providers(monkeypatch, aip, ari, esa)

    exit_code = cli.main(["gaia-tap-network-smoke", "--allow-network"])
    output = capsys.readouterr()
    summary = json.loads(output.out)

    assert exit_code != 0
    assert summary["status"] == "FAIL"
    assert [item["status"] for item in summary["providers"]] == ["FAIL", "OK", "OK"]
    assert summary["providers"][0]["failure_code"] == "RESPONSE_CONTRACT_FAILED"
    assert summary["fallback"]["status"] == "FAIL"
    assert source_id_marker not in output.out
    assert "PASS" not in output.out
    assert output.err == ""


def test_invalid_source_id_fails_nonzero_and_is_redacted(monkeypatch, capsys):
    identifier_marker = "SYNTHETIC_NON_DECIMAL_GAIA_ID_DO_NOT_PRINT"
    aip = _FakeTapClient(
        [
            _success("votable", "OK", identifier_marker),
            _success("votable", "OK"),
        ]
    )
    ari = _FakeTapClient([_success("votable", "OK")])
    esa = _FakeTapClient([_success("csv", None)])
    _install_fake_providers(monkeypatch, aip, ari, esa)

    exit_code = cli.main(["gaia-tap-network-smoke", "--allow-network"])
    output = capsys.readouterr()
    summary = json.loads(output.out)

    assert exit_code != 0
    assert summary["status"] == "FAIL"
    assert summary["providers"][0]["status"] == "FAIL"
    assert summary["providers"][0]["source_ids_valid"] is False
    assert identifier_marker not in output.out
    assert "PASS" not in output.out
    assert output.err == ""
