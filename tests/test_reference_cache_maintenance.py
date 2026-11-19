from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from grandgousier.archive import auto_process
from grandgousier.cli import build_parser


def _generation(cache_dir: Path, source_id: str, *, age_days: float = 0.0) -> tuple[Path, str]:
    positions = [{"source_id": source_id, "ra_deg": 10.0, "dec_deg": 20.0}]
    fingerprint, provenance = auto_process._selected_reference_cache_fingerprint(
        provider="panstarrs_dr2",
        provider_release="dr2",
        identity_catalog_name="gaia_dr3",
        missing_identity_source_ids=[source_id],
        persisted_identity_sources=positions,
        required_bands=["g", "r"],
        filters={
            "require_bands": ["g", "r"],
            "min_mag": None,
            "max_mag": None,
            "max_mag_err": None,
            "min_detections": None,
            "use_remote_filters": True,
            "limit": None,
        },
        match_radius_arcsec=1.0,
    )
    generation_dir = cache_dir / f"panstarrs_dr2_{fingerprint}"
    generation_dir.mkdir(parents=True)
    csv_bytes = b"catalog_source_id,selected_identity_source_id\nps1-id," + source_id.encode() + b"\n"
    (generation_dir / "data.csv").write_bytes(csv_bytes)
    diagnostics = {
        "logical_query_groups": 1,
        "provider_requests": 1,
        "provider_response_bytes_received": 20,
        "provider_rows_received": 1,
        "provider_rows_retained": 1,
        "provider_exact_duplicates_collapsed": 0,
        "provider_conflicting_source_ids": 0,
        "provider_conflicting_rows": 0,
        "matched_rows": 1,
        "ambiguous_rows": 0,
        "ambiguous_identities": 0,
        "unmatched_rows": 0,
        "unmatched_identities": 0,
    }
    manifest = {
        **provenance,
        "fingerprint": fingerprint,
        "logical_query_groups": 1,
        "provider_requests": 1,
        "rows_received": 1,
        "retained_rows": 1,
        "selected_reference_diagnostics": diagnostics,
        "csv_sha256": hashlib.sha256(csv_bytes).hexdigest(),
        "complete": True,
    }
    (generation_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    mtime = time.time() - age_days * 86400
    os.utime(generation_dir / "data.csv", (mtime, mtime))
    os.utime(generation_dir / "manifest.json", (mtime, mtime))
    os.utime(generation_dir, (mtime, mtime))
    return generation_dir, fingerprint


def test_audit_reports_valid_invalid_temp_unknown_metadata_and_age(tmp_path):
    cache_dir = tmp_path / "reference_photometry"
    cache_dir.mkdir()
    valid_dir, valid_fingerprint = _generation(cache_dir, "42", age_days=9)
    invalid_dir, invalid_fingerprint = _generation(cache_dir, "84", age_days=2)
    manifest_path = invalid_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["complete"] = False
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (invalid_dir / "operator-note.txt").write_text("leave me", encoding="utf-8")
    orphan = cache_dir / f".panstarrs_dr2_{valid_fingerprint}.orphan.tmp"
    orphan.mkdir()
    (orphan / "partial.csv").write_text("partial", encoding="utf-8")
    (cache_dir / "unrelated.txt").write_text("untouched", encoding="utf-8")

    report = auto_process.audit_selected_reference_cache(cache_dir)

    by_fingerprint = {item["fingerprint"]: item for item in report["generations"]}
    assert report["generation_count"] == 2
    assert report["valid_generation_count"] == 1
    assert report["invalid_generation_count"] == 1
    assert by_fingerprint[valid_fingerprint]["valid"] is True
    assert by_fingerprint[valid_fingerprint]["provider"] == "panstarrs_dr2"
    assert by_fingerprint[valid_fingerprint]["provider_release"] == "dr2"
    assert by_fingerprint[valid_fingerprint]["selected_identity_count"] == 1
    assert by_fingerprint[valid_fingerprint]["age_days"] >= 8.9
    assert by_fingerprint[invalid_fingerprint]["valid"] is False
    assert "operator-note.txt" in by_fingerprint[invalid_fingerprint]["unexpected_files"][0]
    assert report["generation_bytes"] >= (valid_dir / "data.csv").stat().st_size
    assert report["fingerprints"] == [
        {
            "fingerprint": item["fingerprint"],
            "status": item["status"],
            "provider": item["provider"],
            "provider_release": item["provider_release"],
        }
        for item in sorted(report["generations"], key=lambda item: item["fingerprint"])
    ]
    assert report["orphan_tmp_count"] == 1
    assert report["orphan_tmp_bytes"] == len("partial")
    assert any(item["name"] == "unrelated.txt" for item in report["unexpected_files"])
    assert any(item["name"] == "operator-note.txt" for item in report["unexpected_files"])
    assert any(item["name"] == "partial.csv" for item in report["unexpected_files"])
    assert report["reparse_point_count"] == 0


def test_prune_dry_run_then_apply_only_old_valid_generations_and_is_idempotent(tmp_path):
    cache_dir = tmp_path / "reference_photometry"
    cache_dir.mkdir()
    oldest_dir, oldest_fingerprint = _generation(cache_dir, "42", age_days=40)
    old_dir, old_fingerprint = _generation(cache_dir, "84", age_days=20)
    fresh_dir, fresh_fingerprint = _generation(cache_dir, "126", age_days=1)
    fresh_bytes = (fresh_dir / "data.csv").stat().st_size + (fresh_dir / "manifest.json").stat().st_size
    limits = {"max_age_days": 5, "max_count": 1, "max_bytes": fresh_bytes}

    dry_run = auto_process.prune_selected_reference_cache(cache_dir, **limits)

    assert dry_run["status"] == "PLANNED"
    assert [item["fingerprint"] for item in dry_run["plan"]["selected"]] == [
        oldest_fingerprint,
        old_fingerprint,
    ]
    assert oldest_dir.is_dir() and old_dir.is_dir() and fresh_dir.is_dir()
    assert not dry_run["lock_acquired"]
    assert not (cache_dir / auto_process._REFERENCE_PHOTOMETRY_CACHE_LOCK_FILENAME).exists()

    applied = auto_process.prune_selected_reference_cache(cache_dir, **limits, apply=True)

    apply_blocker = auto_process._reference_cache_prune_apply_blocker()
    if apply_blocker is not None:
        assert applied["status"] == "PARTIAL"
        assert apply_blocker in applied["errors"]
        assert applied["lock_acquired"] is False
        assert oldest_dir.is_dir() and old_dir.is_dir() and fresh_dir.is_dir()
        assert not (cache_dir / auto_process._REFERENCE_PHOTOMETRY_CACHE_LOCK_FILENAME).exists()
        return

    assert applied["status"] == "APPLIED"
    assert applied["lock_acquired"] is True
    assert [item["fingerprint"] for item in applied["deleted"]] == [
        oldest_fingerprint,
        old_fingerprint,
    ]
    assert not oldest_dir.exists() and not old_dir.exists()
    assert (fresh_dir / "data.csv").is_file()
    assert (fresh_dir / "manifest.json").is_file()
    assert applied["audit_after"]["generation_count"] == 1
    assert applied["audit_after"]["generations"][0]["fingerprint"] == fresh_fingerprint

    again = auto_process.prune_selected_reference_cache(cache_dir, **limits, apply=True)
    assert again["status"] == "NOOP"
    assert again["deleted"] == []
    assert fresh_dir.is_dir()


def test_prune_never_deletes_invalid_generation_or_unexpected_contents(tmp_path):
    cache_dir = tmp_path / "reference_photometry"
    cache_dir.mkdir()
    generation_dir, fingerprint = _generation(cache_dir, "42", age_days=60)
    unknown = generation_dir / "preserve-me.txt"
    unknown.write_text("user content", encoding="utf-8")
    root_unknown = cache_dir / "keep.txt"
    root_unknown.write_text("also user content", encoding="utf-8")

    report = auto_process.prune_selected_reference_cache(
        cache_dir,
        max_age_days=10,
        max_count=0,
        max_bytes=0,
        apply=True,
    )

    assert report["status"] == "NOOP"
    assert report["plan"]["selected"] == []
    assert report["audit_before"]["invalid_generation_count"] == 1
    assert generation_dir.is_dir()
    assert unknown.read_text(encoding="utf-8") == "user content"
    assert root_unknown.read_text(encoding="utf-8") == "also user content"
    assert any(item["fingerprint"] == fingerprint for item in report["audit_after"]["generations"])


def test_prune_missing_root_is_reported_and_cli_returns_nonzero(tmp_path, capsys):
    cache_dir = tmp_path / "missing_reference_photometry"
    limits = {"max_age_days": 5, "max_count": 0, "max_bytes": 0}

    applied = auto_process.prune_selected_reference_cache(cache_dir, **limits, apply=True)

    assert applied["status"] == "PARTIAL"
    assert applied["audit_before"]["errors"]
    assert applied["errors"]
    assert applied["lock_acquired"] is False
    assert not cache_dir.exists()

    from grandgousier import cli

    exit_code = cli.main(
        [
            "prune-reference-photometry-cache",
            "--cache-dir",
            str(cache_dir),
            "--max-age",
            "5",
            "--max-count",
            "0",
            "--max-bytes",
            "0",
        ]
    )
    output = json.loads(capsys.readouterr().out)
    assert exit_code != 0
    assert output["status"] == "PARTIAL"
    assert output["errors"]
    assert not cache_dir.exists()


def test_prune_unreadable_prelock_audit_does_not_write_lock_or_prune(tmp_path, monkeypatch):
    cache_dir = tmp_path / "reference_photometry"
    cache_dir.mkdir()
    preserved = cache_dir / "operator-note.txt"
    preserved.write_text("preserve", encoding="utf-8")
    real_scandir = auto_process.os.scandir

    def denied_scandir(path):
        if Path(path) == cache_dir:
            raise PermissionError("simulated unreadable cache root")
        return real_scandir(path)

    monkeypatch.setattr(auto_process.os, "scandir", denied_scandir)
    report = auto_process.prune_selected_reference_cache(
        cache_dir,
        max_age_days=5,
        max_count=0,
        max_bytes=0,
        apply=True,
    )

    lock_path = cache_dir / auto_process._REFERENCE_PHOTOMETRY_CACHE_LOCK_FILENAME
    assert report["status"] == "PARTIAL"
    assert report["audit_before"]["errors"]
    assert any("pre-lock audit:" in error for error in report["errors"])
    assert report["lock_acquired"] is False
    assert report["deleted"] == []
    assert not lock_path.exists()
    assert preserved.read_text(encoding="utf-8") == "preserve"


def test_prune_locked_audit_errors_block_deletion_without_lockfile_writes(tmp_path, monkeypatch):
    if auto_process._reference_cache_prune_apply_blocker() is not None:
        pytest.skip("platform does not provide descriptor-relative safe prune apply")
    cache_dir = tmp_path / "reference_photometry"
    cache_dir.mkdir()
    generation_dir, _fingerprint = _generation(cache_dir, "42", age_days=40)
    lock_path = cache_dir / auto_process._REFERENCE_PHOTOMETRY_CACHE_LOCK_FILENAME
    lock_path.write_bytes(b"\0")
    original_lock_bytes = lock_path.read_bytes()
    real_audit = auto_process.audit_selected_reference_cache
    audit_calls = 0

    def audit_with_locked_error(path):
        nonlocal audit_calls
        audit_calls += 1
        report = real_audit(path)
        if audit_calls == 2:
            report["errors"].append("simulated locked re-audit failure")
        return report

    monkeypatch.setattr(auto_process, "audit_selected_reference_cache", audit_with_locked_error)
    report = auto_process.prune_selected_reference_cache(
        cache_dir,
        max_age_days=5,
        max_count=0,
        max_bytes=0,
        apply=True,
    )

    assert audit_calls == 2
    assert report["status"] == "PARTIAL"
    assert report["audit_before"]["errors"]
    assert any("locked audit:" in error for error in report["errors"])
    assert report["lock_acquired"] is True
    assert report["deleted"] == []
    assert generation_dir.is_dir()
    assert lock_path.read_bytes() == original_lock_bytes


def test_prune_apply_fails_closed_without_safe_unlink_but_audit_and_dry_run_remain(
    tmp_path, monkeypatch
):
    cache_dir = tmp_path / "reference_photometry"
    cache_dir.mkdir()
    generation_dir, _fingerprint = _generation(cache_dir, "42", age_days=40)
    blocker = "simulated platform cannot guarantee no-follow deletion"
    monkeypatch.setattr(
        auto_process,
        "_reference_cache_prune_apply_blocker",
        lambda platform_name=None: blocker,
    )
    monkeypatch.setattr(
        auto_process,
        "_selected_reference_cache_lock",
        lambda *_a, **_k: pytest.fail("unsafe platform must fail before lock or mutation"),
    )
    monkeypatch.setattr(
        auto_process,
        "_delete_reference_cache_generation",
        lambda *_a, **_k: pytest.fail("unsafe platform must not call deletion"),
    )

    audit = auto_process.audit_selected_reference_cache(cache_dir)
    dry_run = auto_process.prune_selected_reference_cache(
        cache_dir, max_age_days=5, max_count=0, max_bytes=0
    )
    applied = auto_process.prune_selected_reference_cache(
        cache_dir, max_age_days=5, max_count=0, max_bytes=0, apply=True
    )

    assert audit["valid_generation_count"] == 1
    assert dry_run["status"] == "PLANNED"
    assert applied["status"] == "PARTIAL"
    assert applied["errors"] == [blocker]
    assert applied["lock_acquired"] is False
    assert applied["deleted"] == []
    assert generation_dir.is_dir()
    assert not (cache_dir / auto_process._REFERENCE_PHOTOMETRY_CACHE_LOCK_FILENAME).exists()


def test_windows_prune_apply_is_unsupported_without_race_safe_handle_deletion():
    assert "disabled on Windows" in auto_process._reference_cache_prune_apply_blocker(
        platform_name="nt"
    )


def test_audit_does_not_follow_generation_symlink(tmp_path):
    cache_dir = tmp_path / "reference_photometry"
    cache_dir.mkdir()
    outside_dir, fingerprint = _generation(tmp_path / "outside", "42")
    link = cache_dir / f"panstarrs_dr2_{fingerprint}"
    try:
        link.symlink_to(outside_dir, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    report = auto_process.audit_selected_reference_cache(cache_dir)

    assert report["generation_count"] == 1
    assert report["valid_generation_count"] == 0
    assert report["invalid_generation_count"] == 1
    assert report["reparse_point_count"] == 1
    assert report["generations"][0]["reparse_point"] is True
    assert (outside_dir / "data.csv").is_file()
    assert (outside_dir / "manifest.json").is_file()

    pruned = auto_process.prune_selected_reference_cache(
        cache_dir,
        max_age_days=0,
        max_count=0,
        max_bytes=0,
        apply=True,
    )
    assert pruned["status"] == "NOOP"
    assert link.is_symlink()
    assert (outside_dir / "data.csv").is_file()


def test_audit_and_prune_do_not_traverse_or_delete_mocked_reparse_generation(
    tmp_path, monkeypatch
):
    cache_dir = tmp_path / "reference_photometry"
    cache_dir.mkdir()
    generation_dir, fingerprint = _generation(cache_dir, "42", age_days=40)
    real_scandir = auto_process.os.scandir
    traversed_generations = []

    class _StatEntry:
        def __init__(self, name, entry_stat):
            self.name = name
            self._entry_stat = entry_stat

        def stat(self, *, follow_symlinks=True):
            return self._entry_stat

    def scandir_with_mocked_reparse_stat(path):
        path = Path(path)
        if path == generation_dir:
            traversed_generations.append(path)
        if path != cache_dir:
            return real_scandir(path)

        with real_scandir(path) as entries:
            wrapped_entries = []
            for entry in entries:
                entry_stat = entry.stat(follow_symlinks=False)
                if entry.name == generation_dir.name:
                    entry_stat = SimpleNamespace(
                        st_mode=entry_stat.st_mode,
                        st_mtime=entry_stat.st_mtime,
                        st_file_attributes=0x400,
                    )
                wrapped_entries.append(_StatEntry(entry.name, entry_stat))
        return nullcontext(wrapped_entries)

    monkeypatch.setattr(auto_process.os, "scandir", scandir_with_mocked_reparse_stat)

    audit = auto_process.audit_selected_reference_cache(cache_dir)

    assert audit["generation_count"] == 1
    assert audit["valid_generation_count"] == 0
    assert audit["reparse_point_count"] == 1
    assert audit["generations"][0]["fingerprint"] == fingerprint
    assert audit["generations"][0]["reparse_point"] is True
    assert traversed_generations == []

    pruned = auto_process.prune_selected_reference_cache(
        cache_dir,
        max_age_days=0,
        max_count=0,
        max_bytes=0,
        apply=True,
    )

    assert pruned["status"] == "NOOP"
    assert pruned["plan"]["selected"] == []
    assert pruned["deleted"] == []
    assert traversed_generations == []
    assert generation_dir.is_dir()
    assert (generation_dir / "data.csv").is_file()
    assert (generation_dir / "manifest.json").is_file()


def test_cache_cli_commands_are_explicit_and_prune_defaults_to_json_dry_run(tmp_path, capsys):
    cache_dir = tmp_path / "reference_photometry"
    cache_dir.mkdir()
    generation_dir, _fingerprint = _generation(cache_dir, "42", age_days=20)

    audit_args = build_parser().parse_args(
        ["audit-reference-photometry-cache", "--cache-dir", str(cache_dir)]
    )
    assert audit_args.command == "audit-reference-photometry-cache"
    assert audit_args.func.__name__ == "cmd_audit_reference_photometry_cache"

    from grandgousier import cli

    assert cli.main(["audit-reference-photometry-cache", "--cache-dir", str(cache_dir)]) == 0
    audit_output = json.loads(capsys.readouterr().out)
    assert audit_output["valid_generation_count"] == 1

    assert cli.main(
        [
            "prune-reference-photometry-cache",
            "--cache-dir",
            str(cache_dir),
            "--max-age",
            "5",
            "--max-count",
            "0",
            "--max-bytes",
            "0",
        ]
    ) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "DRY_RUN"
    assert output["apply"] is False
    assert output["plan"]["selected"]
    assert generation_dir.is_dir()

    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["prune-reference-photometry-cache", "--cache-dir", str(cache_dir)]
        )
