from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from astropy.io.fits import Header


@dataclass(frozen=True)
class InstrumentProfile:
    name: str
    version: str
    header_aliases: dict[str, list[str]]
    filter_aliases: dict[str, str]
    image_type_aliases: dict[str, str]
    saturation_level_adu: float | None = None
    pixel_scale_arcsec: float | None = None
    nominal_gain_e_per_adu: float | None = None
    nominal_read_noise_e: float | None = None
    raw_config: dict[str, Any] | None = None

    @classmethod
    def from_yaml(cls, path: str | Path) -> "InstrumentProfile":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls(
            name=data["name"],
            version=str(data.get("version", "0")),
            header_aliases=data.get("header_aliases", {}),
            filter_aliases={str(k): str(v) for k, v in data.get("filter_aliases", {}).items()},
            image_type_aliases={
                str(k).lower(): str(v).lower() for k, v in data.get("image_type_aliases", {}).items()
            },
            saturation_level_adu=data.get("saturation_level_adu"),
            pixel_scale_arcsec=data.get("pixel_scale_arcsec"),
            nominal_gain_e_per_adu=data.get("nominal_gain_e_per_adu"),
            nominal_read_noise_e=data.get("nominal_read_noise_e"),
            raw_config=data,
        )

    def get_header_value(self, header: Header, field: str, default: Any = None) -> Any:
        for key in self.header_aliases.get(field, []):
            if key in header:
                value = header.get(key)
                if value not in (None, ""):
                    return value
        return default

    def normalize_filter(self, value: Any) -> str | None:
        if value is None:
            return None
        raw = str(value).strip()
        return self.filter_aliases.get(raw, self.filter_aliases.get(raw.upper(), raw))

    def normalize_image_type(self, value: Any) -> str:
        if value is None:
            return "unknown"
        raw = str(value).strip().lower()
        return self.image_type_aliases.get(raw, raw if raw in {"science", "bias", "dark", "flat"} else "unknown")
