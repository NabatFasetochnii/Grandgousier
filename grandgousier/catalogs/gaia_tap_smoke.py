from __future__ import annotations

import asyncio
from contextlib import redirect_stderr, redirect_stdout
import inspect
import os
import re
import tempfile
from dataclasses import dataclass
from typing import Any

_PROVIDER_NAMES = ("gaia_aip_async", "gaia_ari_async", "gaia_esac_async")
_CONTROLLED_FAILURE_NAME = "gaia_smoke_controlled_failure"

# Vega is a fixed, well-known bright Gaia DR3 target. The 3.6-arcsec cone and G<=12
# limit keep each provider query intentionally small and independent of user data.
_SMOKE_CONES = ((279.23473479, 38.78368896, 0.001),)
_MAX_RADIUS_DEG = 0.002
_MAG_LIMIT = 12.0
_MAXREC = 20
_MAX_PHYSICAL_PARTS = 1
_MAX_SUBDIVISION_DEPTH = 1
_TIMEOUT_SEC = 5.0
_MAX_WAIT_SEC = 5.0


class _DiscardOutput:
    def write(self, value: str) -> int:
        return len(value)

    def flush(self) -> None:
        return None


@dataclass(frozen=True)
class _ObservedResponse:
    format: Any
    query_status: Any


class _ResponseRecordingClient:
    """Record only protocol metadata; never retain or render response contents."""

    def __init__(self, client: Any):
        self._client = client
        self.responses: list[_ObservedResponse] = []

    def launch_job_async(
        self,
        query: str,
        *,
        maxrec: int,
        dump_to_file: bool = False,
        output_format: str = "votable",
    ) -> Any:
        result = self._client.launch_job_async(
            query,
            maxrec=maxrec,
            dump_to_file=dump_to_file,
            output_format=output_format,
        )
        if inspect.isawaitable(result):
            async def observe_async_result():
                job = await result
                self._record(job)
                return job

            return observe_async_result()
        self._record(result)
        return result

    def _record(self, job: Any) -> None:
        response = getattr(job, "response", None)
        self.responses.append(
            _ObservedResponse(
                format=getattr(response, "format", None),
                query_status=getattr(response, "query_status", None),
            )
        )


class _ControlledFailureClient:
    """A deterministic first provider used only to exercise fetch fallback."""

    def launch_job_async(
        self,
        query: str,
        *,
        maxrec: int,
        dump_to_file: bool = False,
        output_format: str = "votable",
    ) -> Any:
        del query, maxrec, dump_to_file, output_format
        raise RuntimeError("controlled Gaia TAP smoke fallback trigger")


def run_gaia_tap_network_smoke(*, allow_network: bool = False) -> tuple[dict[str, Any], int]:
    """Run a tightly bounded provider smoke only after explicit network opt-in."""

    if not allow_network:
        return (
            {
                "status": "BLOCKED_OPT_IN_REQUIRED",
                "network_requests": 0,
                "database_access": "NONE",
                "reason": "EXPLICIT_NETWORK_OPT_IN_REQUIRED",
            },
            2,
        )

    try:
        original_directory = os.getcwd()
        with tempfile.TemporaryDirectory(prefix="grandgousier-gaia-tap-smoke-") as workspace:
            try:
                os.chdir(workspace)
                sink = _DiscardOutput()
                with redirect_stdout(sink), redirect_stderr(sink):
                    summary = _run_enabled_smoke()
            finally:
                os.chdir(original_directory)
    except Exception:
        return (
            {
                "status": "FAIL",
                "network_opt_in": True,
                "database_access": "NONE",
                "temporary_workspace": True,
                "failure_code": "SMOKE_SETUP_OR_EXECUTION_FAILED",
                "providers": [],
                "fallback": {"attempted": False, "status": "FAIL"},
            },
            2,
        )

    return summary, 0 if summary.get("status") == "PASS" else 2


