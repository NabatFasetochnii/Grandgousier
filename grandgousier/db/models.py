from __future__ import annotations

import enum
import hashlib
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class LegacyBase(DeclarativeBase):
    """Frozen 0.6 compatibility mappings excluded from the clean 0.7 schema."""

    pass


class SchemaMigration(Base):
    """Ledger row for one schema migration known to the current codebase.

    ``state`` is APPLIED when the SQL migration was executed by the managed
    runner and BASELINED when an equivalent schema was validated/adopted (for
    example a pre-0.5.39.1 database or a fresh ORM-created schema).
    """

    __tablename__ = "schema_migrations"

    migration_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    filename: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="APPLIED")
    application_version: Mapped[str | None] = mapped_column(String(32))
    execution_ms: Mapped[float | None] = mapped_column(Float)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FrameStatus(str, enum.Enum):
    DISCOVERED = "DISCOVERED"
    HEADER_READ = "HEADER_READ"
    COPIED = "COPIED"
    CALIBRATED = "CALIBRATED"
    WCS_SOLVED = "WCS_SOLVED"
    PHOTOMETRY_DONE = "PHOTOMETRY_DONE"
    SELF_CAL_DONE = "SELF_CAL_DONE"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class FrameType(str, enum.Enum):
    SCIENCE = "science"
    BIAS = "bias"
    DARK = "dark"
    FLAT = "flat"
    TEST = "test"
    FOCUS = "focus"
    UNKNOWN = "unknown"


class ProcessingEventLevel(str, enum.Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class FileReferenceStatus(str, enum.Enum):
    """Status of a calibration or auxiliary file reference stored in a FITS header."""

    UNKNOWN = "UNKNOWN"
    MISSING_IN_HEADER = "MISSING_IN_HEADER"
    FROM_HEADER = "FROM_HEADER"
    LOCAL_FILE_EXISTS = "LOCAL_FILE_EXISTS"
    REMOTE_ONLY = "REMOTE_ONLY"
    NOT_ACCESSIBLE = "NOT_ACCESSIBLE"
    NOT_REQUIRED = "NOT_REQUIRED"
    BAD = "BAD"


# Backward-compatible short alias used by the first MVP1 status patch.
StatusFlags = FileReferenceStatus


def make_instrument_unit_uid(
    instrument: str | None,
    site: str | None,
    tube: str | None,
    camera: str | None,
    filter_name: str | None,
    observing_mode: str | None,
) -> str:
    """Return stable UID for one photometric-system unit.

    The UID deliberately separates instrument/site/tube/camera/filter/mode so that
    MASTER EAST/I and RoboPhot i-channel frames never share calibration solutions.
    """

    parts = [instrument, site, tube, camera, filter_name, observing_mode]
    normalized = "|".join((str(part).strip() if part not in (None, "") else "-") for part in parts)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"iu_{digest}"


def make_frame_location_uid(locator: str, location_kind: str = "LOCAL_PATH") -> str:
    """Return a stable locator identity without making it a Frame identity.

    Windows drive/UNC paths are case-insensitive for our archive workflow; POSIX
    paths and non-file URIs keep case.  The resulting key identifies only a
    recorded location, never a physical observation or Frame product.
    """

    text = str(locator).strip().replace("\\", "/")
    kind = str(location_kind or "LOCAL_PATH").strip().upper()
    looks_windows = (len(text) >= 2 and text[1] == ":") or text.startswith("//")
    normalized = text.casefold() if kind == "LOCAL_PATH" and looks_windows else text
    digest = hashlib.sha256(f"{kind}|{normalized}".encode("utf-8")).hexdigest()[:32]
    return f"loc_{digest}"


class InstrumentUnit(Base):
    __tablename__ = "instrument_units"

    instrument_unit_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    unit_uid: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)

    instrument: Mapped[str] = mapped_column(String(64), nullable=False)
    site: Mapped[str | None] = mapped_column(String(64))
    tube: Mapped[str | None] = mapped_column(String(64))
    camera: Mapped[str | None] = mapped_column(String(64))
    filter_name: Mapped[str | None] = mapped_column(String(32))
    observing_mode: Mapped[str | None] = mapped_column(String(64))

    profile_name: Mapped[str | None] = mapped_column(String(64))
    profile_version: Mapped[str | None] = mapped_column(String(32))
    pixel_scale_arcsec: Mapped[float | None] = mapped_column(Float)
    saturation_level_adu: Mapped[float | None] = mapped_column(Float)
    nominal_gain_e_per_adu: Mapped[float | None] = mapped_column(Float)
    nominal_read_noise_e: Mapped[float | None] = mapped_column(Float)

    config_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    active: Mapped[bool] = mapped_column(default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    frames: Mapped[list["Frame"]] = relationship(back_populates="instrument_unit")

    __table_args__ = (
        UniqueConstraint(
            "instrument",
            "site",
            "tube",
            "camera",
            "filter_name",
            "observing_mode",
            name="uq_instrument_units_identity",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_instrument_units_identity", "instrument", "site", "tube", "camera", "filter_name"),
    )


class Observation(Base):
    """Stable identity for one physical exposure/observation.

    One physical observation may have multiple file products in ``frames``.
    ``observation_uid`` is derived only from instrument-native exposure identity,
    never from a filesystem path, file size or modification time.
    """

    __tablename__ = "observations"

    observation_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    observation_uid: Mapped[str] = mapped_column(String(96), nullable=False)
    instrument: Mapped[str] = mapped_column(String(64), nullable=False)
    identity_scheme: Mapped[str] = mapped_column(String(64), nullable=False)
    native_identity: Mapped[str] = mapped_column(Text, nullable=False)
    camera_identity: Mapped[str | None] = mapped_column(String(96))
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    identity_components: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    frames: Mapped[list["Frame"]] = relationship(back_populates="observation")

    __table_args__ = (
        UniqueConstraint("observation_uid", name="uq_observations_observation_uid"),
        UniqueConstraint(
            "identity_scheme",
            "native_identity",
            name="uq_observations_scheme_native_identity",
        ),
        Index("ix_observations_instrument_observed_at", "instrument", "observed_at"),
    )


class Frame(Base):
    __tablename__ = "frames"

    frame_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    frame_uid: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)
    observation_id: Mapped[int | None] = mapped_column(
        ForeignKey("observations.observation_id", ondelete="SET NULL", name="fk_frames_observation_id")
    )

    instrument_unit_id: Mapped[int | None] = mapped_column(
        ForeignKey("instrument_units.instrument_unit_id", ondelete="SET NULL")
    )
    instrument: Mapped[str] = mapped_column(String(64), nullable=False)
    site: Mapped[str | None] = mapped_column(String(64))
    tube: Mapped[str | None] = mapped_column(String(64))
    camera: Mapped[str | None] = mapped_column(String(64))
    filter_name: Mapped[str | None] = mapped_column(String(32))
    observing_mode: Mapped[str | None] = mapped_column(String(64))
    image_type: Mapped[FrameType] = mapped_column(SAEnum(FrameType), nullable=False, default=FrameType.UNKNOWN)

    date_obs: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    jd: Mapped[float | None] = mapped_column(Float)
    mjd: Mapped[float | None] = mapped_column(Float)
    exptime: Mapped[float | None] = mapped_column(Float)
    object_name: Mapped[str | None] = mapped_column(Text)

    ra_center_deg: Mapped[float | None] = mapped_column(Float)
    dec_center_deg: Mapped[float | None] = mapped_column(Float)
    airmass: Mapped[float | None] = mapped_column(Float)
    ccd_temp: Mapped[float | None] = mapped_column(Float)
    gain: Mapped[float | None] = mapped_column(Float)
    read_noise: Mapped[float | None] = mapped_column(Float)
    binning_x: Mapped[int | None] = mapped_column(Integer)
    binning_y: Mapped[int | None] = mapped_column(Integer)

    wcs_status: Mapped[str] = mapped_column(String(32), nullable=False, default="NOT_CHECKED")
    flat_status: Mapped[str] = mapped_column(String(32), nullable=False, default=FileReferenceStatus.UNKNOWN.value)
    dark_status: Mapped[str] = mapped_column(String(32), nullable=False, default=FileReferenceStatus.UNKNOWN.value)
    sexcat_status: Mapped[str] = mapped_column(String(32), nullable=False, default=FileReferenceStatus.UNKNOWN.value)

    processing_status: Mapped[FrameStatus] = mapped_column(
        SAEnum(FrameStatus), nullable=False, default=FrameStatus.DISCOVERED
    )
    quality_flags: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    header_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    instrument_unit: Mapped[InstrumentUnit | None] = relationship(back_populates="frames")
    observation: Mapped[Observation | None] = relationship(back_populates="frames")
    locations: Mapped[list["FrameLocation"]] = relationship(
        back_populates="frame", cascade="all, delete-orphan", order_by="FrameLocation.frame_location_id"
    )
    events: Mapped[list["ProcessingEvent"]] = relationship(
        back_populates="frame", cascade="all, delete-orphan"
    )

    def _primary_location(self) -> "FrameLocation | None":
        rows = list(self.locations or [])
        if not rows:
            return None
        for row in rows:
            if row.is_primary:
                return row
        return rows[0]

    def _ensure_primary_location(self) -> "FrameLocation":
        row = self._primary_location()
        if row is not None:
            return row
        locator = getattr(self, "_pending_raw_path", None)
        if not locator:
            raise ValueError("frame location requires raw_path before file metadata")
        row = FrameLocation(
            locator=str(locator),
            locator_uid=make_frame_location_uid(str(locator)),
            location_kind="LOCAL_PATH",
            is_primary=True,
            file_size_bytes=getattr(self, "_pending_file_size_bytes", None),
            file_mtime_utc=getattr(self, "_pending_file_mtime_utc", None),
        )
        self.locations.append(row)
        return row

    @property
    def raw_path(self) -> str | None:
        row = self._primary_location()
        return row.locator if row is not None else getattr(self, "_pending_raw_path", None)

    @raw_path.setter
    def raw_path(self, value: str | None) -> None:
        if value in (None, ""):
            self._pending_raw_path = None
            return
        value = str(value)
        self._pending_raw_path = value
        row = self._primary_location()
        if row is None:
            row = FrameLocation(
                locator=value,
                locator_uid=make_frame_location_uid(value),
                location_kind="LOCAL_PATH",
                is_primary=True,
                file_size_bytes=getattr(self, "_pending_file_size_bytes", None),
                file_mtime_utc=getattr(self, "_pending_file_mtime_utc", None),
            )
            self.locations.append(row)
        else:
            row.locator = value
            row.locator_uid = make_frame_location_uid(value, row.location_kind)

    @property
    def file_size_bytes(self) -> int | None:
        row = self._primary_location()
        return row.file_size_bytes if row is not None else getattr(self, "_pending_file_size_bytes", None)

    @file_size_bytes.setter
    def file_size_bytes(self, value: int | None) -> None:
        self._pending_file_size_bytes = value
        row = self._primary_location()
        if row is not None:
            row.file_size_bytes = value

    @property
    def file_mtime_utc(self) -> datetime | None:
        row = self._primary_location()
        return row.file_mtime_utc if row is not None else getattr(self, "_pending_file_mtime_utc", None)

    @file_mtime_utc.setter
    def file_mtime_utc(self, value: datetime | None) -> None:
        self._pending_file_mtime_utc = value
        row = self._primary_location()
        if row is not None:
            row.file_mtime_utc = value

    __table_args__ = (
        Index("ix_frames_instrument_unit", "instrument", "site", "tube", "camera", "filter_name"),
        Index("ix_frames_instrument_unit_id", "instrument_unit_id"),
        Index("ix_frames_observation_id", "observation_id"),
        Index("ix_frames_date_obs", "date_obs"),
        Index("ix_frames_status", "processing_status"),
        Index("ix_frames_image_type", "image_type"),
    )


class FrameLocation(Base):
    """Mutable locator/content metadata for one immutable Frame product.

    A Frame can be discovered at multiple paths/URIs over time.  Locator changes
    do not alter Frame or Observation identity.
    """

    __tablename__ = "frame_locations"

    frame_location_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    frame_id: Mapped[int] = mapped_column(
        ForeignKey("frames.frame_id", ondelete="CASCADE"), nullable=False
    )
    locator: Mapped[str] = mapped_column(Text, nullable=False)
    locator_uid: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    location_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="LOCAL_PATH")
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    availability_status: Mapped[str] = mapped_column(String(32), nullable=False, default="AVAILABLE")
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    file_mtime_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    frame: Mapped["Frame"] = relationship(back_populates="locations")

    __table_args__ = (
        UniqueConstraint("frame_id", "locator_uid", name="uq_frame_locations_frame_locator"),
        Index("ix_frame_locations_frame", "frame_id"),
        Index("ix_frame_locations_primary", "frame_id", "is_primary"),
        Index(
            "uq_frame_locations_one_primary",
            "frame_id",
            unique=True,
            postgresql_where=text("is_primary"),
            sqlite_where=text("is_primary"),
        ),
        Index("ix_frame_locations_content_sha256", "content_sha256"),
    )


