from __future__ import annotations

import importlib
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Mapping

from astropy.coordinates import SkyCoord
from astropy.io import fits
import astropy.units as u

from grandgousier.archive.manifest import manifest_path_to_local, read_archive_manifest_csv
from grandgousier.archive.stage1_contract import (
    ArchiveRequest,
    ArchiveSourceDescriptor,
    AssetProvider,
    AssetRef,
    LocalAsset,
    MetadataProvider,
    ObservationRecord,
    ProductRef,
    ProductStore,
    make_source_observation_uid,
)
from grandgousier.identity.observation import (
    ObservationIdentityError,
    ObservationIdentityMissing,
    build_frame_observation_identity,
)
from grandgousier.instruments.base import InstrumentProfile
from grandgousier.io.fits_header import header_to_jsonable
from grandgousier.io.parsing import parse_datetime, parse_dec_deg, parse_ra_deg, to_float, to_int
from grandgousier.resources import instrument_profile_path

_PROFILE_FILES = {"MASTER": "master.yaml", "RoboPhot": "robophot.yaml"}
_SECRET_KEY_PARTS = ("password", "secret", "token", "credential", "database_url")


def _public_mapping(value: Any) -> Any:
    """Return a recursively secret-safe public representation of adapter config."""
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            key_fold = key_text.casefold()
            if any(part in key_fold for part in _SECRET_KEY_PARTS):
                result[key_text] = "<configured>" if item is not None and item != "" else None
            elif isinstance(item, str) and "://" in item and "@" in item:
                result[key_text] = "<configured>"
            else:
                result[key_text] = _public_mapping(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_public_mapping(item) for item in value]
    return value


def load_instrument_profile(value: str | Path) -> InstrumentProfile:
    text = str(value).strip()
    if text in _PROFILE_FILES:
        return InstrumentProfile.from_yaml(instrument_profile_path(_PROFILE_FILES[text]))
    path = Path(value)
    if not path.is_file():
        raise ValueError(
            f"Unknown instrument profile {value!r}; use MASTER, RoboPhot or a YAML path"
        )
    return InstrumentProfile.from_yaml(path)


def _iso_datetime(value: Any) -> str | None:
    if value in {None, ""}:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    parsed = parse_datetime(value)
    return parsed.isoformat() if parsed is not None else str(value).strip() or None


def _optional_float(value: Any) -> float | None:
    result = to_float(value)
    return result if result is not None and math.isfinite(result) else None


def _identity_fields(
    *,
    source_id: str,
    instrument: str,
    camera: str | None,
    date_obs: Any,
    header: Mapping[str, Any],
) -> dict[str, Any]:
    """Reuse the existing P4 identity implementation and add source namespace identity."""
    try:
        identity = build_frame_observation_identity(
            SimpleNamespace(
                instrument=instrument,
                camera=camera,
                date_obs=parse_datetime(date_obs),
                header_json=dict(header),
            )
        )
    except ObservationIdentityMissing as exc:
        return {
            "identity_status": "MISSING",
            "identity_detail": str(exc),
            "native_observation_id": None,
            "source_observation_uid": None,
            "identity_scheme": None,
            "p4_native_identity": None,
            "observation_uid": None,
        }
    except ObservationIdentityError as exc:
        return {
            "identity_status": "INVALID",
            "identity_detail": str(exc),
            "native_observation_id": None,
            "source_observation_uid": None,
            "identity_scheme": None,
            "p4_native_identity": None,
            "observation_uid": None,
        }
    native = identity.native_identity
    return {
        "identity_status": "READY",
        "identity_detail": None,
        "native_observation_id": native,
        "source_observation_uid": make_source_observation_uid(source_id, native),
        "identity_scheme": identity.identity_scheme,
        "p4_native_identity": identity.native_identity,
        "observation_uid": identity.observation_uid,
    }