def _run_enabled_smoke() -> dict[str, Any]:
    from grandgousier.catalogs import gaia_query as gq

    providers = gq._build_gaia_working_catalog_providers(_TIMEOUT_SEC)
    if tuple(provider.name for provider in providers) != _PROVIDER_NAMES:
        return {
            "status": "FAIL",
            "network_opt_in": True,
            "database_access": "NONE",
            "temporary_workspace": True,
            "failure_code": "WORKING_PROVIDER_SET_MISMATCH",
            "providers": [_not_attempted_provider(name) for name in _PROVIDER_NAMES],
            "fallback": {"attempted": False, "status": "FAIL"},
        }

    for provider in providers:
        if isinstance(provider.client, gq._HttpAsyncTapClient):
            provider.client.max_wait_sec = _MAX_WAIT_SEC

    provider_results: list[dict[str, Any]] = []
    for provider in providers:
        wrapped_provider, recorder = _recording_provider(gq, provider)
        result, _diagnostics = _fetch_once(
            gq,
            [wrapped_provider],
            provider_name=provider.name,
            recorder=recorder,
        )
        provider_results.append(result)

    fallback_provider = providers[0]
    wrapped_fallback, fallback_recorder = _recording_provider(gq, fallback_provider)
    controlled_failure = gq._GaiaTapProvider(
        name=_CONTROLLED_FAILURE_NAME,
        client=_ControlledFailureClient(),
        table_name=fallback_provider.table_name,
    )
    fallback_result, fallback_diagnostics = _fetch_once(
        gq,
        [controlled_failure, wrapped_fallback],
        provider_name=fallback_provider.name,
        recorder=fallback_recorder,
    )
    failures = (
        fallback_diagnostics.get("provider_failures", [])
        if isinstance(fallback_diagnostics, dict)
        else []
    )
    controlled_first_failed = any(
        isinstance(item, dict) and item.get("provider") == _CONTROLLED_FAILURE_NAME
        for item in failures
    )
    fallback_attempts = (
        fallback_diagnostics.get("provider_attempts", 0)
        if isinstance(fallback_diagnostics, dict)
        else 0
    )
    used = (
        fallback_diagnostics.get("providers_used", [])
        if isinstance(fallback_diagnostics, dict)
        else []
    )
    real_provider_used = fallback_provider.name in used
    fallback_ok = (
        controlled_first_failed
        and fallback_attempts >= 2
        and real_provider_used
        and fallback_result["status"] == "OK"
    )
    fallback = {
        "attempted": True,
        "controlled_first_provider_failed": bool(controlled_first_failed),
        "real_provider": fallback_provider.name,
        "real_provider_attempted": bool(fallback_attempts >= 2),
        "status": "OK" if fallback_ok else "FAIL",
        "response_format": fallback_result["response_format"],
        "query_status": fallback_result["query_status"],
        "source_count": fallback_result["source_count"],
        "source_ids_valid": fallback_result["source_ids_valid"],
    }
    all_ok = all(item["status"] == "OK" for item in provider_results) and fallback_ok
    return {
        "status": "PASS" if all_ok else "FAIL",
        "network_opt_in": True,
        "database_access": "NONE",
        "temporary_workspace": True,
        "cone": "fixed_vega_0.001deg",
        "limits": {
            "maxrec": _MAXREC,
            "max_physical_parts": _MAX_PHYSICAL_PARTS,
            "timeout_sec": _TIMEOUT_SEC,
            "max_wait_sec": _MAX_WAIT_SEC,
        },
        "providers": provider_results,
        "fallback": fallback,
    }


def _recording_provider(gq: Any, provider: Any) -> tuple[Any, _ResponseRecordingClient]:
    recorder = _ResponseRecordingClient(provider.client)
    return (
        gq._GaiaTapProvider(
            name=provider.name,
            client=recorder,
            table_name=provider.table_name,
        ),
        recorder,
    )