class ProcessingEvent(Base):
    __tablename__ = "processing_events"

    event_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    frame_id: Mapped[int | None] = mapped_column(ForeignKey("frames.frame_id", ondelete="CASCADE"))
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    level: Mapped[ProcessingEventLevel] = mapped_column(SAEnum(ProcessingEventLevel), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    frame: Mapped[Frame | None] = relationship(back_populates="events")

    __table_args__ = (Index("ix_processing_events_stage_level", "stage", "level"),)


class CatalogSource(Base):
    __tablename__ = "catalog_sources"

    catalog_source_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    catalog_name: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_uid: Mapped[str] = mapped_column(String(192), nullable=False, unique=True)

    ra_deg: Mapped[float] = mapped_column(Float, nullable=False)
    dec_deg: Mapped[float] = mapped_column(Float, nullable=False)
    epoch: Mapped[float | None] = mapped_column(Float)
    pmra_masyr: Mapped[float | None] = mapped_column(Float)
    pmdec_masyr: Mapped[float | None] = mapped_column(Float)

    ref_mags: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    colors: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    source_flags: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    raw_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    frame_links: Mapped[list["FrameCatalogLink"]] = relationship(
        back_populates="catalog_source", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("catalog_name", "source_id", name="uq_catalog_sources_catalog_source"),
        Index("ix_catalog_sources_catalog", "catalog_name"),
        Index("ix_catalog_sources_sky", "ra_deg", "dec_deg"),
    )


class ReferencePhotometryCatalog(Base):
    __tablename__ = "reference_photometry_catalogs"

    refcat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    catalog_name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    catalog_version: Mapped[str | None] = mapped_column(String(64))
    photometric_system: Mapped[str | None] = mapped_column(String(64))
    source_priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (Index("ix_reference_photometry_catalogs_priority", "source_priority", "catalog_name"),)


class PhotometricBand(Base):
    __tablename__ = "photometric_bands"

    band_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    refcat_id: Mapped[int] = mapped_column(
        ForeignKey("reference_photometry_catalogs.refcat_id", ondelete="CASCADE"), nullable=False
    )
    canonical_name: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_band_name: Mapped[str | None] = mapped_column(String(64))
    photometric_system: Mapped[str | None] = mapped_column(String(64))
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    catalog: Mapped["ReferencePhotometryCatalog"] = relationship()

    __table_args__ = (
        UniqueConstraint("refcat_id", "canonical_name", name="uq_photometric_bands_catalog_name"),
        Index("ix_photometric_bands_name", "canonical_name"),
    )


class ReferencePhotometrySource(Base):
    __tablename__ = "reference_photometry_sources"

    refphot_source_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    refcat_id: Mapped[int] = mapped_column(
        ForeignKey("reference_photometry_catalogs.refcat_id", ondelete="CASCADE"), nullable=False
    )
    catalog_source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    ref_uid: Mapped[str] = mapped_column(String(192), nullable=False, unique=True)

    identity_catalog_name: Mapped[str | None] = mapped_column(String(64))
    identity_source_id: Mapped[str | None] = mapped_column(String(128))
    identity_catalog_source_id: Mapped[int | None] = mapped_column(
        ForeignKey("catalog_sources.catalog_source_id", ondelete="RESTRICT")
    )
    distance_to_identity_arcsec: Mapped[float | None] = mapped_column(Float)

    ra_deg: Mapped[float] = mapped_column(Float, nullable=False)
    dec_deg: Mapped[float] = mapped_column(Float, nullable=False)
    epoch: Mapped[float | None] = mapped_column(Float)

    colors: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    quality_flags: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    raw_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    catalog: Mapped["ReferencePhotometryCatalog"] = relationship()
    identity_catalog_source: Mapped["CatalogSource | None"] = relationship()
    points: Mapped[list["ReferencePhotometryPoint"]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("refcat_id", "catalog_source_id", name="uq_reference_photometry_sources_catalog_source"),
        Index("ix_reference_photometry_sources_catalog", "refcat_id"),
        Index("ix_reference_photometry_sources_sky", "ra_deg", "dec_deg"),
        Index("ix_reference_photometry_sources_identity", "identity_catalog_name", "identity_source_id"),
        Index("ix_reference_photometry_sources_identity_source", "identity_catalog_source_id"),
    )


class ReferencePhotometryPoint(Base):
    __tablename__ = "reference_photometry_points"

    refphot_point_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    refphot_source_id: Mapped[int] = mapped_column(
        ForeignKey("reference_photometry_sources.refphot_source_id", ondelete="CASCADE"), nullable=False
    )
    band_id: Mapped[int] = mapped_column(
        ForeignKey("photometric_bands.band_id", ondelete="RESTRICT"), nullable=False
    )
    magnitude: Mapped[float] = mapped_column(Float, nullable=False)
    magnitude_error: Mapped[float | None] = mapped_column(Float)
    quality_status: Mapped[str | None] = mapped_column(String(32))
    quality_flags: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    provider_payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    source: Mapped["ReferencePhotometrySource"] = relationship(back_populates="points")
    band: Mapped["PhotometricBand"] = relationship()

    __table_args__ = (
        UniqueConstraint("refphot_source_id", "band_id", name="uq_reference_photometry_points_source_band"),
        Index("ix_reference_photometry_points_band", "band_id"),
    )


class LegacyReferencePhotometry(LegacyBase):
    """Frozen pre-0.7.0-D wide reference-photometry mapping for forensic reads."""

    __tablename__ = "reference_photometry"

    refphot_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    refcat_id: Mapped[int | None] = mapped_column(BigInteger)
    catalog_name: Mapped[str] = mapped_column(String(64), nullable=False)
    catalog_source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    ref_uid: Mapped[str] = mapped_column(String(192), nullable=False)
    gaia_catalog_name: Mapped[str | None] = mapped_column(String(64))
    gaia_source_id: Mapped[str | None] = mapped_column(String(128))
    gaia_catalog_source_id: Mapped[int | None] = mapped_column(BigInteger)
    distance_to_gaia_arcsec: Mapped[float | None] = mapped_column(Float)
    ra_deg: Mapped[float] = mapped_column(Float, nullable=False)
    dec_deg: Mapped[float] = mapped_column(Float, nullable=False)
    epoch: Mapped[float | None] = mapped_column(Float)
    mag_g: Mapped[float | None] = mapped_column(Float)
    mag_r: Mapped[float | None] = mapped_column(Float)
    mag_i: Mapped[float | None] = mapped_column(Float)
    mag_z: Mapped[float | None] = mapped_column(Float)
    mag_y: Mapped[float | None] = mapped_column(Float)
    mag_b: Mapped[float | None] = mapped_column(Float)
    mag_v: Mapped[float | None] = mapped_column(Float)
    mag_j: Mapped[float | None] = mapped_column(Float)
    mag_h: Mapped[float | None] = mapped_column(Float)
    mag_k: Mapped[float | None] = mapped_column(Float)
    mag_bp: Mapped[float | None] = mapped_column(Float)
    mag_rp: Mapped[float | None] = mapped_column(Float)
    mag_g_gaia: Mapped[float | None] = mapped_column(Float)
    mag_errors: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    colors: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    quality_flags: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    raw_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)



