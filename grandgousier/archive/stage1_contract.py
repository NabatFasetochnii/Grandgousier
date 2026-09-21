from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence

STAGE1_PLAN_SCHEMA = "grandgousier.stage1.plan.v1"


def _clean_token(value: Any, *, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} must not be empty")
    return text


def make_source_observation_uid(source_id: str, native_observation_id: str) -> str:
    """Return a path-independent Stage-1 source identity."""
    source = _clean_token(source_id, name="source_id")
    native = _clean_token(native_observation_id, name="native_observation_id")
    payload = f"grandgousier-stage1-source|{source}|{native}".encode("utf-8")
    return "srcobs_" + hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class ArchiveRequest:
    source_id: str
    filters: tuple[str, ...] = ()
    datetime_start: str | None = None
    datetime_end: str | None = None
    object_name: str | None = None
    ra_deg: float | None = None
    dec_deg: float | None = None
    radius_deg: float | None = None
    limit: int | None = None

    def __post_init__(self) -> None:
        _clean_token(self.source_id, name="source_id")
        if self.limit is not None and self.limit <= 0:
            raise ValueError("limit must be positive")
        if (self.ra_deg is None) != (self.dec_deg is None):
            raise ValueError("ra_deg and dec_deg must be supplied together")
        if self.ra_deg is not None and not 0.0 <= float(self.ra_deg) < 360.0:
            raise ValueError("ra_deg must be in [0, 360)")
        if self.dec_deg is not None and not -90.0 <= float(self.dec_deg) <= 90.0:
            raise ValueError("dec_deg must be in [-90, 90]")
        if self.radius_deg is not None and float(self.radius_deg) <= 0.0:
            raise ValueError("radius_deg must be positive")
        if self.radius_deg is not None and self.ra_deg is None:
            raise ValueError("radius_deg requires ra_deg and dec_deg")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "filters": list(self.filters),
            "datetime_start": self.datetime_start,
            "datetime_end": self.datetime_end,
            "object_name": self.object_name,
            "ra_deg": self.ra_deg,
            "dec_deg": self.dec_deg,
            "radius_deg": self.radius_deg,
            "limit": self.limit,
        }


@dataclass(frozen=True, slots=True)
class ArchiveSourceDescriptor:
    source_id: str
    adapter: str
    instrument_profile: str
    metadata_backend: str
    asset_backend: str
    public_config: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _clean_token(self.source_id, name="source_id")
        _clean_token(self.adapter, name="adapter")
        _clean_token(self.instrument_profile, name="instrument_profile")
        _clean_token(self.metadata_backend, name="metadata_backend")
        _clean_token(self.asset_backend, name="asset_backend")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "adapter": self.adapter,
            "instrument_profile": self.instrument_profile,
            "metadata_backend": self.metadata_backend,
            "asset_backend": self.asset_backend,
            "config": dict(self.public_config),
        }


@dataclass(frozen=True, slots=True)
class AssetRef:
    source_id: str
    role: str
    backend: str
    locator: str
    checksum_sha256: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _clean_token(self.source_id, name="asset source_id")
        _clean_token(self.role, name="asset role")
        _clean_token(self.backend, name="asset backend")
        _clean_token(self.locator, name="asset locator")

    @property
    def ref_uid(self) -> str:
        payload = (
            f"grandgousier-stage1-asset|{self.source_id}|{self.backend}|{self.role}|{self.locator}"
        ).encode("utf-8")
        return "asset_" + hashlib.sha256(payload).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ref_uid": self.ref_uid,
            "source_id": self.source_id,
            "role": self.role,
            "backend": self.backend,
            "locator": self.locator,
        }
        if self.checksum_sha256:
            result["checksum_sha256"] = self.checksum_sha256
        if self.metadata:
            result["metadata"] = dict(self.metadata)
        return result


