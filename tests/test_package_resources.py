from __future__ import annotations

from pathlib import Path

import yaml

from grandgousier.db.migrations import MIGRATIONS, migration_path, migrations_dir, validate_manifest
from grandgousier.resources import instrument_profile_path, resource_path


ROOT = Path(__file__).resolve().parents[1]


def test_bundled_instrument_profiles_exist_and_parse() -> None:
    expected_names = {"master.yaml": "MASTER", "robophot.yaml": "RoboPhot"}
    for filename, expected_name in expected_names.items():
        path = instrument_profile_path(filename)
        assert path.is_file()
        assert "grandgousier" in path.parts and "resources" in path.parts
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert payload["name"] == expected_name


def test_bundled_profiles_match_repository_compatibility_copies() -> None:
    for filename in ("master.yaml", "robophot.yaml"):
        packaged = instrument_profile_path(filename)
        repository = ROOT / "configs" / filename
        assert packaged.read_bytes() == repository.read_bytes()


def test_managed_migrations_resolve_from_installed_package_resources() -> None:
    validate_manifest()
    assert migrations_dir() == resource_path("migrations")
    assert migrations_dir().is_dir()
    for spec in MIGRATIONS:
        packaged = migration_path(spec)
        assert packaged.is_file()
        assert migrations_dir() in packaged.parents


def test_bundled_migrations_match_repository_compatibility_copies() -> None:
    for spec in MIGRATIONS:
        packaged = migration_path(spec)
        repository = ROOT / "scripts" / spec.filename
        assert repository.is_file()
        assert packaged.read_bytes() == repository.read_bytes()


def test_all_repository_sql_resources_are_packaged_byte_identically() -> None:
    repository_files = sorted((ROOT / "scripts").glob("*.sql"))
    assert repository_files
    packaged_names = {path.name for path in resource_path("migrations").glob("*.sql")}
    assert packaged_names == {path.name for path in repository_files}
    for repository in repository_files:
        assert (resource_path("migrations") / repository.name).read_bytes() == repository.read_bytes()