class FrameCatalogLink(Base):
    __tablename__ = "frame_catalog_links"

    frame_catalog_link_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    frame_id: Mapped[int] = mapped_column(ForeignKey("frames.frame_id", ondelete="CASCADE"), nullable=False)
    catalog_source_id: Mapped[int] = mapped_column(
        ForeignKey("catalog_sources.catalog_source_id", ondelete="CASCADE"), nullable=False
    )

    x: Mapped[float | None] = mapped_column(Float)
    y: Mapped[float | None] = mapped_column(Float)
    in_footprint: Mapped[bool] = mapped_column(default=False, nullable=False)
    margin_pix: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    match_status: Mapped[str] = mapped_column(String(32), nullable=False, default="WCS_PROJECTED")
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    frame: Mapped["Frame"] = relationship()
    catalog_source: Mapped["CatalogSource"] = relationship(back_populates="frame_links")

    __table_args__ = (
        UniqueConstraint("frame_id", "catalog_source_id", name="uq_frame_catalog_links_frame_source"),
        Index("ix_frame_catalog_links_frame", "frame_id"),
        Index("ix_frame_catalog_links_source", "catalog_source_id"),
        Index("ix_frame_catalog_links_status", "match_status"),
    )


class DetectionRun(Base):
    """Run-level provenance/configuration for detection and catalog-matching work.

    High-cardinality child rows reference this table instead of repeating the same
    algorithm/configuration payload millions of times.  ``run_kind`` separates
    source extraction from detection↔catalog matching while keeping one provenance
    vocabulary for the detection subsystem.
    """

    __tablename__ = "detection_runs"

    detection_run_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    pipeline_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("pipeline_runs.pipeline_run_id", ondelete="RESTRICT")
    )
    run_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    catalog_name: Mapped[str | None] = mapped_column(String(64))
    algorithm: Mapped[str] = mapped_column(String(64), nullable=False)
    algorithm_version: Mapped[str | None] = mapped_column(String(32))
    parameters: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "run_kind IN ('SOURCE_DETECTION', 'CATALOG_MATCH')",
            name="ck_detection_runs_kind",
        ),
        CheckConstraint(
            "(run_kind = 'SOURCE_DETECTION' AND catalog_name IS NULL) OR "
            "(run_kind = 'CATALOG_MATCH' AND catalog_name IS NOT NULL)",
            name="ck_detection_runs_catalog_scope",
        ),
        Index("ix_detection_runs_pipeline", "pipeline_run_id"),
        Index("ix_detection_runs_kind_algorithm", "run_kind", "algorithm"),
        Index("ix_detection_runs_catalog", "catalog_name"),
        Index("ix_detection_runs_fingerprint", "fingerprint"),
    )


class DetectionRunFrame(Base):
    """Per-frame execution state for one :class:`DetectionRun`.

    This row is the canonical frame scope for source detections and catalog matches.
    It also preserves successful zero-detection/zero-match frames and frame-level
    diagnostics without copying those values into every source row.
    """

    __tablename__ = "detection_run_frames"

    detection_run_frame_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    detection_run_id: Mapped[int] = mapped_column(
        ForeignKey("detection_runs.detection_run_id", ondelete="CASCADE"), nullable=False
    )
    frame_id: Mapped[int] = mapped_column(
        ForeignKey("frames.frame_id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="DONE")
    diagnostics: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    detection_run: Mapped["DetectionRun"] = relationship()
    frame: Mapped["Frame"] = relationship()

    __table_args__ = (
        UniqueConstraint("detection_run_id", "frame_id", name="uq_detection_run_frames_run_frame"),
        Index("ix_detection_run_frames_frame", "frame_id"),
        Index("ix_detection_run_frames_status", "status"),
    )


class SourceDetection(Base):
    __tablename__ = "source_detections"

    source_detection_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    detection_run_frame_id: Mapped[int] = mapped_column(
        ForeignKey("detection_run_frames.detection_run_frame_id", ondelete="CASCADE"), nullable=False
    )

    x: Mapped[float] = mapped_column(Float, nullable=False)
    y: Mapped[float] = mapped_column(Float, nullable=False)
    ra_deg: Mapped[float | None] = mapped_column(Float)
    dec_deg: Mapped[float | None] = mapped_column(Float)

    peak_adu: Mapped[float | None] = mapped_column(Float)
    flux: Mapped[float | None] = mapped_column(Float)
    flux_err: Mapped[float | None] = mapped_column(Float)
    local_background: Mapped[float | None] = mapped_column(Float)
    snr: Mapped[float | None] = mapped_column(Float)
    npix: Mapped[int | None] = mapped_column(Integer)

    detection_status: Mapped[str] = mapped_column(String(32), nullable=False, default="DETECTED")
    flags: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    detection_run_frame: Mapped["DetectionRunFrame"] = relationship()

    __table_args__ = (
        Index("ix_source_detections_run_frame", "detection_run_frame_id"),
        Index("ix_source_detections_status", "detection_status"),
        Index("ix_source_detections_sky", "ra_deg", "dec_deg"),
        Index("ix_source_detections_snr", "snr"),
    )


class DetectionCatalogMatch(Base):
    __tablename__ = "detection_catalog_matches"

    detection_catalog_match_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    detection_run_frame_id: Mapped[int] = mapped_column(
        ForeignKey("detection_run_frames.detection_run_frame_id", ondelete="CASCADE"), nullable=False
    )
    source_detection_id: Mapped[int] = mapped_column(
        ForeignKey("source_detections.source_detection_id", ondelete="CASCADE"), nullable=False
    )
    catalog_source_id: Mapped[int] = mapped_column(
        ForeignKey("catalog_sources.catalog_source_id", ondelete="CASCADE"), nullable=False
    )

    match_status: Mapped[str] = mapped_column(String(32), nullable=False, default="MATCHED")

    sep_arcsec: Mapped[float | None] = mapped_column(Float)
    dx_pix: Mapped[float | None] = mapped_column(Float)
    dy_pix: Mapped[float | None] = mapped_column(Float)
    # Keep these scalar snapshots deliberately. CatalogSource.ref_mags is currently
    # updatable, so removing them would destroy exact historical per-match QC.
    detection_mag_inst: Mapped[float | None] = mapped_column(Float)
    catalog_mag: Mapped[float | None] = mapped_column(Float)
    zero_point: Mapped[float | None] = mapped_column(Float)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    detection_run_frame: Mapped["DetectionRunFrame"] = relationship()
    source_detection: Mapped["SourceDetection"] = relationship()
    catalog_source: Mapped["CatalogSource"] = relationship()

    __table_args__ = (
        UniqueConstraint(
            "detection_run_frame_id",
            "source_detection_id",
            name="uq_detection_catalog_matches_run_frame_detection",
        ),
        UniqueConstraint(
            "detection_run_frame_id",
            "catalog_source_id",
            name="uq_detection_catalog_matches_run_frame_catalog_source",
        ),
        Index("ix_detection_catalog_matches_run_frame", "detection_run_frame_id"),
        Index("ix_detection_catalog_matches_detection", "source_detection_id"),
        Index("ix_detection_catalog_matches_catalog_source", "catalog_source_id"),
        Index("ix_detection_catalog_matches_sep", "sep_arcsec"),
        Index("ix_detection_catalog_matches_zp", "zero_point"),
    )


class FrameQc(Base):
    __tablename__ = "frame_qc"

    frame_qc_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    frame_id: Mapped[int] = mapped_column(ForeignKey("frames.frame_id", ondelete="CASCADE"), nullable=False)
    catalog_name: Mapped[str] = mapped_column(String(64), nullable=False)
    algorithm: Mapped[str] = mapped_column(String(64), nullable=False, default="detection_catalog_qc_v1")

    n_detections: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    n_catalog_sources: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    n_matched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    zp_n: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    astrom_rms_arcsec: Mapped[float | None] = mapped_column(Float)
    dx_median_pix: Mapped[float | None] = mapped_column(Float)
    dy_median_pix: Mapped[float | None] = mapped_column(Float)
    sep_median_arcsec: Mapped[float | None] = mapped_column(Float)
    sep_p95_arcsec: Mapped[float | None] = mapped_column(Float)
    zp_median: Mapped[float | None] = mapped_column(Float)
    zp_mad: Mapped[float | None] = mapped_column(Float)

    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    frame: Mapped["Frame"] = relationship()

    __table_args__ = (
        UniqueConstraint("frame_id", "catalog_name", "algorithm", name="uq_frame_qc_frame_catalog_algorithm"),
        Index("ix_frame_qc_frame", "frame_id"),
        Index("ix_frame_qc_catalog", "catalog_name"),
        Index("ix_frame_qc_astrom", "astrom_rms_arcsec"),
        Index("ix_frame_qc_zp", "zp_median"),
    )


class Object(Base):
    __tablename__ = "objects"

    object_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    object_uid: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)
    identity_scheme: Mapped[str] = mapped_column(String(32), nullable=False, default="INTERNAL_UUID_V1")
    ra_deg: Mapped[float] = mapped_column(Float, nullable=False)
    dec_deg: Mapped[float] = mapped_column(Float, nullable=False)
    reference_epoch: Mapped[float | None] = mapped_column(Float)
    ref_mags: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    colors: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    pmra_masyr: Mapped[float | None] = mapped_column(Float)
    pmdec_masyr: Mapped[float | None] = mapped_column(Float)
    crowding_index: Mapped[float | None] = mapped_column(Float)
    object_flags: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    catalog_links: Mapped[list["ObjectCatalogLink"]] = relationship(
        back_populates="object", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_objects_sky", "ra_deg", "dec_deg"),
        Index("ix_objects_identity_scheme", "identity_scheme"),
    )


class ObjectCatalogLink(Base):
    """Explicit evidence that one external catalog source represents an Object."""

    __tablename__ = "object_catalog_links"

    object_catalog_link_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    object_id: Mapped[int] = mapped_column(
        ForeignKey("objects.object_id", ondelete="CASCADE"), nullable=False
    )
    catalog_source_id: Mapped[int] = mapped_column(
        ForeignKey("catalog_sources.catalog_source_id", ondelete="RESTRICT"), nullable=False
    )
    association_method: Mapped[str] = mapped_column(String(48), nullable=False)
    separation_arcsec: Mapped[float | None] = mapped_column(Float)
    comparison_epoch: Mapped[float | None] = mapped_column(Float)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    object: Mapped["Object"] = relationship(back_populates="catalog_links")
    catalog_source: Mapped["CatalogSource"] = relationship()

    __table_args__ = (
        UniqueConstraint("catalog_source_id", name="uq_object_catalog_links_catalog_source"),
        UniqueConstraint("object_id", "catalog_source_id", name="uq_object_catalog_links_object_source"),
        Index("ix_object_catalog_links_object", "object_id"),
        Index("ix_object_catalog_links_method", "association_method"),
    )


class ObjectQc(Base):
    __tablename__ = "object_qc"

    object_qc_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    object_id: Mapped[int] = mapped_column(ForeignKey("objects.object_id", ondelete="CASCADE"), nullable=False)
    calibration_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("calibration_versions.calibration_version_id", ondelete="RESTRICT")
    )
    instrument_unit_id: Mapped[int | None] = mapped_column(
        ForeignKey("instrument_units.instrument_unit_id", ondelete="SET NULL")
    )
    catalog_name: Mapped[str | None] = mapped_column(String(64))
    algorithm: Mapped[str] = mapped_column(String(64), nullable=False, default="object_qc_v1")

    nearest_neighbor_arcsec: Mapped[float | None] = mapped_column(Float)
    neighbors_within_3arcsec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    neighbors_within_5arcsec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    neighbors_within_10arcsec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    median_x: Mapped[float | None] = mapped_column(Float)
    median_y: Mapped[float | None] = mapped_column(Float)
    edge_distance_pix_min: Mapped[float | None] = mapped_column(Float)

    n_measurements: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mag_median: Mapped[float | None] = mapped_column(Float)
    mag_rms: Mapped[float | None] = mapped_column(Float)
    mag_mad: Mapped[float | None] = mapped_column(Float)
    mag_err_median: Mapped[float | None] = mapped_column(Float)

    flags: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    object: Mapped["Object"] = relationship()
    calibration_version: Mapped["CalibrationVersion | None"] = relationship()
    instrument_unit: Mapped["InstrumentUnit | None"] = relationship()

    __table_args__ = (
        UniqueConstraint(
            "object_id",
            "catalog_name",
            "instrument_unit_id",
            "calibration_version_id",
            "algorithm",
            name="uq_object_qc_identity",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_object_qc_object", "object_id"),
        Index("ix_object_qc_catalog", "catalog_name"),
        Index("ix_object_qc_instrument_unit", "instrument_unit_id"),
        Index("ix_object_qc_flags", "flags", postgresql_using="gin"),
        Index("ix_object_qc_nn", "nearest_neighbor_arcsec"),
        Index("ix_object_qc_rms", "mag_rms"),
    )


class Measurement(Base):
    __tablename__ = "measurements"

    measurement_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    object_id: Mapped[int] = mapped_column(ForeignKey("objects.object_id", ondelete="CASCADE"))
    frame_id: Mapped[int] = mapped_column(ForeignKey("frames.frame_id", ondelete="CASCADE"))
    calibration_version_id: Mapped[int] = mapped_column(
        ForeignKey("calibration_versions.calibration_version_id", ondelete="RESTRICT"),
        nullable=False,
    )

    x: Mapped[float | None] = mapped_column(Float)
    y: Mapped[float | None] = mapped_column(Float)
    flux: Mapped[float | None] = mapped_column(Float)
    flux_err: Mapped[float | None] = mapped_column(Float)
    mag_inst: Mapped[float | None] = mapped_column(Float)
    mag_cal: Mapped[float | None] = mapped_column(Float)
    mag_err: Mapped[float | None] = mapped_column(Float)
    mag_err_random: Mapped[float | None] = mapped_column(Float)
    mag_err_systematic: Mapped[float | None] = mapped_column(Float)
    aperture_radius_pix: Mapped[float | None] = mapped_column(Float)
    local_background: Mapped[float | None] = mapped_column(Float)
    local_zp_correction: Mapped[float | None] = mapped_column(Float)
    centroid_dx_pix: Mapped[float | None] = mapped_column(Float)
    centroid_dy_pix: Mapped[float | None] = mapped_column(Float)
    flags: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        UniqueConstraint("object_id", "frame_id", "calibration_version_id", name="uq_measurement_version"),
        Index("ix_measurements_object", "object_id"),
        Index("ix_measurements_frame", "frame_id"),
        Index(
            "ix_measurements_calibration_object_frame",
            "calibration_version_id",
            "object_id",
            "frame_id",
        ),
    )