def _request_matches(record: ObservationRecord, request: ArchiveRequest) -> bool:
    if request.filters and (record.filter_name or "") not in set(request.filters):
        return False
    if request.object_name:
        if (record.object_name or "").strip().casefold() != request.object_name.strip().casefold():
            return False
    if request.datetime_start and record.observed_at:
        start = parse_datetime(request.datetime_start)
        current = parse_datetime(record.observed_at)
        if start is not None and current is not None and current < start:
            return False
    if request.datetime_end and record.observed_at:
        end = parse_datetime(request.datetime_end)
        current = parse_datetime(record.observed_at)
        if end is not None and current is not None and current >= end:
            return False
    if request.ra_deg is not None:
        if record.ra_deg is None or record.dec_deg is None:
            return False
        radius = float(request.radius_deg or 0.0)
        if _angular_separation_deg(
            float(request.ra_deg),
            float(request.dec_deg),
            float(record.ra_deg),
            float(record.dec_deg),
        ) > radius:
            return False
    return True


def _angular_separation_deg(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
    ra1r, dec1r = math.radians(ra1), math.radians(dec1)
    ra2r, dec2r = math.radians(ra2), math.radians(dec2)
    cosine = (
        math.sin(dec1r) * math.sin(dec2r)
        + math.cos(dec1r) * math.cos(dec2r) * math.cos(ra1r - ra2r)
    )
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))


def _normalize_relative_locator(path: Path, root: Path) -> tuple[str, str]:
    resolved = path.expanduser().resolve(strict=False)
    root_resolved = root.expanduser().resolve(strict=False)
    try:
        relative = resolved.relative_to(root_resolved)
    except ValueError:
        return "filesystem-absolute", str(resolved)
    return "filesystem", relative.as_posix()


def _asset_from_header_path(
    *,
    source_id: str,
    role: str,
    value: Any,
    root: Path,
) -> AssetRef | None:
    text = str(value or "").strip()
    if not text:
        return None
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = root / PurePosixPath(text.replace("\\", "/"))
    backend, locator = _normalize_relative_locator(candidate, root)
    return AssetRef(source_id=source_id, role=role, backend=backend, locator=locator)


class FitsFilesystemMetadataProvider(MetadataProvider):
    """Metadata-only FITS discovery adapter; search reads headers, never pixel arrays."""

    def __init__(
        self,
        *,
        source_id: str,
        root: str | Path,
        profile: InstrumentProfile,
        recursive: bool = True,
        science_asset_role: str = "processed",
        required_asset_roles: Iterable[str] | None = None,
    ) -> None:
        self.source_id = source_id
        self.root = Path(root)
        self.profile = profile
        self.recursive = bool(recursive)
        if science_asset_role not in {"science_raw", "processed"}:
            raise ValueError("science_asset_role must be science_raw or processed")
        self.science_asset_role = science_asset_role
        self.required_asset_roles = tuple(required_asset_roles or ())

    def _paths(self) -> list[Path]:
        if not self.root.is_dir():
            raise ValueError(f"FITS source root does not exist: {self.root}")
        iterator = self.root.rglob("*") if self.recursive else self.root.glob("*")
        endings = (".fits", ".fit", ".fts", ".fits.gz", ".fit.gz", ".fts.gz")
        return sorted(
            path
            for path in iterator
            if path.is_file() and path.name.casefold().endswith(endings)
        )

    def search(self, request: ArchiveRequest) -> Iterable[ObservationRecord]:
        emitted = 0
        for path in self._paths():
            header = fits.getheader(path, ext=0)
            header_json = header_to_jsonable(header)
            frame_type = self.profile.normalize_image_type(
                self.profile.get_header_value(header, "image_type")
            )
            camera = _text_or_none(self.profile.get_header_value(header, "camera"))
            date_obs = self.profile.get_header_value(header, "date_obs")
            identity = _identity_fields(
                source_id=self.source_id,
                instrument=self.profile.name,
                camera=camera,
                date_obs=date_obs,
                header=header_json,
            )
            backend, locator = _normalize_relative_locator(path, self.root)
            if frame_type == "science":
                primary_role = self.science_asset_role
            elif frame_type in {"dark", "flat", "bias"}:
                primary_role = frame_type
            else:
                primary_role = "source_file"
            assets: list[AssetRef] = [
                AssetRef(
                    source_id=self.source_id,
                    role=primary_role,
                    backend=backend,
                    locator=locator,
                    metadata={"header_embedded": True},
                )
            ]
            if frame_type == "science":
                for role, field_name in (("dark", "dark_path"), ("flat", "flat_path")):
                    ref = _asset_from_header_path(
                        source_id=self.source_id,
                        role=role,
                        value=self.profile.get_header_value(header, field_name),
                        root=self.root,
                    )
                    if ref is not None:
                        assets.append(ref)

            record = ObservationRecord(
                source_id=self.source_id,
                instrument=self.profile.name,
                frame_type=frame_type,
                input_state="processed" if self.science_asset_role == "processed" else "raw",
                site=_text_or_none(self.profile.get_header_value(header, "site")),
                tube=_text_or_none(self.profile.get_header_value(header, "tube")),
                camera=camera,
                filter_name=self.profile.normalize_filter(
                    self.profile.get_header_value(header, "filter")
                ),
                object_name=_text_or_none(self.profile.get_header_value(header, "object")),
                observed_at=_iso_datetime(date_obs),
                exptime=_optional_float(self.profile.get_header_value(header, "exptime")),
                ra_deg=parse_ra_deg(self.profile.get_header_value(header, "ra_center")),
                dec_deg=parse_dec_deg(self.profile.get_header_value(header, "dec_center")),
                ccd_temp=_optional_float(self.profile.get_header_value(header, "ccd_temp")),
                binning_x=to_int(self.profile.get_header_value(header, "binning_x")),
                binning_y=to_int(self.profile.get_header_value(header, "binning_y")),
                required_asset_roles=(
                    self.required_asset_roles if frame_type == "science" else (primary_role,)
                ),
                assets=tuple(assets),
                metadata={"source_locator": locator},
                **identity,
            )
            if not _request_matches(record, request):
                continue
            yield record
            emitted += 1
            if request.limit is not None and emitted >= request.limit:
                break