@dataclass(frozen=True, slots=True)
class ObservationRecord:
    source_id: str
    instrument: str
    frame_type: str
    input_state: str
    identity_status: str
    native_observation_id: str | None = None
    source_observation_uid: str | None = None
    identity_scheme: str | None = None
    p4_native_identity: str | None = None
    observation_uid: str | None = None
    identity_detail: str | None = None
    site: str | None = None
    tube: str | None = None
    camera: str | None = None
    filter_name: str | None = None
    object_name: str | None = None
    observed_at: str | None = None
    exptime: float | None = None
    ra_deg: float | None = None
    dec_deg: float | None = None
    ccd_temp: float | None = None
    binning_x: int | None = None
    binning_y: int | None = None
    required_asset_roles: tuple[str, ...] = ()
    assets: tuple[AssetRef, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _clean_token(self.source_id, name="observation source_id")
        _clean_token(self.instrument, name="instrument")
        _clean_token(self.frame_type, name="frame_type")
        _clean_token(self.input_state, name="input_state")
        status = _clean_token(self.identity_status, name="identity_status").upper()
        if status == "READY":
            native = _clean_token(self.native_observation_id, name="native_observation_id")
            expected = make_source_observation_uid(self.source_id, native)
            if self.source_observation_uid != expected:
                raise ValueError("source_observation_uid does not match source/native identity")
            _clean_token(self.identity_scheme, name="identity_scheme")
            _clean_token(self.p4_native_identity, name="p4_native_identity")
            _clean_token(self.observation_uid, name="observation_uid")
        for ref in self.assets:
            if ref.source_id != self.source_id:
                raise ValueError("observation asset source_id does not match observation source_id")

    def asset(self, role: str) -> AssetRef | None:
        for item in self.assets:
            if item.role == role:
                return item
        return None

    def assets_for_roles(self, roles: Iterable[str]) -> tuple[AssetRef, ...]:
        wanted = set(roles)
        return tuple(item for item in self.assets if item.role in wanted)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "instrument": self.instrument,
            "frame_type": self.frame_type,
            "input_state": self.input_state,
            "identity_status": self.identity_status,
            "native_observation_id": self.native_observation_id,
            "source_observation_uid": self.source_observation_uid,
            "identity_scheme": self.identity_scheme,
            "p4_native_identity": self.p4_native_identity,
            "observation_uid": self.observation_uid,
            "identity_detail": self.identity_detail,
            "site": self.site,
            "tube": self.tube,
            "camera": self.camera,
            "filter_name": self.filter_name,
            "object_name": self.object_name,
            "observed_at": self.observed_at,
            "exptime": self.exptime,
            "ra_deg": self.ra_deg,
            "dec_deg": self.dec_deg,
            "ccd_temp": self.ccd_temp,
            "binning_x": self.binning_x,
            "binning_y": self.binning_y,
            "required_asset_roles": list(self.required_asset_roles),
            "assets": [item.to_dict() for item in self.assets],
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CalibrationGroup:
    mode: str
    assets: tuple[AssetRef, ...]
    criteria: Mapping[str, Any] = field(default_factory=dict)
    explanation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "assets": [item.to_dict() for item in self.assets],
            "criteria": dict(self.criteria),
            "explanation": self.explanation,
        }


@dataclass(frozen=True, slots=True)
class ProductRef:
    store_id: str
    backend: str
    locator: str

    def __post_init__(self) -> None:
        _clean_token(self.store_id, name="store_id")
        _clean_token(self.backend, name="product backend")
        _clean_token(self.locator, name="product locator")

    def to_dict(self) -> dict[str, str]:
        return {"store_id": self.store_id, "backend": self.backend, "locator": self.locator}


@dataclass(frozen=True, slots=True)
class LocalAsset:
    ref: AssetRef
    path: Path


@dataclass(frozen=True, slots=True)
class CalibrationPlanItem:
    observation: ObservationRecord
    action: str
    status: str
    product: ProductRef | None
    calibration: CalibrationGroup
    missing_asset_roles: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation": self.observation.to_dict(),
            "action": self.action,
            "status": self.status,
            "product": self.product.to_dict() if self.product else None,
            "calibration": self.calibration.to_dict(),
            "missing_asset_roles": list(self.missing_asset_roles),
            "issues": list(self.issues),
        }


@dataclass(frozen=True, slots=True)
class CalibrationPlan:
    source: ArchiveSourceDescriptor
    request: ArchiveRequest
    product_store: Mapping[str, Any]
    items: tuple[CalibrationPlanItem, ...]
    discovered_records: int
    generated_at_utc: str
    schema: str = STAGE1_PLAN_SCHEMA

    @property
    def blocked_items(self) -> int:
        return sum(item.status != "READY" for item in self.items)

    @property
    def ready_items(self) -> int:
        return len(self.items) - self.blocked_items

    @property
    def status(self) -> str:
        if not self.items:
            return "EMPTY"
        return "READY" if self.blocked_items == 0 else "BLOCKED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "generated_at_utc": self.generated_at_utc,
            "status": self.status,
            "source": self.source.to_dict(),
            "request": self.request.to_dict(),
            "product_store": dict(self.product_store),
            "summary": {
                "discovered_records": self.discovered_records,
                "science_items": len(self.items),
                "ready_items": self.ready_items,
                "blocked_items": self.blocked_items,
            },
            "items": [item.to_dict() for item in self.items],
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent, sort_keys=False)

    @classmethod
    def now(
        cls,
        *,
        source: ArchiveSourceDescriptor,
        request: ArchiveRequest,
        product_store: Mapping[str, Any],
        items: Sequence[CalibrationPlanItem],
        discovered_records: int,
    ) -> "CalibrationPlan":
        stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return cls(
            source=source,
            request=request,
            product_store=product_store,
            items=tuple(items),
            discovered_records=int(discovered_records),
            generated_at_utc=stamp,
        )


class MetadataProvider(Protocol):
    def search(self, request: ArchiveRequest) -> Iterable[ObservationRecord]: ...


class AssetProvider(Protocol):
    def materialize(self, ref: AssetRef, destination: str | Path | None = None) -> LocalAsset: ...


class ProductStore(Protocol):
    @property
    def public_descriptor(self) -> Mapping[str, Any]: ...

    def plan_product(self, observation: ObservationRecord) -> ProductRef: ...

    def resolve(self, ref: ProductRef) -> Path: ...