class ScienceMeasurement(LegacyBase):
    """Final science photometry materialization.

    During the D2 compatibility phase this table is dual-written from
    ``BASE_PHOTOMETRY + normalized calibration terms`` and shadow-compared
    against the legacy final row in ``measurements``.  Downstream science
    readers prefer this table and fall back to ``measurements`` for older
    products.
    """

    __tablename__ = "science_measurements"

    science_measurement_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    calibration_version_id: Mapped[int] = mapped_column(
        ForeignKey("calibration_versions.calibration_version_id", ondelete="CASCADE"), nullable=False
    )
    source_measurement_id: Mapped[int | None] = mapped_column(
        ForeignKey("measurements.measurement_id", ondelete="SET NULL")
    )
    base_measurement_id: Mapped[int | None] = mapped_column(
        ForeignKey("measurements.measurement_id", ondelete="SET NULL")
    )
    object_id: Mapped[int] = mapped_column(ForeignKey("objects.object_id", ondelete="CASCADE"), nullable=False)
    frame_id: Mapped[int] = mapped_column(ForeignKey("frames.frame_id", ondelete="CASCADE"), nullable=False)

    x: Mapped[float | None] = mapped_column(Float)
    y: Mapped[float | None] = mapped_column(Float)
    flux: Mapped[float | None] = mapped_column(Float)
    flux_err: Mapped[float | None] = mapped_column(Float)
    mag_inst: Mapped[float | None] = mapped_column(Float)
    mag_cal: Mapped[float | None] = mapped_column(Float)
    mag_err: Mapped[float | None] = mapped_column(Float)
    mag_err_random: Mapped[float | None] = mapped_column(Float)
    mag_err_systematic: Mapped[float | None] = mapped_column(Float)
    aperture_radius_pix: Mapped[float | None] = mapped_column(Float)
    local_background: Mapped[float | None] = mapped_column(Float)
    local_zp_correction: Mapped[float | None] = mapped_column(Float)
    centroid_dx_pix: Mapped[float | None] = mapped_column(Float)
    centroid_dy_pix: Mapped[float | None] = mapped_column(Float)
    flags: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    materialization_source: Mapped[str] = mapped_column(
        String(48), nullable=False, default="RECONSTRUCTED_NORMALIZED", server_default="RECONSTRUCTED_NORMALIZED"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "calibration_version_id", "object_id", "frame_id",
            name="uq_science_measurements_version_object_frame",
        ),
        # The UNIQUE constraint above already provides the primary science-reader
        # access path (calibration_version_id, object_id, frame_id).  Keep only
        # the complementary frame-oriented path.  Stand-alone object/frame/base
        # indexes are intentionally omitted for the immutable archive workload: all
        # normal readers are calibration-scoped and parent-row deletes are rare.
        Index(
            "ix_science_measurements_calibration_frame",
            "calibration_version_id",
            "frame_id",
            "object_id",
        ),
    )


class ScienceFlagOverrideSet(LegacyBase):
    """Exact historical flag-delta manifest for one SCIENCE product.

    The normalized reconstruction remains authoritative for science scalars and
    modern flags.  Historical V1 products may contain legacy flag semantics that
    differ from the current reconstruction.  This manifest stores only the
    calibration-wide portion of that exact top-level patch plus proof metadata;
    row-specific residuals live in :class:`ScienceFlagOverride`.
    """

    __tablename__ = "science_flag_override_sets"

    calibration_version_id: Mapped[int] = mapped_column(
        ForeignKey("calibration_versions.calibration_version_id", ondelete="CASCADE"),
        primary_key=True,
    )
    source_row_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    row_override_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    common_override_flags: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    common_removed_keys: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    algorithm_version: Mapped[str] = mapped_column(
        String(48), nullable=False, default="TOPLEVEL_EXACT_V1"
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="COMPLETE_EXACT"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ScienceFlagOverride(LegacyBase):
    """Row-specific residual of an exact historical SCIENCE flag patch."""

    __tablename__ = "science_flag_overrides"

    calibration_version_id: Mapped[int] = mapped_column(
        ForeignKey("science_flag_override_sets.calibration_version_id", ondelete="CASCADE"),
        primary_key=True,
    )
    base_measurement_id: Mapped[int] = mapped_column(
        ForeignKey("measurements.measurement_id", ondelete="CASCADE"),
        primary_key=True,
    )
    override_flags: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    removed_keys: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)


class SciencePhotometryCache(LegacyBase):
    """Frozen 0.6 rebuildable SCIENCE cache mapping for forensic tools only."""

    __tablename__ = "science_photometry_cache"

    calibration_version_id: Mapped[int] = mapped_column(
        ForeignKey("calibration_versions.calibration_version_id", ondelete="CASCADE"),
        primary_key=True,
    )
    base_measurement_id: Mapped[int] = mapped_column(
        ForeignKey("measurements.measurement_id", ondelete="CASCADE"),
        primary_key=True,
    )
    mag_cal: Mapped[float | None] = mapped_column(Float)
    mag_err_random: Mapped[float | None] = mapped_column(Float)


