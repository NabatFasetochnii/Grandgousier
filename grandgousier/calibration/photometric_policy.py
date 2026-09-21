from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from grandgousier.instruments.base import InstrumentProfile


@dataclass(frozen=True)
class ColorCalibrationPolicy:
    enabled: bool
    reference_catalog_name: str | None = None
    photometric_system: str | None = None
    reference_band: str | None = None
    color_index: str | None = None
    require_bands: tuple[str, ...] = ()
    min_measurements_per_object: int = 5
    min_stars_per_frame: int = 20
    max_measurement_mag_err: float | None = None
    max_reference_mag_err: float | None = None
    min_reference_mag: float | None = None
    max_reference_mag: float | None = None
    min_color: float | None = None
    max_color: float | None = None
    clip_sigma: float = 4.0
    n_iterations: int = 5
    min_calibrator_objects: int = 20
    min_color_span: float = 0.3
    max_transform_residual_mad: float | None = 0.10
    max_pivot_color_fraction: float | None = 0.25
    max_shape_metric_delta: float = 1e-10
    min_color_term_significance: float = 2.0
    min_color_model_bic_improvement: float = 6.0
    min_color_model_mad_improvement: float = 0.0
    skip_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["require_bands"] = list(self.require_bands)
        return payload


def resolve_color_calibration_policy(
    profile: "InstrumentProfile",
    filter_name: str | None,
    *,
    enabled_override: bool | None = None,
    reference_catalog_name: str | None = None,
    reference_band: str | None = None,
    color_index: str | None = None,
) -> ColorCalibrationPolicy:
    """Resolve the final reference-color calibration policy for one filter.

    Instrument profiles may define a ``photometric_calibration`` section with
    common defaults and per-filter overrides. Explicit CLI/API values win over
    the profile. A disabled or incomplete policy is returned as data instead of
    raising, so archive orchestration can report why the color stage was skipped.
    """

    root = (profile.raw_config or {}).get("photometric_calibration") or {}
    normalized_filter = profile.normalize_filter(filter_name) if filter_name is not None else None
    filters = root.get("filters") or {}
    per_filter = {}
    if normalized_filter is not None:
        per_filter = filters.get(normalized_filter) or filters.get(str(normalized_filter).lower()) or {}

    root_enabled = bool(root.get("enabled", False))
    filter_enabled = per_filter.get("enabled")
    enabled = bool(root_enabled if filter_enabled is None else filter_enabled)
    if enabled_override is not None:
        enabled = bool(enabled_override)

    def pick(key: str, explicit: Any = None, default: Any = None) -> Any:
        if explicit is not None:
            return explicit
        if key in per_filter:
            return per_filter.get(key)
        if key in root:
            return root.get(key)
        return default

    ref_catalog = _text_or_none(pick("reference_catalog_name", reference_catalog_name))
    ref_band = _text_or_none(pick("reference_band", reference_band))
    color = _text_or_none(pick("color_index", color_index))
    require_bands = tuple(str(v).strip() for v in (pick("require_bands", default=[]) or []) if str(v).strip())

    skip_reason = None
    if not enabled:
        skip_reason = "disabled"
    elif normalized_filter is None:
        enabled = False
        skip_reason = "filter is unknown"
    elif not ref_catalog:
        enabled = False
        skip_reason = "reference_catalog_name is not configured"
    elif not ref_band:
        enabled = False
        skip_reason = "reference_band is not configured"
    elif not color:
        enabled = False
        skip_reason = "color_index is not configured"

    return ColorCalibrationPolicy(
        enabled=enabled,
        reference_catalog_name=ref_catalog,
        photometric_system=_text_or_none(pick("photometric_system")),
        reference_band=ref_band,
        color_index=color,
        require_bands=require_bands,
        min_measurements_per_object=max(1, int(pick("min_measurements_per_object", default=5))),
        min_stars_per_frame=max(1, int(pick("min_stars_per_frame", default=20))),
        max_measurement_mag_err=_float_or_none(pick("max_measurement_mag_err")),
        max_reference_mag_err=_float_or_none(pick("max_reference_mag_err")),
        min_reference_mag=_float_or_none(pick("min_reference_mag")),
        max_reference_mag=_float_or_none(pick("max_reference_mag")),
        min_color=_float_or_none(pick("min_color")),
        max_color=_float_or_none(pick("max_color")),
        clip_sigma=float(pick("clip_sigma", default=4.0)),
        n_iterations=max(1, int(pick("n_iterations", default=5))),
        min_calibrator_objects=max(3, int(pick("min_calibrator_objects", default=20))),
        min_color_span=max(0.0, float(pick("min_color_span", default=0.3))),
        max_transform_residual_mad=_float_or_none(pick("max_transform_residual_mad", default=0.10)),
        max_pivot_color_fraction=_float_or_none(pick("max_pivot_color_fraction", default=0.25)),
        max_shape_metric_delta=max(0.0, float(pick("max_shape_metric_delta", default=1e-10))),
        min_color_term_significance=max(0.0, float(pick("min_color_term_significance", default=2.0))),
        min_color_model_bic_improvement=max(0.0, float(pick("min_color_model_bic_improvement", default=6.0))),
        min_color_model_mad_improvement=float(pick("min_color_model_mad_improvement", default=0.0)),
        skip_reason=skip_reason,
    )


def identity_scoped_reference_catalog_name(
    provider_name: str,
    identity_catalog_name: str,
    *,
    max_length: int = 64,
) -> str:
    """Return a stable reference-catalog namespace for one identity catalog.

    Normalized ``reference_photometry_sources`` stores one identity-catalog
    match on each imported provider source. Re-importing a global Pan-STARRS
    namespace against another generated identity catalog would therefore
    overwrite the previous match. The scoped name keeps provider measurements
    idempotent for every identity catalog.
    """

    provider = _slug(provider_name) or "reference"
    identity = _slug(identity_catalog_name) or "identity"
    digest = hashlib.sha256(str(identity_catalog_name).encode("utf-8")).hexdigest()[:10]
    suffix = f"__id_{digest}"
    available = max(1, int(max_length) - len(provider) - len(suffix) - 2)
    identity = identity[:available].rstrip("_") or "identity"
    value = f"{provider}__{identity}{suffix}"
    return value[: int(max_length)]


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _slug(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", str(value).strip()).strip("_").lower()