class MasterManifestMetadataProvider(MetadataProvider):
    """Canonical MASTER adapter over the existing prepared inventory/cache contract."""

    def __init__(
        self,
        *,
        source_id: str,
        manifest: str | Path,
        profile: InstrumentProfile,
        cache_root: str | Path | None = None,
    ) -> None:
        self.source_id = source_id
        self.manifest = Path(manifest)
        self.profile = profile
        self.cache_root = Path(cache_root) if cache_root is not None else None

    def _cached_header(self, row: Mapping[str, Any]) -> Mapping[str, Any] | None:
        if self.cache_root is None or not row.get("header_path"):
            return None
        path = manifest_path_to_local(str(row["header_path"]), self.cache_root)
        if not path.is_file():
            return None
        return header_to_jsonable(fits.getheader(path, ext=0))

    def search(self, request: ArchiveRequest) -> Iterable[ObservationRecord]:
        if not self.manifest.is_file():
            raise ValueError(f"MASTER inventory does not exist: {self.manifest}")
        rows = read_archive_manifest_csv(self.manifest)
        emitted = 0
        for row in rows:
            cached_header = self._cached_header(row)
            uniqnum = _first_nonempty(row.get("uniqnum"), row.get("UNIQNUM"))
            if uniqnum is None and cached_header is not None:
                uniqnum = _first_nonempty(cached_header.get("UNIQNUM"))
            header_for_identity: dict[str, Any] = {}
            if uniqnum is not None:
                header_for_identity["UNIQNUM"] = uniqnum
            camera = _first_nonempty(row.get("camera"), row.get("id_cam"))
            if camera is None and cached_header is not None:
                camera = _first_nonempty(cached_header.get("ID_CAM"), cached_header.get("CAMERA"))
            date_obs = _first_nonempty(row.get("datetime"), row.get("date_obs"))
            identity = _identity_fields(
                source_id=self.source_id,
                instrument="MASTER",
                camera=_text_or_none(camera),
                date_obs=date_obs,
                header=header_for_identity,
            )
            assets: list[AssetRef] = []
            for role, key in (
                ("science_raw", "raw_path"),
                ("processing_header", "header_path"),
                ("dark", "dark_path"),
                ("flat", "flat_path"),
            ):
                locator = str(row.get(key) or "").strip()
                if locator:
                    assets.append(
                        AssetRef(
                            source_id=self.source_id,
                            role=role,
                            backend="master-cache",
                            locator=locator.replace("\\", "/"),
                        )
                    )
            coord_ra, coord_dec = _master_coord_pair(row)
            record = ObservationRecord(
                source_id=self.source_id,
                instrument="MASTER",
                frame_type="science",
                input_state="raw",
                site=_text_or_none(row.get("site")),
                tube=_text_or_none(row.get("tube")),
                camera=_text_or_none(camera),
                filter_name=self.profile.normalize_filter(row.get("filter")),
                object_name=_text_or_none(row.get("object_name") or row.get("object")),
                observed_at=_iso_datetime(date_obs),
                exptime=_optional_float(row.get("exptime")),
                ra_deg=coord_ra,
                dec_deg=coord_dec,
                ccd_temp=_optional_float(row.get("ccd_temp")),
                binning_x=to_int(row.get("binning_x")),
                binning_y=to_int(row.get("binning_y")),
                required_asset_roles=("science_raw", "processing_header", "dark", "flat"),
                assets=tuple(assets),
                metadata={
                    "processed_frame_id": _text_or_none(row.get("processed_frame_id")),
                    "raw_id": _text_or_none(row.get("raw_id")),
                },
                **identity,
            )
            if not _request_matches(record, request):
                continue
            yield record
            emitted += 1
            if request.limit is not None and emitted >= request.limit:
                break


