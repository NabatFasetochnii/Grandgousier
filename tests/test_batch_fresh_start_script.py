from pathlib import Path


def test_batch_runner_fresh_start_disables_legacy_state_import_and_manifest_recovery() -> None:
    script = Path(__file__).resolve().parents[1] / "process_all_tess_observations_v1_5.ps1"
    text = script.read_text(encoding="utf-8-sig")

    assert "[switch]$FreshStart" in text
    assert "(-not $FreshStart) -and (Test-Path -LiteralPath $StateCsv" in text
    assert "(-not $FreshStart) -and\n        $previous" in text
    assert 'fresh_start = [bool]$FreshStart' in text
    assert '$BatchScriptVersion = "1.5.2"' in text


def test_batch_runner_excludes_metadata_identical_copied_directories() -> None:
    script = Path(__file__).resolve().parents[1] / "process_all_tess_observations_v1_5.ps1"
    text = script.read_text(encoding="utf-8-sig")

    assert "function Get-FitsDirectorySignature" in text
    assert "ContentSignature = $contentSignature" in text
    assert 'duplicate_directory:$($owner.RelativePath)' in text
    assert 'duplicate_directory_guard = "filename_size_signature_v1"' in text
    assert '$BatchScriptVersion = "1.5.2"' in text
