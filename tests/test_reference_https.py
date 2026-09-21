from __future__ import annotations

import ssl
from pathlib import Path

import pytest

from grandgousier.catalogs.reference_photometry import (
    _build_reference_https_context,
    _reference_tls_error_hint,
)


def _clear_ca_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GRANDGOUSIER_CA_BUNDLE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        monkeypatch.delenv(name, raising=False)


def test_reference_https_context_uses_certifi_or_python_default(monkeypatch: pytest.MonkeyPatch):
    _clear_ca_environment(monkeypatch)
    context, info = _build_reference_https_context()
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert info["source"] in {"certifi", "python_default"}
    if info["source"] == "certifi":
        assert info["path"] is not None
        assert Path(info["path"]).is_file()


def test_reference_https_context_rejects_missing_explicit_bundle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    _clear_ca_environment(monkeypatch)
    missing = tmp_path / "missing-ca.pem"
    monkeypatch.setenv("GRANDGOUSIER_CA_BUNDLE", str(missing))
    with pytest.raises(RuntimeError, match="missing CA bundle"):
        _build_reference_https_context()


def test_certificate_error_hint_reports_ca_source():
    exc = ssl.SSLCertVerificationError(1, "certificate verify failed")
    hint = _reference_tls_error_hint(exc, {"source": "certifi", "path": "C:/ca.pem"})
    assert "CA source: certifi" in hint
    assert "GRANDGOUSIER_CA_BUNDLE" in hint
    assert "certifi" in hint