class FilesystemAssetProvider(AssetProvider):
    def __init__(self, *, source_id: str, root: str | Path) -> None:
        self.source_id = source_id
        self.root = Path(root)

    def materialize(self, ref: AssetRef, destination: str | Path | None = None) -> LocalAsset:
        del destination
        if ref.source_id != self.source_id:
            raise ValueError(f"Asset source mismatch: {ref.source_id!r} != {self.source_id!r}")
        if ref.backend == "filesystem":
            path = self.root.joinpath(*PurePosixPath(ref.locator).parts)
        elif ref.backend == "filesystem-absolute":
            path = Path(ref.locator)
        else:
            raise ValueError(f"Unsupported filesystem asset backend: {ref.backend!r}")
        if not path.is_file():
            raise FileNotFoundError(path)
        return LocalAsset(ref=ref, path=path)


class MasterCacheAssetProvider(AssetProvider):
    """Resolve MASTER assets from partial local caches and fetch only missing files.

    Remote transport deliberately stays outside the public package.  When a
    selected asset is absent locally, the full repository checkout may provide
    ``internal_tools.master_remote``; it is imported lazily only from
    ``materialize()``.  Metadata-only planning therefore never opens a network
    connection.
    """

    def __init__(
        self,
        *,
        source_id: str,
        cache_root: str | Path,
        search_roots: Iterable[str | Path] = (),
        fetch_missing: bool = True,
        retries: int = 4,
        timeout_connect: float = 15.0,
        timeout_read: float = 180.0,
    ) -> None:
        self.source_id = source_id
        self.cache_root = Path(cache_root)
        roots: list[Path] = []
        for value in (self.cache_root, *tuple(Path(root) for root in search_roots)):
            if value not in roots:
                roots.append(value)
        self.search_roots = tuple(roots)
        self.fetch_missing = bool(fetch_missing)
        self.retries = max(0, int(retries))
        self.timeout_connect = float(timeout_connect)
        self.timeout_read = float(timeout_read)
        self._session = None
        self._transport = None

    def _existing_path(self, locator: str) -> Path | None:
        for root in self.search_roots:
            candidate = manifest_path_to_local(locator, root)
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
        return None

    def _repository_transport(self):
        if self._transport is not None:
            return self._transport
        try:
            module = importlib.import_module("internal_tools.master_remote")
        except ModuleNotFoundError as exc:
            if exc.name not in {"internal_tools", "internal_tools.master_remote"}:
                raise
            raise RuntimeError(
                "MASTER asset is absent from local caches and the repository-local "
                "fetch transport is unavailable. Run from a full Grandgousier checkout "
                "or prefetch the selected assets with master-archive-fetch."
            ) from exc
        required = (
            "MasterFetchTask",
            "download_master_task",
            "get_master_http_base_url",
            "make_master_http_session",
            "master_remote_path_to_url",
            "resolve_master_http_credentials",
        )
        missing = [name for name in required if not hasattr(module, name)]
        if missing:
            raise RuntimeError(
                "Repository-local MASTER fetch transport is incomplete: " + ", ".join(missing)
            )
        self._transport = module
        return module

    def _http_session(self):
        if self._session is not None:
            return self._session
        transport = self._repository_transport()
        user, password = transport.resolve_master_http_credentials()
        self._session = transport.make_master_http_session(
            user=user, password=password, retries=self.retries
        )
        return self._session

    def _fetch_missing(self, ref: AssetRef) -> Path:
        transport = self._repository_transport()
        url = transport.master_remote_path_to_url(ref.locator)
        if not url:
            raise RuntimeError(f"Cannot map MASTER asset to repository fetch URL: {ref.locator!r}")
        target = manifest_path_to_local(ref.locator, self.cache_root)
        task = transport.MasterFetchTask(
            remote_path=ref.locator,
            url=url,
            local_path=target,
            roles=(ref.role,),
        )
        result = transport.download_master_task(
            task,
            session=self._http_session(),
            timeout_connect=self.timeout_connect,
            timeout_read=self.timeout_read,
            overwrite=False,
            checksum=False,
        )
        if result.status == "FAILED" or not target.is_file() or target.stat().st_size <= 0:
            detail = result.error or f"download status={result.status}"
            raise RuntimeError(
                f"Failed to materialize MASTER asset {ref.role} {ref.locator}: {detail}"
            )
        return target

    def materialize(self, ref: AssetRef, destination: str | Path | None = None) -> LocalAsset:
        del destination
        if ref.source_id != self.source_id or ref.backend != "master-cache":
            raise ValueError("Asset is not owned by this MASTER cache provider")
        path = self._existing_path(ref.locator)
        if path is None:
            primary = manifest_path_to_local(ref.locator, self.cache_root)
            if not self.fetch_missing:
                raise FileNotFoundError(
                    f"MASTER asset is not present in configured local caches: {primary}"
                )
            path = self._fetch_missing(ref)
        return LocalAsset(ref=ref, path=path)

    def close(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            close = getattr(session, "close", None)
            if callable(close):
                close()


class FilesystemProductStore(ProductStore):
    def __init__(self, *, root: str | Path, store_id: str = "default") -> None:
        self.root = Path(root)
        self.store_id = str(store_id).strip() or "default"

    @property
    def public_descriptor(self) -> Mapping[str, Any]:
        return {"store_id": self.store_id, "backend": "filesystem"}

    def plan_product(self, observation: ObservationRecord) -> ProductRef:
        if observation.identity_status != "READY" or not observation.source_observation_uid:
            raise ValueError("Cannot plan a product without stable source observation identity")
        date_parts = ("undated",)
        if observation.observed_at:
            parsed = parse_datetime(observation.observed_at)
            if parsed is not None:
                date_parts = (f"{parsed.year:04d}", f"{parsed.month:02d}", f"{parsed.day:02d}")
        source_token = _safe_path_token(observation.source_id)
        instrument_token = _safe_path_token(observation.instrument)
        locator = PurePosixPath(
            source_token,
            instrument_token,
            *date_parts,
            f"{observation.source_observation_uid}_processed.fits",
        ).as_posix()
        return ProductRef(store_id=self.store_id, backend="filesystem", locator=locator)

    def resolve(self, ref: ProductRef) -> Path:
        if ref.store_id != self.store_id or ref.backend != "filesystem":
            raise ValueError("Product ref is not owned by this filesystem store")
        return self.root.joinpath(*PurePosixPath(ref.locator).parts)


@dataclass(frozen=True)
class ArchiveSourceRuntime:
    descriptor: ArchiveSourceDescriptor
    metadata_provider: MetadataProvider
    asset_provider: AssetProvider


SourceFactory = Callable[[str, Mapping[str, Any]], ArchiveSourceRuntime]
_SOURCE_FACTORIES: dict[str, SourceFactory] = {}


def register_archive_source_adapter(name: str, factory: SourceFactory) -> None:
    key = str(name).strip().casefold()
    if not key:
        raise ValueError("archive source adapter name must not be empty")
    _SOURCE_FACTORIES[key] = factory


def build_archive_source_runtime(payload: Mapping[str, Any], source_id: str) -> ArchiveSourceRuntime:
    archives = payload.get("archives")
    if not isinstance(archives, Mapping):
        raise ValueError("Grandgousier config requires an archives mapping for stage1-plan")
    raw = archives.get(source_id)
    if not isinstance(raw, Mapping):
        raise ValueError(f"Archive source {source_id!r} is not configured")
    adapter = str(raw.get("adapter") or "").strip().casefold()
    factory = _SOURCE_FACTORIES.get(adapter)
    if factory is None:
        raise ValueError(
            f"Unsupported archive adapter {adapter!r}; available={sorted(_SOURCE_FACTORIES)}"
        )
    return factory(source_id, raw)


def build_product_store(payload: Mapping[str, Any]) -> ProductStore:
    products = payload.get("products")
    if not isinstance(products, Mapping):
        raise ValueError("Grandgousier config requires a products mapping for stage1-plan")
    backend = str(products.get("backend") or "").strip().casefold()
    if backend != "filesystem":
        raise ValueError("0.6.0 foundation currently supports products.backend=filesystem")
    root = products.get("root")
    if root in {None, ""}:
        raise ValueError("products.root is required for filesystem ProductStore")
    return FilesystemProductStore(
        root=str(root), store_id=str(products.get("store_id") or "default")
    )


def _fits_factory(source_id: str, raw: Mapping[str, Any]) -> ArchiveSourceRuntime:
    profile_value = raw.get("instrument_profile")
    if profile_value in {None, ""}:
        raise ValueError(f"archives.{source_id}.instrument_profile is required")
    profile = load_instrument_profile(str(profile_value))
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), Mapping) else {}
    assets = raw.get("assets") if isinstance(raw.get("assets"), Mapping) else {}
    metadata_backend = str(metadata.get("backend") or "fits-headers").strip().casefold()
    asset_backend = str(assets.get("backend") or "filesystem").strip().casefold()
    if metadata_backend != "fits-headers":
        raise ValueError("fits-filesystem adapter requires metadata.backend=fits-headers")
    if asset_backend != "filesystem":
        raise ValueError("fits-filesystem adapter requires assets.backend=filesystem")
    root = metadata.get("root", assets.get("root"))
    if root in {None, ""}:
        raise ValueError(f"archives.{source_id}.metadata.root is required")
    provider = FitsFilesystemMetadataProvider(
        source_id=source_id,
        root=str(root),
        profile=profile,
        recursive=bool(metadata.get("recursive", True)),
        science_asset_role=str(metadata.get("science_asset_role") or "processed"),
        required_asset_roles=tuple(metadata.get("required_asset_roles") or ()),
    )
    asset_provider = FilesystemAssetProvider(source_id=source_id, root=str(root))
    descriptor = ArchiveSourceDescriptor(
        source_id=source_id,
        adapter="fits-filesystem",
        instrument_profile=profile.name,
        metadata_backend="fits-headers",
        asset_backend="filesystem",
        public_config=_public_mapping(raw),
    )
    return ArchiveSourceRuntime(descriptor, provider, asset_provider)