def _fetch_once(
    gq: Any,
    providers: list[Any],
    *,
    provider_name: str,
    recorder: _ResponseRecordingClient,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    catalog = None
    diagnostics: dict[str, Any] | None = None
    fetch_succeeded = False
    try:
        catalog, diagnostics = asyncio.run(
            gq.fetch_gaia_working_catalog(
                _SMOKE_CONES,
                max_radius_deg=_MAX_RADIUS_DEG,
                providers=providers,
                mag_limit=_MAG_LIMIT,
                maxrec=_MAXREC,
                max_subdivision_depth=_MAX_SUBDIVISION_DEPTH,
                max_physical_parts=_MAX_PHYSICAL_PARTS,
                gaia_timeout_sec=_TIMEOUT_SEC,
            )
        )
        fetch_succeeded = True
    except gq.GaiaWorkingCatalogFetchError as exc:
        if isinstance(exc.diagnostics, dict):
            diagnostics = exc.diagnostics
    except Exception:
        pass

    response = recorder.responses[0] if len(recorder.responses) == 1 else None
    response_info = _response_contract(response, provider_name=provider_name)
    source_count, source_ids_valid = _source_id_summary(catalog)
    provider_used = (
        isinstance(diagnostics, dict)
        and provider_name in diagnostics.get("providers_used", [])
    )
    okay = (
        fetch_succeeded
        and provider_used
        and response_info["valid"]
        and source_count > 0
        and source_ids_valid
    )
    if okay:
        failure_code = "NONE"
    elif not response_info["valid"]:
        failure_code = "RESPONSE_CONTRACT_FAILED"
    elif source_count == 0 or not source_ids_valid:
        failure_code = "EMPTY_OR_INVALID_SOURCE_IDS"
    else:
        failure_code = "FETCH_FAILED"

    if catalog is not None:
        catalog.clear()
    return (
        {
            "provider": provider_name,
            "attempted": True,
            "status": "OK" if okay else "FAIL",
            "response_format": response_info["format"],
            "query_status": response_info["query_status"],
            "source_count": source_count,
            "source_ids_valid": source_ids_valid,
            "failure_code": failure_code,
        },
        diagnostics,
    )


def _response_contract(
    response: _ObservedResponse | None, *, provider_name: str
) -> dict[str, Any]:
    if response is None:
        return {"format": "UNAVAILABLE", "query_status": "UNAVAILABLE", "valid": False}

    raw_format = response.format
    format_name = raw_format.strip().casefold() if isinstance(raw_format, str) else ""
    if format_name not in {"csv", "votable"}:
        return {"format": "UNSUPPORTED", "query_status": "UNAVAILABLE", "valid": False}

    raw_status = response.query_status
    status = raw_status.strip().upper() if isinstance(raw_status, str) else None
    if format_name == "votable":
        return {
            "format": "VOTABLE",
            "query_status": "OK" if status == "OK" else "MISSING_OR_NOT_OK",
            "valid": status == "OK",
        }
    return {
        "format": "CSV",
        "query_status": "NOT_PRESENT",
        "valid": provider_name in _PROVIDER_NAMES[1:] and status in {None, "OK"},
    }


def _source_id_summary(catalog: Any) -> tuple[int, bool]:
    if catalog is None:
        return 0, False
    try:
        source_ids = catalog.source_ids.tolist()
    except Exception:
        return 0, False
    valid = bool(source_ids) and all(
        type(source_id) is str and re.fullmatch(r"[0-9]+", source_id) is not None
        for source_id in source_ids
    )
    return len(source_ids), valid


def _not_attempted_provider(provider_name: str) -> dict[str, Any]:
    return {
        "provider": provider_name,
        "attempted": False,
        "status": "FAIL",
        "response_format": "UNAVAILABLE",
        "query_status": "UNAVAILABLE",
        "source_count": 0,
        "source_ids_valid": False,
        "failure_code": "NOT_ATTEMPTED",
    }
