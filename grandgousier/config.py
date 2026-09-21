from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import yaml


def read_config_file(path: str | Path) -> dict[str, Any]:
    """Read a Grandgousier YAML/JSON config without applying side effects."""

    config_path = Path(path)
    if not config_path.is_file():
        raise ValueError(f"Grandgousier config does not exist: {config_path}")
    suffix = config_path.suffix.casefold()
    text = config_path.read_text(encoding="utf-8")
    if suffix == ".json":
        payload = json.loads(text)
    elif suffix in {".yaml", ".yml"}:
        payload = yaml.safe_load(text)
    else:
        raise ValueError("Grandgousier config must be JSON or YAML")
    if payload is None:
        return {}
    if not isinstance(payload, Mapping):
        raise ValueError("Grandgousier config root must be a mapping")
    return dict(payload)


_SECRET_CONFIG_KEY_PARTS = ("password", "secret", "token", "credential", "database_url")


def archive_sources_mapping(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Return configured Stage-1 archive sources without resolving credentials."""

    raw = payload.get("archives")
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError("Grandgousier config 'archives' must be a mapping")
    result: dict[str, dict[str, Any]] = {}
    for source_id, value in raw.items():
        source = str(source_id).strip()
        if not source:
            raise ValueError("Grandgousier archive source id must not be empty")
        if not isinstance(value, Mapping):
            raise ValueError(f"Grandgousier archive source {source!r} must be a mapping")
        result[source] = dict(value)
    return result


def product_store_mapping(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the Stage-1 ProductStore section, if configured."""

    raw = payload.get("products")
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError("Grandgousier config 'products' must be a mapping")
    return dict(raw)


def _public_value(value: Any, *, key: str | None = None) -> Any:
    key_fold = (key or "").casefold()
    if key and any(part in key_fold for part in _SECRET_CONFIG_KEY_PARTS):
        return "<configured>" if value is not None and value != "" else None
    if isinstance(value, str) and "://" in value and "@" in value:
        return "<configured>"
    if isinstance(value, Mapping):
        return {str(k): _public_value(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_public_value(item) for item in value]
    return value


def application_settings_mapping(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Extract application-level settings from either compact or nested config."""

    result: dict[str, Any] = {}
    database = payload.get("database")
    logging = payload.get("logging")
    if payload.get("database_url") not in {None, ""}:
        result["database_url"] = str(payload["database_url"])
    elif isinstance(database, Mapping) and database.get("url") not in {None, ""}:
        result["database_url"] = str(database["url"])

    if payload.get("log_level") not in {None, ""}:
        result["log_level"] = str(payload["log_level"])
    elif isinstance(logging, Mapping) and logging.get("level") not in {None, ""}:
        result["log_level"] = str(logging["level"])
    return result


def field_archive_mapping(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Extract field-archive job settings from general or legacy flat config.

    The pre-0.5.56 flat ``--job-config`` format remains valid.  New general
    configs may place job settings under ``field_archive`` and shared paths
    under ``paths``.
    """

    nested = payload.get("field_archive")
    if isinstance(nested, Mapping):
        result = dict(nested)
    else:
        # Legacy field-archive config: keep only job keys instead of copying
        # unrelated future application sections into runtime configuration.
        allowed = {
            "database_url",
            "processed_root",
            "field_id",
            "catalog_name",
            "instrument_profile",
            "execute",
            "resume",
            "stage2_recipe",
            "stage3_policies",
            "control_object_ids",
            "exclude_dates",
            "include_dates",
            "out_root",
            "plot_max_points",
        }
        result = {key: value for key, value in payload.items() if key in allowed}

    app = application_settings_mapping(payload)
    if "database_url" not in result and app.get("database_url"):
        result["database_url"] = app["database_url"]

    paths = payload.get("paths")
    if isinstance(paths, Mapping):
        if "processed_root" not in result and paths.get("processed_root") not in {None, ""}:
            result["processed_root"] = paths["processed_root"]
        if "out_root" not in result and paths.get("out_root") not in {None, ""}:
            result["out_root"] = paths["out_root"]
    return result


def public_config_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a secret-safe summary suitable for doctor/status JSON."""

    app = application_settings_mapping(payload)
    field_archive = field_archive_mapping(payload)
    return {
        "database_url": "<configured>" if app.get("database_url") else None,
        "log_level": app.get("log_level"),
        "field_archive": {
            key: ("<configured>" if key == "database_url" and value else value)
            for key, value in field_archive.items()
        },
        "archives": _public_value(archive_sources_mapping(payload)),
        "products": _public_value(product_store_mapping(payload)),
    }
