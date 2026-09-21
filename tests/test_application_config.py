from __future__ import annotations

from pathlib import Path

from grandgousier.config import (
    application_settings_mapping,
    archive_sources_mapping,
    field_archive_mapping,
    product_store_mapping,
    public_config_summary,
    read_config_file,
)
from grandgousier.services.runtime import FieldArchiveJobConfig
from grandgousier.settings import get_settings


def test_general_config_extracts_application_and_field_archive_sections(tmp_path: Path):
    path = tmp_path / "grandgousier.yaml"
    path.write_text(
        """
database:
  url: postgresql+psycopg://user:secret@db/grandgousier
logging:
  level: WARNING
paths:
  processed_root: C:\\data\\processed
  out_root: reports\\field_archive
field_archive:
  field_id: 33
  catalog_name: shared
  stage3_policies:
    - filter: V
      min_common: 15
      reference_series_id: 88
""".strip(),
        encoding="utf-8",
    )
    payload = read_config_file(path)
    app = application_settings_mapping(payload)
    field = field_archive_mapping(payload)
    public = public_config_summary(payload)

    assert app["database_url"].endswith("@db/grandgousier")
    assert app["log_level"] == "WARNING"
    assert field["field_id"] == 33
    assert field["processed_root"] == r"C:\data\processed"
    assert field["out_root"] == r"reports\field_archive"
    assert field["database_url"] == app["database_url"]
    assert public["database_url"] == "<configured>"
    assert public["field_archive"]["database_url"] == "<configured>"
    assert "secret" not in str(public)



def test_stage1_archive_config_is_parsed_and_secret_safe(tmp_path: Path):
    path = tmp_path / "stage1.yaml"
    path.write_text(
        r"""
archives:
  master-prod:
    adapter: master-manifest
    instrument_profile: MASTER
    metadata:
      backend: prepared-inventory
      manifest: C:\master\inventory.csv
      database_url: postgresql://user:secret@private-db/imdata
      endpoint: https://user:another-secret@example.invalid/api
      connection_env: GRANDGOUSIER_MASTER_DATABASE_URL
    assets:
      backend: master-cache
      cache_root: C:\master\cache
      password: hidden
products:
  backend: filesystem
  store_id: stage1-default
  root: C:\grandgousier\products
""".strip(),
        encoding="utf-8",
    )
    payload = read_config_file(path)
    archives = archive_sources_mapping(payload)
    products = product_store_mapping(payload)
    public = public_config_summary(payload)

    assert archives["master-prod"]["adapter"] == "master-manifest"
    assert products["backend"] == "filesystem"
    assert public["archives"]["master-prod"]["metadata"]["database_url"] == "<configured>"
    assert public["archives"]["master-prod"]["metadata"]["endpoint"] == "<configured>"
    assert public["archives"]["master-prod"]["assets"]["password"] == "<configured>"
    assert "secret" not in str(public)
    assert "hidden" not in str(public)

def test_field_archive_job_accepts_general_config_and_cli_override(tmp_path: Path):
    path = tmp_path / "grandgousier.yaml"
    path.write_text(
        """
paths:
  processed_root: from_paths
  out_root: reports/from_config
field_archive:
  field_id: 33
  catalog_name: shared
  execute: false
  stage3_policies:
    - filter: R
      min_common: 20
      reference_series_id: 89
""".strip(),
        encoding="utf-8",
    )
    config = FieldArchiveJobConfig.from_sources(
        config_file=path,
        environ={},
        overrides={"processed_root": "from_cli"},
    )
    assert config.processed_root == "from_cli"
    assert config.out_root == "reports/from_config"
    assert config.field_id == 33
    assert config.stage3_policies[0].filter_name == "R"
    assert config.stage3_policies[0].reference_series_id == 89


def test_legacy_flat_field_archive_config_remains_supported(tmp_path: Path):
    path = tmp_path / "legacy.yaml"
    path.write_text(
        """
processed_root: legacy_root
field_id: 33
catalog_name: legacy_catalog
stage3_policies: ["V:15:88"]
""".strip(),
        encoding="utf-8",
    )
    config = FieldArchiveJobConfig.from_sources(config_file=path, environ={})
    assert config.processed_root == "legacy_root"
    assert config.catalog_name == "legacy_catalog"
    assert config.stage3_policies[0].reference_series_id == 88


def test_get_settings_overlays_explicit_application_config(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GRANDGOUSIER_LOG_LEVEL", "INFO")
    path = tmp_path / "app.json"
    path.write_text('{"logging":{"level":"ERROR"}}', encoding="utf-8")
    settings = get_settings(config_file=path)
    assert settings.log_level == "ERROR"