class SciencePhotometry(Base):
    """Durable narrow final SCIENCE photometry for a calibration product.

    Object/frame identity and BASE-identical extraction geometry are inherited
    through ``base_measurement_id``.  Only scalar state that calibration stages
    can change is stored here.  Flags remain part of normalized/current
    reconstruction semantics rather than a repeated per-row compatibility blob.
    """

    __tablename__ = "science_photometry"

    calibration_version_id: Mapped[int] = mapped_column(
        ForeignKey("calibration_versions.calibration_version_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    base_measurement_id: Mapped[int] = mapped_column(
        ForeignKey("measurements.measurement_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    flux: Mapped[float | None] = mapped_column(Float)
    flux_err: Mapped[float | None] = mapped_column(Float)
    mag_inst: Mapped[float | None] = mapped_column(Float)
    mag_cal: Mapped[float | None] = mapped_column(Float)
    mag_err: Mapped[float | None] = mapped_column(Float)
    mag_err_random: Mapped[float | None] = mapped_column(Float)
    mag_err_systematic: Mapped[float | None] = mapped_column(Float)
    local_zp_correction: Mapped[float | None] = mapped_column(Float)



class CalibrationVersion(Base):
    __tablename__ = "calibration_versions"

    calibration_version_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    instrument_unit_id: Mapped[int | None] = mapped_column(
        ForeignKey("instrument_units.instrument_unit_id", ondelete="SET NULL")
    )
    source_calibration_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("calibration_versions.calibration_version_id", ondelete="RESTRICT")
    )

    # MVP5.39.2 product-provenance foundation.  These columns are additive: the
    # current materialized calibration rows remain compatibility-mutable until
    # the later base+final cutover, but every new main-pipeline product can now
    # carry explicit run/scope/type identity instead of encoding it in its name.
    pipeline_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("pipeline_runs.pipeline_run_id", ondelete="SET NULL")
    )
    field_id: Mapped[int | None] = mapped_column(
        ForeignKey("fields.field_id", ondelete="SET NULL")
    )
    series_id: Mapped[int | None] = mapped_column(
        ForeignKey("field_series.series_id", ondelete="SET NULL")
    )
    supersedes_calibration_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("calibration_versions.calibration_version_id", ondelete="RESTRICT")
    )
    product_kind: Mapped[str] = mapped_column(
        String(48), nullable=False, default="LEGACY_UNCLASSIFIED", server_default="LEGACY_UNCLASSIFIED"
    )
    product_role: Mapped[str] = mapped_column(
        String(32), nullable=False, default="INTERMEDIATE", server_default="INTERMEDIATE"
    )
    materialization_kind: Mapped[str] = mapped_column(
        String(48), nullable=False, default="FULL_MEASUREMENTS", server_default="FULL_MEASUREMENTS"
    )
    lifecycle_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="LEGACY_MUTABLE", server_default="LEGACY_MUTABLE"
    )
    definition_fingerprint_sha256: Mapped[str | None] = mapped_column(String(64))
    input_fingerprint_sha256: Mapped[str | None] = mapped_column(String(64))
    provenance_fingerprint_sha256: Mapped[str | None] = mapped_column(String(64))
    materialized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # MVP5.39.5 immutable-generation compatibility layer. ``name`` remains the
    # publication/head name for backward-compatible readers. ``logical_name``
    # and ``generation_no`` identify the durable generation independently of
    # that publication name; superseded rows are renamed to archival physical
    # names while their scientific content and primary key remain unchanged.
    logical_name: Mapped[str | None] = mapped_column(String(128))
    generation_no: Mapped[int | None] = mapped_column(Integer)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    source_calibration: Mapped["CalibrationVersion | None"] = relationship(
        "CalibrationVersion",
        remote_side="CalibrationVersion.calibration_version_id",
        foreign_keys="CalibrationVersion.source_calibration_version_id",
        post_update=True,
    )
    supersedes_calibration: Mapped["CalibrationVersion | None"] = relationship(
        "CalibrationVersion",
        remote_side="CalibrationVersion.calibration_version_id",
        foreign_keys="CalibrationVersion.supersedes_calibration_version_id",
        post_update=True,
    )
    instrument_profile: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    used_catalogs: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    parameters: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("calibration_version_id", "field_id", name="uq_calibration_versions_id_field"),
        UniqueConstraint("calibration_version_id", "field_id", "series_id",
                         name="uq_calibration_versions_id_field_series"),
        Index("ix_calibration_versions_source", "source_calibration_version_id"),
        Index("ix_calibration_versions_pipeline_run", "pipeline_run_id"),
        Index("ix_calibration_versions_field_series", "field_id", "series_id"),
        Index("ix_calibration_versions_product_kind", "product_kind", "product_role"),
        Index("ix_calibration_versions_lifecycle", "lifecycle_status"),
        Index("ix_calibration_versions_supersedes", "supersedes_calibration_version_id"),
        Index("ix_calibration_versions_provenance_fingerprint", "provenance_fingerprint_sha256"),
        Index("ix_calibration_versions_logical_name", "logical_name"),
        UniqueConstraint(
            "logical_name",
            "generation_no",
            name="uq_calibration_versions_logical_generation",
        ),
    )


class CalibrationProductAlias(Base):
    """Stable logical publication name pointing at the current immutable generation."""

    __tablename__ = "calibration_product_aliases"

    alias_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    logical_name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    head_calibration_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("calibration_versions.calibration_version_id", ondelete="RESTRICT")
    )
    head_generation_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    updated_by_pipeline_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("pipeline_runs.pipeline_run_id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    head_calibration_version: Mapped["CalibrationVersion | None"] = relationship(
        foreign_keys=[head_calibration_version_id]
    )

    __table_args__ = (
        Index("ix_calibration_product_aliases_head", "head_calibration_version_id"),
        Index("ix_calibration_product_aliases_pipeline_run", "updated_by_pipeline_run_id"),
    )


class CalibrationIteration(Base):
    """Compact, scientifically meaningful calibration pass.

    A row represents a pipeline-significant calibration pass, not every inner
    sigma-clipping/optimizer loop.  Large per-measurement intermediate products
    remain compatibility materializations during the dual-write phase, while
    the model state needed to reproduce them is stored here and in the child
    frame/object tables.
    """

    __tablename__ = "calibration_iterations"

    calibration_iteration_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    calibration_version_id: Mapped[int] = mapped_column(
        ForeignKey("calibration_versions.calibration_version_id", ondelete="RESTRICT"), nullable=False
    )
    source_calibration_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("calibration_versions.calibration_version_id", ondelete="RESTRICT")
    )
    pipeline_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("pipeline_runs.pipeline_run_id", ondelete="SET NULL")
    )
    field_id: Mapped[int | None] = mapped_column(
        ForeignKey("fields.field_id", ondelete="SET NULL")
    )
    series_id: Mapped[int | None] = mapped_column(
        ForeignKey("field_series.series_id", ondelete="SET NULL")
    )
    iteration_kind: Mapped[str] = mapped_column(String(48), nullable=False)
    iteration_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    algorithm: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="COMPLETE", server_default="COMPLETE")
    convergence_status: Mapped[str | None] = mapped_column(String(48))
    convergence_reason: Mapped[str | None] = mapped_column(Text)
    is_final: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    parameters: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    metrics: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "calibration_version_id",
            "iteration_kind",
            "iteration_no",
            name="uq_calibration_iterations_version_kind_no",
        ),
        Index("ix_calibration_iterations_version", "calibration_version_id", "iteration_no"),
        Index("ix_calibration_iterations_run", "pipeline_run_id"),
        Index("ix_calibration_iterations_series", "series_id"),
        Index("ix_calibration_iterations_kind", "iteration_kind", "status"),
    )


class CalibrationFrameTerm(Base):
    """One normalized per-frame calibration model/offset for an iteration."""

    __tablename__ = "calibration_frame_terms"

    calibration_frame_term_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    calibration_iteration_id: Mapped[int] = mapped_column(
        ForeignKey("calibration_iterations.calibration_iteration_id", ondelete="CASCADE"), nullable=False
    )
    frame_id: Mapped[int] = mapped_column(ForeignKey("frames.frame_id", ondelete="CASCADE"), nullable=False)
    term_kind: Mapped[str] = mapped_column(String(48), nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(64))

    # Generic coefficients cover the current spatial aperture plane
    # (c0 + cx*x_norm + cy*y_norm).  Pure-offset calibrations use offset_mag.
    coefficient_0: Mapped[float | None] = mapped_column(Float)
    coefficient_x: Mapped[float | None] = mapped_column(Float)
    coefficient_y: Mapped[float | None] = mapped_column(Float)
    offset_mag: Mapped[float | None] = mapped_column(Float)
    offset_err: Mapped[float | None] = mapped_column(Float)
    scatter_mad: Mapped[float | None] = mapped_column(Float)
    scatter_rms: Mapped[float | None] = mapped_column(Float)
    n_candidates: Mapped[int | None] = mapped_column(Integer)
    n_used: Mapped[int | None] = mapped_column(Integer)
    n_reference: Mapped[int | None] = mapped_column(Integer)
    accepted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    quality_status: Mapped[str | None] = mapped_column(String(48))
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "calibration_iteration_id",
            "frame_id",
            "term_kind",
            name="uq_calibration_frame_terms_iteration_frame_kind",
        ),
        Index("ix_calibration_frame_terms_iteration", "calibration_iteration_id"),
        Index("ix_calibration_frame_terms_frame", "frame_id"),
        Index("ix_calibration_frame_terms_kind", "term_kind"),
    )


class CalibrationIterationObject(Base):
    """Compact object membership/statistics used by one calibration iteration."""

    __tablename__ = "calibration_iteration_objects"

    calibration_iteration_object_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    calibration_iteration_id: Mapped[int] = mapped_column(
        ForeignKey("calibration_iterations.calibration_iteration_id", ondelete="CASCADE"), nullable=False
    )
    object_id: Mapped[int] = mapped_column(ForeignKey("objects.object_id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(48), nullable=False, default="NON_REFERENCE", server_default="NON_REFERENCE")
    is_reference: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    requested_exclusion: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    effective_exclusion: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    n_measurements: Mapped[int | None] = mapped_column(Integer)
    median_mag: Mapped[float | None] = mapped_column(Float)
    rms: Mapped[float | None] = mapped_column(Float)
    mad: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str | None] = mapped_column(String(48))
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "calibration_iteration_id",
            "object_id",
            name="uq_calibration_iteration_objects_iteration_object",
        ),
        Index("ix_calibration_iteration_objects_iteration", "calibration_iteration_id"),
        Index("ix_calibration_iteration_objects_object", "object_id"),
        Index("ix_calibration_iteration_objects_reference", "calibration_iteration_id", "is_reference"),
        Index("ix_calibration_iteration_objects_excluded", "calibration_iteration_id", "effective_exclusion"),
    )