def _master_manifest_factory(source_id: str, raw: Mapping[str, Any]) -> ArchiveSourceRuntime:
    profile = load_instrument_profile(str(raw.get("instrument_profile") or "MASTER"))
    if profile.name != "MASTER":
        raise ValueError("master-manifest adapter requires the MASTER instrument profile")
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), Mapping) else {}
    assets = raw.get("assets") if isinstance(raw.get("assets"), Mapping) else {}
    metadata_backend = str(metadata.get("backend") or "prepared-inventory").strip().casefold()
    asset_backend = str(assets.get("backend") or "master-cache").strip().casefold()
    if metadata_backend != "prepared-inventory":
        raise ValueError("master-manifest adapter requires metadata.backend=prepared-inventory")
    if asset_backend != "master-cache":
        raise ValueError("master-manifest adapter requires assets.backend=master-cache")
    manifest = metadata.get("manifest")
    if manifest in {None, ""}:
        raise ValueError(f"archives.{source_id}.metadata.manifest is required")
    metadata_cache_root = metadata.get("cache_root") or assets.get("cache_root")
    asset_cache_root = assets.get("cache_root") or metadata_cache_root
    if asset_cache_root in {None, ""}:
        raise ValueError(f"archives.{source_id}.assets.cache_root is required")
    configured_search_roots = assets.get("search_roots") or ()
    if isinstance(configured_search_roots, (str, Path)):
        configured_search_roots = (configured_search_roots,)
    elif not isinstance(configured_search_roots, (list, tuple)):
        raise ValueError(f"archives.{source_id}.assets.search_roots must be a list of paths")
    search_roots = [str(value) for value in configured_search_roots if str(value).strip()]
    if metadata_cache_root not in {None, ""}:
        search_roots.append(str(metadata_cache_root))
    provider = MasterManifestMetadataProvider(
        source_id=source_id,
        manifest=str(manifest),
        profile=profile,
        cache_root=str(metadata_cache_root) if metadata_cache_root not in {None, ""} else None,
    )
    asset_provider = MasterCacheAssetProvider(
        source_id=source_id,
        cache_root=str(asset_cache_root),
        search_roots=search_roots,
        fetch_missing=bool(assets.get("fetch_missing", True)),
        retries=int(assets.get("retries", 4)),
        timeout_connect=float(assets.get("timeout_connect", 15.0)),
        timeout_read=float(assets.get("timeout_read", 180.0)),
    )
    descriptor = ArchiveSourceDescriptor(
        source_id=source_id,
        adapter="master-manifest",
        instrument_profile="MASTER",
        metadata_backend="prepared-inventory",
        asset_backend="master-cache",
        public_config=_public_mapping(raw),
    )
    return ArchiveSourceRuntime(descriptor, provider, asset_provider)


