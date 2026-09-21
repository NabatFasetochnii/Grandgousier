from __future__ import annotations

from pathlib import Path


FORBIDDEN_PUBLIC_STRINGS = (
    "master.kourovka.ru",
    "/master/imdata/",
    "master2/processing.php",
    "master2/index.php",
    "MASTER Collaboration Private Area",
    "212.220.202.178",
    "192.168.240.17",
    "googlov",
)


def test_public_package_has_no_private_master_transport_details() -> None:
    root = Path(__file__).resolve().parents[1]
    public_files = [root / "pyproject.toml", root / "README_PUBLIC.md"]
    public_files.extend((root / "grandgousier").rglob("*.py"))
    public_files.extend((root / "grandgousier").rglob("*.yaml"))
    public_files.extend((root / "grandgousier").rglob("*.sql"))
    corpus = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in public_files)
    lowered = corpus.lower()
    for token in FORBIDDEN_PUBLIC_STRINGS:
        assert token.lower() not in lowered, token


def test_private_remote_adapter_is_outside_public_package() -> None:
    root = Path(__file__).resolve().parents[1]
    public_stub = root / "grandgousier" / "archive" / "master_remote.py"
    private_adapter = root / "internal_tools" / "master_remote.py"
    assert public_stub.is_file()
    assert private_adapter.is_file()
    public_text = public_stub.read_text(encoding="utf-8")
    private_text = private_adapter.read_text(encoding="utf-8")
    assert "RemoteArchiveTransportUnavailable" in public_text
    assert len(private_text) > len(public_text) * 10