class InterseriesCalibrationGeneration(Base):
    """Immutable Stage-3 M4 graph-calibration generation."""

    __tablename__ = "interseries_calibration_generations"

    interseries_generation_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    generation_uid: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)
    fingerprint_sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    field_id: Mapped[int] = mapped_column(ForeignKey("fields.field_id", ondelete="CASCADE"), nullable=False)
    instrument_unit_id: Mapped[int | None] = mapped_column(
        ForeignKey("instrument_units.instrument_unit_id", ondelete="SET NULL")
    )
    filter_name: Mapped[str | None] = mapped_column(String(32))
    catalog_name: Mapped[str | None] = mapped_column(String(64))
    analysis_algorithm: Mapped[str] = mapped_column(String(64), nullable=False)
    sign_convention: Mapped[str] = mapped_column(Text, nullable=False)
    reference_series_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False
    )
    analysis_status: Mapped[str] = mapped_column(String(48), nullable=False)
    graph_status: Mapped[str] = mapped_column(String(48), nullable=False)
    reliability_status: Mapped[str] = mapped_column(String(48), nullable=False)
    n_series: Mapped[int] = mapped_column(Integer, nullable=False)
    n_edges: Mapped[int] = mapped_column(Integer, nullable=False)
    graph_dof: Mapped[int] = mapped_column(Integer, nullable=False)
    chi2: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    reduced_chi2: Mapped[float | None] = mapped_column(Float)
    max_abs_standardized_residual: Mapped[float | None] = mapped_column(Float)
    min_points_per_object: Mapped[int] = mapped_column(Integer, nullable=False)
    min_common_objects: Mapped[int] = mapped_column(Integer, nullable=False)
    clip_sigma: Mapped[float] = mapped_column(Float, nullable=False)
    max_clip_iterations: Mapped[int] = mapped_column(Integer, nullable=False)
    input_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    analysis_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    lifecycle_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="COMPLETE_IMMUTABLE", server_default="COMPLETE_IMMUTABLE"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "interseries_generation_id", "field_id", name="uq_interseries_generations_id_field"
        ),
        ForeignKeyConstraint(
            ["reference_series_id", "field_id"],
            ["field_series.series_id", "field_series.field_id"],
            name="fk_interseries_generations_reference_series_field",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["interseries_generation_id", "reference_series_id"],
            ["interseries_calibration_series.interseries_generation_id",
             "interseries_calibration_series.series_id"],
            name="fk_interseries_generations_reference_member",
            ondelete="NO ACTION", onupdate="NO ACTION", deferrable=True, initially="DEFERRED",
        ),
        CheckConstraint("n_series >= 2", name="ck_interseries_generations_n_series"),
        CheckConstraint("n_edges >= 1", name="ck_interseries_generations_n_edges"),
        CheckConstraint("graph_dof >= 0", name="ck_interseries_generations_graph_dof"),
        Index("ix_interseries_generations_field_filter", "field_id", "filter_name"),
        Index("ix_interseries_generations_catalog", "catalog_name"),
        Index("ix_interseries_generations_reliability", "reliability_status"),
        Index("ix_interseries_generations_created", "created_at"),
    )


class InterseriesCalibrationSeries(Base):
    """Per-series source SCIENCE identity and additive zero-point for one M4 generation."""

    __tablename__ = "interseries_calibration_series"

    interseries_series_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    interseries_generation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    field_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    series_id: Mapped[int | None] = mapped_column(BigInteger)
    science_calibration_version_id: Mapped[int] = mapped_column(
        ForeignKey("calibration_versions.calibration_version_id", ondelete="RESTRICT"), nullable=False
    )
    offset_mag: Mapped[float] = mapped_column(Float, nullable=False)
    offset_err_mag: Mapped[float | None] = mapped_column(Float)
    is_reference_series: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    n_incident_edges: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    n_frames: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    n_science_points: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    n_objects: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    n_state_objects: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    n_ensemble_references: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    n_reference_eligible: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    median_within_series_robust_sigma_mag: Mapped[float | None] = mapped_column(Float)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        ForeignKeyConstraint(
            ["interseries_generation_id", "field_id"],
            [
                "interseries_calibration_generations.interseries_generation_id",
                "interseries_calibration_generations.field_id",
            ],
            name="fk_interseries_series_generation_field",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["series_id", "field_id"],
            ["field_series.series_id", "field_series.field_id"],
            name="fk_interseries_series_series_field",
            ondelete="SET NULL (series_id)",
        ),
        ForeignKeyConstraint(
            ["science_calibration_version_id", "field_id"],
            ["calibration_versions.calibration_version_id", "calibration_versions.field_id"],
            name="fk_interseries_series_science_cv_field",
            ondelete="NO ACTION", onupdate="NO ACTION", deferrable=False,
        ),
        ForeignKeyConstraint(
            ["science_calibration_version_id", "field_id", "series_id"],
            ["calibration_versions.calibration_version_id", "calibration_versions.field_id",
             "calibration_versions.series_id"],
            name="fk_interseries_series_science_cv_field_series",
            ondelete="NO ACTION", onupdate="NO ACTION", deferrable=False,
        ),
        UniqueConstraint(
            "interseries_generation_id", "series_id", name="uq_interseries_calibration_series_generation_series"
        ),
        UniqueConstraint(
            "interseries_generation_id", "science_calibration_version_id",
            name="uq_interseries_calibration_series_generation_science",
        ),
        Index("ix_interseries_calibration_series_generation", "interseries_generation_id"),
        Index("ix_interseries_calibration_series_series", "series_id"),
        Index("ix_interseries_calibration_series_science", "science_calibration_version_id"),
    )


class InterseriesCalibrationEdge(Base):
    """Pairwise M2 result and M3 closure diagnostic for one M4 generation."""

    __tablename__ = "interseries_calibration_edges"

    interseries_edge_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    interseries_generation_id: Mapped[int] = mapped_column(
        ForeignKey("interseries_calibration_generations.interseries_generation_id", ondelete="CASCADE"), nullable=False
    )
    left_series_id: Mapped[int] = mapped_column(
        ForeignKey(
            "field_series.series_id",
            name="fk_interseries_edges_left_series_restrict",
            ondelete="RESTRICT",
            deferrable=False,
        ),
        nullable=False,
    )
    right_series_id: Mapped[int] = mapped_column(
        ForeignKey(
            "field_series.series_id",
            name="fk_interseries_edges_right_series_restrict",
            ondelete="RESTRICT",
            deferrable=False,
        ),
        nullable=False,
    )
    measured_delta_mag: Mapped[float | None] = mapped_column(Float)
    measured_err_mag: Mapped[float | None] = mapped_column(Float)
    scatter_mag: Mapped[float | None] = mapped_column(Float)
    n_initial_calibrators: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    n_used_calibrators: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    n_rejected_calibrators: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    clip_iterations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pair_status: Mapped[str] = mapped_column(String(48), nullable=False)
    predicted_delta_mag: Mapped[float | None] = mapped_column(Float)
    closure_residual_mag: Mapped[float | None] = mapped_column(Float)
    standardized_residual: Mapped[float | None] = mapped_column(Float)
    common_reference_object_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    used_object_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    rejected_object_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        ForeignKeyConstraint(
            ["interseries_generation_id", "left_series_id"],
            ["interseries_calibration_series.interseries_generation_id",
             "interseries_calibration_series.series_id"],
            name="fk_interseries_edges_left_generation_member",
            ondelete="NO ACTION",
            onupdate="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["interseries_generation_id", "right_series_id"],
            ["interseries_calibration_series.interseries_generation_id",
             "interseries_calibration_series.series_id"],
            name="fk_interseries_edges_right_generation_member",
            ondelete="NO ACTION",
            onupdate="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint("left_series_id < right_series_id", name="ck_interseries_calibration_edges_series_order"),
        UniqueConstraint(
            "interseries_generation_id", "left_series_id", "right_series_id",
            name="uq_interseries_calibration_edges_generation_pair",
        ),
        Index("ix_interseries_calibration_edges_generation", "interseries_generation_id"),
        Index("ix_interseries_calibration_edges_left", "left_series_id"),
        Index("ix_interseries_calibration_edges_right", "right_series_id"),
        Index("ix_interseries_calibration_edges_status", "pair_status"),
    )


class InterseriesCalibrationOffset(LegacyBase):
    __tablename__ = "interseries_calibration_offsets"

    offset_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    calibration_version_id: Mapped[int] = mapped_column(
        ForeignKey("calibration_versions.calibration_version_id", ondelete="CASCADE"), nullable=False
    )
    series_id: Mapped[int | None] = mapped_column(
        ForeignKey("field_series.series_id", ondelete="CASCADE")
    )
    instrument_unit_id: Mapped[int | None] = mapped_column(
        ForeignKey("instrument_units.instrument_unit_id", ondelete="SET NULL")
    )
    filter_name: Mapped[str | None] = mapped_column(String(32))
    algorithm: Mapped[str] = mapped_column(String(64), nullable=False, default="interseries_median_v1")
    reference_label: Mapped[str] = mapped_column(String(128), nullable=False, default="global")
    offset_mag: Mapped[float] = mapped_column(Float, nullable=False)
    offset_err: Mapped[float | None] = mapped_column(Float)
    scatter_mag: Mapped[float | None] = mapped_column(Float)
    n_calibrators: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ACTIVE")
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "calibration_version_id",
            "series_id",
            "algorithm",
            "reference_label",
            name="uq_interseries_offsets_scope",
        ),
        Index("ix_interseries_offsets_calibration", "calibration_version_id"),
        Index("ix_interseries_offsets_series", "series_id"),
        Index("ix_interseries_offsets_unit_filter", "instrument_unit_id", "filter_name"),
        Index("ix_interseries_offsets_status", "status"),
    )