def _master_coord_pair(row: Mapping[str, Any]) -> tuple[float | None, float | None]:
    ra = _optional_float(row.get("ra_deg"))
    dec = _optional_float(row.get("dec_deg"))
    if ra is not None and dec is not None:
        return ra, dec
    coord = str(row.get("coord2000") or "").strip()
    if not coord:
        return ra, dec
    try:
        parsed = SkyCoord(coord, unit=(u.hourangle, u.deg), frame="icrs")
    except Exception:
        return ra, dec
    return float(parsed.ra.deg), float(parsed.dec.deg)


def _safe_path_token(value: Any) -> str:
    text = str(value or "unknown").strip()
    result = "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in text)
    return result.strip("._") or "unknown"


def _text_or_none(value: Any) -> str | None:
    if value in {None, ""}:
        return None
    text = str(value).strip()
    return text or None


def _first_nonempty(*values: Any) -> Any:
    for value in values:
        if value not in {None, ""} and str(value).strip():
            return value
    return None


register_archive_source_adapter("fits-filesystem", _fits_factory)
register_archive_source_adapter("master-manifest", _master_manifest_factory)


__all__ = [
    "ArchiveSourceRuntime",
    "FilesystemAssetProvider",
    "FilesystemProductStore",
    "FitsFilesystemMetadataProvider",
    "MasterCacheAssetProvider",
    "MasterManifestMetadataProvider",
    "build_archive_source_runtime",
    "build_product_store",
    "load_instrument_profile",
    "register_archive_source_adapter",
]
