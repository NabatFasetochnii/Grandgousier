from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from grandgousier.config import field_archive_mapping, read_config_file

from grandgousier.orchestration.field_archive import (
    STAGE2_RECIPE_STAGE3_SCALE_V1,
    Stage3Policy,
    parse_stage3_policy,
)
from grandgousier.settings import get_settings

_FIELD_ARCHIVE_ENV_PREFIX = "GRANDGOUSIER_FIELD_ARCHIVE_"


def _parse_bool(value: Any, *, name: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().casefold()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean for {name}: {value!r}")


def _parse_int_list(value: Any, *, name: str) -> tuple[int, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ()
        if text.startswith("["):
            raw = json.loads(text)
        else:
            raw = [part.strip() for part in text.split(",") if part.strip()]
    else:
        raw = list(value)
    try:
        return tuple(int(item) for item in raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid integer list for {name}: {value!r}") from exc


def _parse_str_list(value: Any) -> tuple[str, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ()
        if text.startswith("["):
            raw = json.loads(text)
        else:
            raw = [part.strip() for part in text.split(",") if part.strip()]
    else:
        raw = list(value)
    return tuple(str(item).strip() for item in raw if str(item).strip())


def _parse_policies(value: Any) -> tuple[Stage3Policy, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ()
        if text.startswith("["):
            raw: Sequence[Any] = json.loads(text)
        else:
            raw = [part.strip() for part in text.split(",") if part.strip()]
    else:
        raw = list(value)

    policies: list[Stage3Policy] = []
    for item in raw:
        if isinstance(item, Stage3Policy):
            policies.append(item)
        elif isinstance(item, str):
            policies.append(parse_stage3_policy(item))
        elif isinstance(item, Mapping):
            filter_name = str(item.get("filter_name") or item.get("filter") or "").strip()
            min_common_value = item.get("min_common_objects", item.get("min_common"))
            if not filter_name or min_common_value in {None, ""}:
                raise ValueError(
                    "Stage-3 policy mappings require filter/filter_name and min_common/min_common_objects"
                )
            try:
                policy = Stage3Policy(
                    filter_name=filter_name,
                    min_common_objects=int(min_common_value),
                    reference_series_id=(
                        int(item["reference_series_id"])
                        if item.get("reference_series_id") not in {None, ""}
                        else None
                    ),
                    instrument_unit_id=(
                        int(item["instrument_unit_id"])
                        if item.get("instrument_unit_id") not in {None, ""}
                        else None
                    ),
                    min_points_per_object=int(item.get("min_points_per_object", 5)),
                    clip_sigma=float(item.get("clip_sigma", 4.0)),
                    max_clip_iterations=int(item.get("max_clip_iterations", 5)),
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid Stage-3 policy mapping: {item!r}") from exc
            if policy.min_common_objects < 1 or policy.min_points_per_object < 1:
                raise ValueError(f"Invalid Stage-3 policy thresholds: {item!r}")
            policies.append(policy)
        else:
            raise ValueError(f"Unsupported Stage-3 policy value: {item!r}")
    return tuple(policies)


def _env_mapping(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    env = environ if environ is not None else os.environ
    result: dict[str, Any] = {}
    names = {
        "processed_root": "PROCESSED_ROOT",
        "field_id": "FIELD_ID",
        "catalog_name": "CATALOG_NAME",
        "instrument_profile": "INSTRUMENT_PROFILE",
        "execute": "EXECUTE",
        "resume": "RESUME",
        "stage2_recipe": "STAGE2_RECIPE",
        "stage3_policies": "STAGE3_POLICIES",
        "control_object_ids": "CONTROL_OBJECT_IDS",
        "exclude_dates": "EXCLUDE_DATES",
        "include_dates": "INCLUDE_DATES",
        "out_root": "OUT_ROOT",
        "plot_max_points": "PLOT_MAX_POINTS",
    }
    for key, suffix in names.items():
        value = env.get(_FIELD_ARCHIVE_ENV_PREFIX + suffix)
        if value is not None:
            result[key] = value
    database_url = env.get("GRANDGOUSIER_DATABASE_URL")
    if database_url is not None:
        result["database_url"] = database_url
    return result


@dataclass(frozen=True, slots=True)
class FieldArchiveJobConfig:
    """Typed service-layer configuration for one M8 field-archive job.

    The scientific policy is still executed by ``run_field_archive_workflow``;
    this object only normalizes runtime/configuration inputs.
    """

    processed_root: str
    field_id: int
    catalog_name: str
    stage3_policies: tuple[Stage3Policy, ...]
    database_url: str | None = None
    control_object_ids: tuple[int, ...] = ()
    execute: bool = False
    resume: bool = True
    instrument_profile: str = "MASTER"
    stage2_recipe: str = STAGE2_RECIPE_STAGE3_SCALE_V1
    exclude_dates: tuple[str, ...] = ()
    include_dates: tuple[str, ...] = ()
    out_root: str = "reports/field_archive"
    plot_max_points: int = 200

    def __post_init__(self) -> None:
        if not str(self.processed_root).strip():
            raise ValueError("processed_root is required")
        if int(self.field_id) < 1:
            raise ValueError("field_id must be positive")
        if not str(self.catalog_name).strip():
            raise ValueError("catalog_name is required")
        if not self.stage3_policies:
            raise ValueError("At least one Stage-3 policy is required")
        if int(self.plot_max_points) < 1:
            raise ValueError("plot_max_points must be >= 1")
        profile = str(self.instrument_profile).strip()
        if profile.casefold() not in {"auto", "master", "robophot"}:
            raise ValueError("instrument_profile must be one of: auto, MASTER, RoboPhot")

    @classmethod
    def from_sources(
        cls,
        *,
        config_file: str | Path | None = None,
        overrides: Mapping[str, Any] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> "FieldArchiveJobConfig":
        """Load defaults < environment < YAML/JSON < explicit non-None overrides."""

        merged: dict[str, Any] = {
            "database_url": get_settings().database_url,
            "execute": False,
            "resume": True,
            "instrument_profile": "MASTER",
            "stage2_recipe": STAGE2_RECIPE_STAGE3_SCALE_V1,
            "control_object_ids": (),
            "exclude_dates": (),
            "include_dates": (),
            "out_root": "reports/field_archive",
            "plot_max_points": 200,
        }
        merged.update(_env_mapping(environ))
        if config_file is not None:
            merged.update(field_archive_mapping(read_config_file(config_file)))
        if overrides:
            merged.update({key: value for key, value in overrides.items() if value is not None})

        missing = []
        for key in ("processed_root", "field_id", "catalog_name", "stage3_policies"):
            value = merged.get(key)
            if value is None or value == "" or value == () or value == []:
                missing.append(key)
        if missing:
            raise ValueError(
                "Missing required field-archive job configuration: " + ", ".join(missing)
            )

        return cls(
            database_url=(str(merged["database_url"]) if merged.get("database_url") else None),
            processed_root=str(merged["processed_root"]),
            field_id=int(merged["field_id"]),
            catalog_name=str(merged["catalog_name"]),
            stage3_policies=_parse_policies(merged["stage3_policies"]),
            control_object_ids=_parse_int_list(merged.get("control_object_ids"), name="control_object_ids"),
            execute=_parse_bool(merged.get("execute", False), name="execute"),
            resume=_parse_bool(merged.get("resume", True), name="resume"),
            instrument_profile=str(merged.get("instrument_profile") or "MASTER"),
            stage2_recipe=str(merged.get("stage2_recipe") or STAGE2_RECIPE_STAGE3_SCALE_V1),
            exclude_dates=_parse_str_list(merged.get("exclude_dates")),
            include_dates=_parse_str_list(merged.get("include_dates")),
            out_root=str(merged.get("out_root") or "reports/field_archive"),
            plot_max_points=int(merged.get("plot_max_points", 200)),
        )

    def workflow_kwargs(self) -> dict[str, Any]:
        return {
            "database_url": self.database_url,
            "processed_root": self.processed_root,
            "field_id": self.field_id,
            "catalog_name": self.catalog_name,
            "policies": self.stage3_policies,
            "control_object_ids": self.control_object_ids,
            "execute": self.execute,
            "resume": self.resume,
            "instrument_profile": self.instrument_profile,
            "stage2_recipe": self.stage2_recipe,
            "exclude_dates": self.exclude_dates,
            "include_dates": self.include_dates,
            "out_root": self.out_root,
            "plot_max_points": self.plot_max_points,
        }

    def public_dict(self) -> dict[str, Any]:
        return {
            "database_url": "<configured>" if self.database_url else None,
            "processed_root": self.processed_root,
            "field_id": self.field_id,
            "catalog_name": self.catalog_name,
            "stage3_policies": [asdict(policy) for policy in self.stage3_policies],
            "control_object_ids": list(self.control_object_ids),
            "execute": self.execute,
            "resume": self.resume,
            "instrument_profile": self.instrument_profile,
            "stage2_recipe": self.stage2_recipe,
            "exclude_dates": list(self.exclude_dates),
            "include_dates": list(self.include_dates),
            "out_root": self.out_root,
            "plot_max_points": self.plot_max_points,
        }

    def fingerprint(self) -> str:
        private = {
            **self.public_dict(),
            "database_url": self.database_url,
        }
        payload = json.dumps(private, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