# ---------------------------------------------------------------------------
# Persistent field/series, pipeline provenance and event registry models.
#
# These tables existed in production databases before 0.5.39.0, but were
# created only by SQL migration scripts.  Keeping their ORM declarations in
# the same metadata makes Base.metadata the complete current schema contract
# and makes ``init-db`` / ``reset-db`` deterministic for a fresh database.
# ---------------------------------------------------------------------------


class Field(Base):
    __tablename__ = "fields"

    field_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    field_uid: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)
    field_name: Mapped[str | None] = mapped_column(Text)
    primary_object_name: Mapped[str | None] = mapped_column(Text)
    ra_center_deg: Mapped[float] = mapped_column(Float, nullable=False)
    dec_center_deg: Mapped[float] = mapped_column(Float, nullable=False)
    radius_arcmin: Mapped[float] = mapped_column(Float, nullable=False, default=30.0)
    reference_catalog_name: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="READY")
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_fields_sky", "ra_center_deg", "dec_center_deg"),
        Index("ix_fields_status", "status"),
        Index("ix_fields_name", "field_name"),
    )


class FieldSeries(Base):
    __tablename__ = "field_series"

    series_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    field_id: Mapped[int] = mapped_column(
        ForeignKey("fields.field_id", ondelete="CASCADE"), nullable=False
    )
    series_uid: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)
    instrument_unit_id: Mapped[int | None] = mapped_column(
        ForeignKey("instrument_units.instrument_unit_id", ondelete="SET NULL")
    )
    filter_name: Mapped[str | None] = mapped_column(String(32))
    time_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    time_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mjd_start: Mapped[float | None] = mapped_column(Float)
    mjd_end: Mapped[float | None] = mapped_column(Float)
    ra_center_deg: Mapped[float | None] = mapped_column(Float)
    dec_center_deg: Mapped[float | None] = mapped_column(Float)
    n_frames: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    grouping_method: Mapped[str] = mapped_column(String(64), nullable=False, default="center_time_v1")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="READY")
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("series_id", "field_id", name="uq_field_series_series_field"),
        Index("ix_field_series_field", "field_id"),
        Index("ix_field_series_instrument_unit", "instrument_unit_id"),
        Index("ix_field_series_status", "status"),
        Index("ix_field_series_time", "time_start", "time_end"),
        Index("ix_field_series_sky", "ra_center_deg", "dec_center_deg"),
    )


class FieldSeriesFrame(Base):
    __tablename__ = "field_series_frames"

    series_frame_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    series_id: Mapped[int] = mapped_column(
        ForeignKey("field_series.series_id", ondelete="CASCADE"), nullable=False
    )
    frame_id: Mapped[int] = mapped_column(
        ForeignKey("frames.frame_id", ondelete="CASCADE"), nullable=False
    )
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("series_id", "frame_id", name="uq_field_series_frames_series_frame"),
        Index("ix_field_series_frames_series", "series_id", "order_index"),
        Index("ix_field_series_frames_frame", "frame_id"),
    )


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    pipeline_run_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_uid: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    instrument_unit_id: Mapped[int | None] = mapped_column(
        ForeignKey("instrument_units.instrument_unit_id", ondelete="SET NULL")
    )
    catalog_name: Mapped[str | None] = mapped_column(Text)
    calibration_prefix: Mapped[str | None] = mapped_column(Text)
    recipe_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="RUNNING")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    log_dir: Mapped[str | None] = mapped_column(Text)
    log_file: Mapped[str | None] = mapped_column(Text)
    jsonl_log_file: Mapped[str | None] = mapped_column(Text)
    error_log_file: Mapped[str | None] = mapped_column(Text)
    command_json_file: Mapped[str | None] = mapped_column(Text)
    environment_json_file: Mapped[str | None] = mapped_column(Text)
    field_id: Mapped[int | None] = mapped_column(ForeignKey("fields.field_id", ondelete="SET NULL"))
    series_id: Mapped[int | None] = mapped_column(
        ForeignKey("field_series.series_id", ondelete="SET NULL")
    )

    __table_args__ = (
        Index("ix_pipeline_runs_instrument_unit_id", "instrument_unit_id"),
        Index("ix_pipeline_runs_catalog_name", "catalog_name"),
        Index("ix_pipeline_runs_status", "status"),
        Index("ix_pipeline_runs_field", "field_id"),
        Index("ix_pipeline_runs_series", "series_id"),
    )


class PipelineRunStep(Base):
    __tablename__ = "pipeline_run_steps"

    pipeline_run_step_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    pipeline_run_id: Mapped[int] = mapped_column(
        ForeignKey("pipeline_runs.pipeline_run_id", ondelete="CASCADE"), nullable=False
    )
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    step_name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="RUNNING")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("pipeline_run_id", "step_name", name="uq_pipeline_run_steps_run_step"),
        Index("ix_pipeline_run_steps_pipeline_run_id", "pipeline_run_id"),
        Index("ix_pipeline_run_steps_status", "status"),
    )


class PipelineLogEvent(Base):
    __tablename__ = "pipeline_log_events"

    pipeline_log_event_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    pipeline_run_id: Mapped[int] = mapped_column(
        ForeignKey("pipeline_runs.pipeline_run_id", ondelete="CASCADE"), nullable=False
    )
    pipeline_run_step_id: Mapped[int | None] = mapped_column(
        ForeignKey("pipeline_run_steps.pipeline_run_step_id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    level: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    context_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_pipeline_log_events_run", "pipeline_run_id", "pipeline_log_event_id"),
        Index("ix_pipeline_log_events_step", "pipeline_run_step_id"),
        Index("ix_pipeline_log_events_level", "level"),
        Index("ix_pipeline_log_events_event_type", "event_type"),
    )


class Artifact(Base):
    """Immutable file artifact produced or consumed by a recorded pipeline run."""

    __tablename__ = "artifacts"

    artifact_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    artifact_uid: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    pipeline_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("pipeline_runs.pipeline_run_id", ondelete="SET NULL")
    )
    pipeline_run_step_id: Mapped[int | None] = mapped_column(
        ForeignKey("pipeline_run_steps.pipeline_run_step_id", ondelete="SET NULL")
    )
    batch_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("batch_runs.batch_run_id", ondelete="SET NULL")
    )
    batch_job_attempt_id: Mapped[int | None] = mapped_column(
        ForeignKey("batch_job_attempts.batch_job_attempt_id", ondelete="SET NULL")
    )
    calibration_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("calibration_versions.calibration_version_id", ondelete="RESTRICT")
    )
    series_id: Mapped[int | None] = mapped_column(
        ForeignKey("field_series.series_id", ondelete="SET NULL")
    )
    artifact_type: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_role: Mapped[str] = mapped_column(String(48), nullable=False, default="DIAGNOSTIC")
    artifact_format: Mapped[str | None] = mapped_column(String(24))
    schema_version: Mapped[str | None] = mapped_column(String(32))
    storage_kind: Mapped[str] = mapped_column(String(24), nullable=False, default="FILE")
    uri: Mapped[str] = mapped_column(Text, nullable=False)
    publication_uri: Mapped[str | None] = mapped_column(Text)
    relative_path: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64))
    lifecycle_status: Mapped[str] = mapped_column(String(32), nullable=False, default="ACTIVE_IMMUTABLE")
    is_immutable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("pipeline_run_id", "uri", name="uq_artifacts_run_uri"),
        UniqueConstraint("batch_job_attempt_id", "uri", name="uq_artifacts_batch_attempt_uri"),
        Index("ix_artifacts_run", "pipeline_run_id", "artifact_id"),
        Index("ix_artifacts_step", "pipeline_run_step_id"),
        Index("ix_artifacts_batch_run", "batch_run_id", "artifact_id"),
        Index("ix_artifacts_batch_attempt", "batch_job_attempt_id"),
        Index("ix_artifacts_calibration", "calibration_version_id"),
        Index("ix_artifacts_series", "series_id"),
        Index("ix_artifacts_type_role", "artifact_type", "artifact_role"),
        Index("ix_artifacts_sha256", "sha256"),
        Index("ix_artifacts_publication_uri", "publication_uri"),
    )


class ArtifactPublication(Base):
    """Mutable publication/head pointer to the current immutable artifact."""

    __tablename__ = "artifact_publications"

    artifact_publication_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    publication_uri: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    artifact_id: Mapped[int] = mapped_column(
        ForeignKey("artifacts.artifact_id", ondelete="CASCADE"), nullable=False
    )
    updated_by_pipeline_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("pipeline_runs.pipeline_run_id", ondelete="SET NULL")
    )
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_artifact_publications_artifact", "artifact_id"),
        Index("ix_artifact_publications_pipeline_run", "updated_by_pipeline_run_id"),
    )


class BatchRun(Base):
    """Persistent DB-owned batch queue for one archive root and batch name."""

    __tablename__ = "batch_runs"

    batch_run_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    batch_uid: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    batch_name: Mapped[str] = mapped_column(String(128), nullable=False)
    root_path: Mapped[str] = mapped_column(Text, nullable=False)
    script_name: Mapped[str | None] = mapped_column(String(128))
    script_version: Mapped[str | None] = mapped_column(String(32))
    plan_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    inventory_sha256: Mapped[str | None] = mapped_column(String(64))
    queue_sha256: Mapped[str | None] = mapped_column(String(64))
    config_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PLANNED")
    planned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_batch_runs_name", "batch_name"),
        Index("ix_batch_runs_status", "status"),
        Index("ix_batch_runs_root", "root_path"),
    )


class BatchInventoryItem(Base):
    """Immutable inventory snapshot entry for one batch plan revision."""

    __tablename__ = "batch_inventory_items"

    batch_inventory_item_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    batch_run_id: Mapped[int] = mapped_column(
        ForeignKey("batch_runs.batch_run_id", ondelete="CASCADE"), nullable=False
    )
    plan_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    job_id: Mapped[str] = mapped_column(String(160), nullable=False)
    object_name: Mapped[str | None] = mapped_column(Text)
    observation_name: Mapped[str | None] = mapped_column(Text)
    directory_name: Mapped[str | None] = mapped_column(Text)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    relative_path: Mapped[str | None] = mapped_column(Text)
    fits_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    classification: Mapped[str | None] = mapped_column(String(48))
    exclude_reason: Mapped[str | None] = mapped_column(Text)
    selected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "batch_run_id", "plan_revision", "job_id", name="uq_batch_inventory_revision_job"
        ),
        Index("ix_batch_inventory_run_revision", "batch_run_id", "plan_revision"),
        Index("ix_batch_inventory_selected", "batch_run_id", "plan_revision", "selected"),
    )


class BatchJob(Base):
    """Stable logical queue item whose current state is derived from append-only attempts."""

    __tablename__ = "batch_jobs"

    batch_job_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    batch_run_id: Mapped[int] = mapped_column(
        ForeignKey("batch_runs.batch_run_id", ondelete="CASCADE"), nullable=False
    )
    job_id: Mapped[str] = mapped_column(String(160), nullable=False)
    object_name: Mapped[str | None] = mapped_column(Text)
    observation_name: Mapped[str | None] = mapped_column(Text)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    relative_path: Mapped[str | None] = mapped_column(Text)
    fits_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    queue_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_seen_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_seen_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    active_in_plan: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    current_attempt_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_status: Mapped[str] = mapped_column(String(48), nullable=False, default="PENDING")
    current_health: Mapped[str] = mapped_column(String(48), nullable=False, default="PENDING")
    current_science_status: Mapped[str | None] = mapped_column(String(64))
    latest_pipeline_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("pipeline_runs.pipeline_run_id", ondelete="SET NULL")
    )
    exit_code: Mapped[int | None] = mapped_column(Integer)
    catalog_name: Mapped[str | None] = mapped_column(Text)
    catalog_sources: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    pipeline_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_points: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    accepted_points: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    elapsed_sec: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    manifest_path: Mapped[str | None] = mapped_column(Text)
    console_log_path: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str | None] = mapped_column(Text)
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("batch_run_id", "job_id", name="uq_batch_jobs_run_job"),
        Index("ix_batch_jobs_run_queue", "batch_run_id", "active_in_plan", "queue_index"),
        Index("ix_batch_jobs_status", "batch_run_id", "current_status"),
        Index("ix_batch_jobs_object", "batch_run_id", "object_name"),
        Index("ix_batch_jobs_pipeline_run", "latest_pipeline_run_id"),
    )


class BatchJobAttempt(Base):
    """Append-only execution/recovery attempt for one batch job."""

    __tablename__ = "batch_job_attempts"

    batch_job_attempt_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    batch_job_id: Mapped[int] = mapped_column(
        ForeignKey("batch_jobs.batch_job_id", ondelete="CASCADE"), nullable=False
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="EXECUTION")
    pipeline_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("pipeline_runs.pipeline_run_id", ondelete="SET NULL")
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_code: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(48), nullable=False, default="RUNNING")
    health: Mapped[str] = mapped_column(String(48), nullable=False, default="RUNNING")
    science_status: Mapped[str | None] = mapped_column(String(64))
    catalog_name: Mapped[str | None] = mapped_column(Text)
    catalog_sources: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    pipeline_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_points: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    accepted_points: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    elapsed_sec: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    manifest_path: Mapped[str | None] = mapped_column(Text)
    console_log_path: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("batch_job_id", "attempt_no", name="uq_batch_job_attempts_job_attempt"),
        Index("ix_batch_job_attempts_job", "batch_job_id", "attempt_no"),
        Index("ix_batch_job_attempts_status", "status"),
        Index("ix_batch_job_attempts_pipeline_run", "pipeline_run_id"),
    )


class EventRegistry(Base):
    __tablename__ = "event_registry"

    event_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_uid: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    event_subtype: Mapped[str | None] = mapped_column(String(64))
    source_catalog: Mapped[str | None] = mapped_column(String(64))
    source_event_id: Mapped[str | None] = mapped_column(Text)
    external_catalog: Mapped[str | None] = mapped_column(String(64))
    external_id: Mapped[str | None] = mapped_column(Text)
    target_name: Mapped[str | None] = mapped_column(Text)
    ra_deg: Mapped[float | None] = mapped_column(Float)
    dec_deg: Mapped[float | None] = mapped_column(Float)
    position_error_arcsec: Mapped[float | None] = mapped_column(Float)
    object_id: Mapped[int | None] = mapped_column(ForeignKey("objects.object_id", ondelete="SET NULL"))
    t_start_bjd_tdb: Mapped[float | None] = mapped_column(Float)
    t_peak_bjd_tdb: Mapped[float | None] = mapped_column(Float)
    t_end_bjd_tdb: Mapped[float | None] = mapped_column(Float)
    t0_bjd_tdb: Mapped[float | None] = mapped_column(Float)
    period_days: Mapped[float | None] = mapped_column(Float)
    duration_hours: Mapped[float | None] = mapped_column(Float)
    expected_sign: Mapped[str | None] = mapped_column(String(32))
    expected_depth_ppt: Mapped[float | None] = mapped_column(Float)
    expected_amplitude_mag: Mapped[float | None] = mapped_column(Float)
    priority: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="NEW")
    event_model_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    raw_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_event_registry_type", "event_type"),
        Index("ix_event_registry_source", "source_catalog", "source_event_id"),
        Index("ix_event_registry_external", "external_catalog", "external_id"),
        Index("ix_event_registry_sky", "ra_deg", "dec_deg"),
        Index("ix_event_registry_time_start", "t_start_bjd_tdb"),
        Index("ix_event_registry_time_peak", "t_peak_bjd_tdb"),
        Index("ix_event_registry_object", "object_id"),
    )


class EventObjectLink(Base):
    __tablename__ = "event_object_links"

    event_object_link_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("event_registry.event_id", ondelete="CASCADE"), nullable=False
    )
    object_id: Mapped[int] = mapped_column(
        ForeignKey("objects.object_id", ondelete="CASCADE"), nullable=False
    )
    match_type: Mapped[str] = mapped_column(String(64), nullable=False)
    separation_arcsec: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float | None] = mapped_column(Float)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("event_id", "object_id", "match_type", name="uq_event_object_link"),
        Index("ix_event_object_links_event", "event_id"),
        Index("ix_event_object_links_object", "object_id"),
    )


class EventFrameCoverage(Base):
    __tablename__ = "event_frame_coverage"

    event_frame_coverage_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("event_registry.event_id", ondelete="CASCADE"), nullable=False
    )
    frame_id: Mapped[int] = mapped_column(ForeignKey("frames.frame_id", ondelete="CASCADE"), nullable=False)
    object_id: Mapped[int | None] = mapped_column(ForeignKey("objects.object_id", ondelete="CASCADE"))
    calibration_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("calibration_versions.calibration_version_id", ondelete="RESTRICT")
    )
    instrument_unit_id: Mapped[int | None] = mapped_column(
        ForeignKey("instrument_units.instrument_unit_id", ondelete="SET NULL")
    )
    time_relation: Mapped[str | None] = mapped_column(String(32))
    frame_time_jd_approx: Mapped[float | None] = mapped_column(Float)
    measurement_id: Mapped[int | None] = mapped_column(
        ForeignKey("measurements.measurement_id", ondelete="RESTRICT")
    )
    mag_cal: Mapped[float | None] = mapped_column(Float)
    mag_err: Mapped[float | None] = mapped_column(Float)
    frame_contains_position: Mapped[bool | None] = mapped_column(Boolean)
    distance_to_frame_edge_pix: Mapped[float | None] = mapped_column(Float)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "event_id",
            "frame_id",
            "object_id",
            "calibration_version_id",
            name="uq_event_frame_coverage",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_event_frame_coverage_event", "event_id"),
        Index("ix_event_frame_coverage_frame", "frame_id"),
        Index("ix_event_frame_coverage_object", "object_id"),
        Index("ix_event_frame_coverage_time_relation", "time_relation"),
    )


class EventDetectionLink(Base):
    __tablename__ = "event_detection_links"

    event_detection_link_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("event_registry.event_id", ondelete="CASCADE"), nullable=False
    )
    source_detection_id: Mapped[int] = mapped_column(
        ForeignKey("source_detections.source_detection_id", ondelete="CASCADE"), nullable=False
    )
    frame_id: Mapped[int] = mapped_column(ForeignKey("frames.frame_id", ondelete="CASCADE"), nullable=False)
    object_id: Mapped[int | None] = mapped_column(ForeignKey("objects.object_id", ondelete="SET NULL"))
    instrument_unit_id: Mapped[int | None] = mapped_column(
        ForeignKey("instrument_units.instrument_unit_id", ondelete="SET NULL")
    )
    time_relation: Mapped[str | None] = mapped_column(String(32))
    frame_time_jd_approx: Mapped[float | None] = mapped_column(Float)
    separation_arcsec: Mapped[float | None] = mapped_column(Float)
    detection_snr: Mapped[float | None] = mapped_column(Float)
    detection_flux: Mapped[float | None] = mapped_column(Float)
    peak_adu: Mapped[float | None] = mapped_column(Float)
    catalog_matched: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    nearest_catalog_name: Mapped[str | None] = mapped_column(String(64))
    nearest_catalog_source_id: Mapped[str | None] = mapped_column(String(128))
    nearest_catalog_sep_arcsec: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float | None] = mapped_column(Float)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "event_id", "source_detection_id", name="uq_event_detection_links_event_detection"
        ),
        Index("ix_event_detection_links_event", "event_id"),
        Index("ix_event_detection_links_detection", "source_detection_id"),
        Index("ix_event_detection_links_frame", "frame_id"),
        Index("ix_event_detection_links_object", "object_id"),
        Index("ix_event_detection_links_instrument_unit", "instrument_unit_id"),
        Index("ix_event_detection_links_time_relation", "time_relation"),
        Index("ix_event_detection_links_catalog_matched", "catalog_matched"),
        Index("ix_event_detection_links_snr", "detection_snr"),
        Index("ix_event_detection_links_sep", "separation_arcsec"),
    )
