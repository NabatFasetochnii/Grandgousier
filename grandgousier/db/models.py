from __future__ import annotations

import enum
import hashlib
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


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
        ),
        Index("ix_instrument_units_identity", "instrument", "site", "tube", "camera", "filter_name"),
    )


class Frame(Base):
    __tablename__ = "frames"

    frame_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    frame_uid: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)

    raw_path: Mapped[str] = mapped_column(Text, nullable=False)
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    file_mtime_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

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
    events: Mapped[list["ProcessingEvent"]] = relationship(
        back_populates="frame", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_frames_instrument_unit", "instrument", "site", "tube", "camera", "filter_name"),
        Index("ix_frames_instrument_unit_id", "instrument_unit_id"),
        Index("ix_frames_date_obs", "date_obs"),
        Index("ix_frames_status", "processing_status"),
        Index("ix_frames_image_type", "image_type"),
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


class ReferencePhotometry(Base):
    __tablename__ = "reference_photometry"

    refphot_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    refcat_id: Mapped[int | None] = mapped_column(
        ForeignKey("reference_photometry_catalogs.refcat_id", ondelete="SET NULL")
    )
    catalog_name: Mapped[str] = mapped_column(String(64), nullable=False)
    catalog_source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    ref_uid: Mapped[str] = mapped_column(String(192), nullable=False, unique=True)

    gaia_catalog_name: Mapped[str | None] = mapped_column(String(64))
    gaia_source_id: Mapped[str | None] = mapped_column(String(128))
    gaia_catalog_source_id: Mapped[int | None] = mapped_column(
        ForeignKey("catalog_sources.catalog_source_id", ondelete="SET NULL")
    )
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

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    catalog: Mapped["ReferencePhotometryCatalog | None"] = relationship()
    gaia_catalog_source: Mapped["CatalogSource | None"] = relationship()

    __table_args__ = (
        UniqueConstraint("catalog_name", "catalog_source_id", name="uq_reference_photometry_catalog_source"),
        Index("ix_reference_photometry_catalog", "catalog_name"),
        Index("ix_reference_photometry_sky", "ra_deg", "dec_deg"),
        Index("ix_reference_photometry_gaia", "gaia_catalog_name", "gaia_source_id"),
        Index("ix_reference_photometry_gaia_source", "gaia_catalog_source_id"),
    )


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


class SourceDetection(Base):
    __tablename__ = "source_detections"

    source_detection_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    frame_id: Mapped[int] = mapped_column(ForeignKey("frames.frame_id", ondelete="CASCADE"), nullable=False)

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
    algorithm: Mapped[str] = mapped_column(String(64), nullable=False, default="numpy_localmax_v1")
    parameters: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    flags: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    frame: Mapped["Frame"] = relationship()

    __table_args__ = (
        Index("ix_source_detections_frame", "frame_id"),
        Index("ix_source_detections_status", "detection_status"),
        Index("ix_source_detections_sky", "ra_deg", "dec_deg"),
        Index("ix_source_detections_snr", "snr"),
    )


class DetectionCatalogMatch(Base):
    __tablename__ = "detection_catalog_matches"

    detection_catalog_match_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    frame_id: Mapped[int] = mapped_column(ForeignKey("frames.frame_id", ondelete="CASCADE"), nullable=False)
    source_detection_id: Mapped[int] = mapped_column(
        ForeignKey("source_detections.source_detection_id", ondelete="CASCADE"), nullable=False
    )
    catalog_source_id: Mapped[int] = mapped_column(
        ForeignKey("catalog_sources.catalog_source_id", ondelete="CASCADE"), nullable=False
    )

    catalog_name: Mapped[str] = mapped_column(String(64), nullable=False)
    algorithm: Mapped[str] = mapped_column(String(64), nullable=False, default="nearest_sky_v1")
    match_status: Mapped[str] = mapped_column(String(32), nullable=False, default="MATCHED")

    sep_arcsec: Mapped[float | None] = mapped_column(Float)
    dx_pix: Mapped[float | None] = mapped_column(Float)
    dy_pix: Mapped[float | None] = mapped_column(Float)
    detection_mag_inst: Mapped[float | None] = mapped_column(Float)
    catalog_mag: Mapped[float | None] = mapped_column(Float)
    zero_point: Mapped[float | None] = mapped_column(Float)

    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    frame: Mapped["Frame"] = relationship()
    source_detection: Mapped["SourceDetection"] = relationship()
    catalog_source: Mapped["CatalogSource"] = relationship()

    __table_args__ = (
        UniqueConstraint(
            "source_detection_id",
            "catalog_source_id",
            "algorithm",
            name="uq_detection_catalog_matches_detection_source_algorithm",
        ),
        UniqueConstraint(
            "frame_id",
            "catalog_source_id",
            "catalog_name",
            "algorithm",
            name="uq_detection_catalog_matches_frame_source_catalog_algorithm",
        ),
        Index("ix_detection_catalog_matches_frame", "frame_id"),
        Index("ix_detection_catalog_matches_catalog", "catalog_name"),
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
    ra_deg: Mapped[float] = mapped_column(Float, nullable=False)
    dec_deg: Mapped[float] = mapped_column(Float, nullable=False)
    gaia_dr3_id: Mapped[str | None] = mapped_column(String(64))
    external_ids: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    ref_mags: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    colors: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    pmra_masyr: Mapped[float | None] = mapped_column(Float)
    pmdec_masyr: Mapped[float | None] = mapped_column(Float)
    crowding_index: Mapped[float | None] = mapped_column(Float)
    object_flags: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (Index("ix_objects_sky", "ra_deg", "dec_deg"),)




class ObjectQc(Base):
    __tablename__ = "object_qc"

    object_qc_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    object_id: Mapped[int] = mapped_column(ForeignKey("objects.object_id", ondelete="CASCADE"), nullable=False)
    calibration_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("calibration_versions.calibration_version_id", ondelete="SET NULL")
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
        ),
        Index("ix_object_qc_object", "object_id"),
        Index("ix_object_qc_catalog", "catalog_name"),
        Index("ix_object_qc_instrument_unit", "instrument_unit_id"),
        Index("ix_object_qc_flags", "flags"),
        Index("ix_object_qc_nn", "nearest_neighbor_arcsec"),
        Index("ix_object_qc_rms", "mag_rms"),
    )


class Measurement(Base):
    __tablename__ = "measurements"

    measurement_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    object_id: Mapped[int] = mapped_column(ForeignKey("objects.object_id", ondelete="CASCADE"))
    frame_id: Mapped[int] = mapped_column(ForeignKey("frames.frame_id", ondelete="CASCADE"))
    calibration_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("calibration_versions.calibration_version_id")
    )

    x: Mapped[float | None] = mapped_column(Float)
    y: Mapped[float | None] = mapped_column(Float)
    flux: Mapped[float | None] = mapped_column(Float)
    flux_err: Mapped[float | None] = mapped_column(Float)
    mag_inst: Mapped[float | None] = mapped_column(Float)
    mag_cal: Mapped[float | None] = mapped_column(Float)
    mag_err: Mapped[float | None] = mapped_column(Float)
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
    )


class CalibrationVersion(Base):
    __tablename__ = "calibration_versions"

    calibration_version_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    instrument_unit_id: Mapped[int | None] = mapped_column(
        ForeignKey("instrument_units.instrument_unit_id", ondelete="SET NULL")
    )
    source_calibration_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("calibration_versions.calibration_version_id", ondelete="SET NULL")
    )
    source_calibration: Mapped["CalibrationVersion | None"] = relationship(
        "CalibrationVersion",
        remote_side="CalibrationVersion.calibration_version_id",
        foreign_keys="CalibrationVersion.source_calibration_version_id",
        post_update=True,
    )
    instrument_profile: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    used_catalogs: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    parameters: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("ix_calibration_versions_source", "source_calibration_version_id"),)


class InterseriesCalibrationOffset(Base):
    __tablename__ = "interseries_calibration_offsets"

    offset_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    calibration_version_id: Mapped[int] = mapped_column(
        ForeignKey("calibration_versions.calibration_version_id", ondelete="CASCADE"), nullable=False
    )
    # field_series is migration-managed, not ORM-managed in this MVP branch.
    series_id: Mapped[int | None] = mapped_column(BigInteger)
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

    calibration_version: Mapped["CalibrationVersion"] = relationship()
    instrument_unit: Mapped["InstrumentUnit | None"] = relationship()

    __table_args__ = (
        UniqueConstraint(
            "calibration_version_id",
            "series_id",
            "algorithm",
            "reference_label",
            name="uq_interseries_offsets_scope_model",
        ),
        Index("ix_interseries_offsets_calibration_model", "calibration_version_id"),
        Index("ix_interseries_offsets_series_model", "series_id"),
        Index("ix_interseries_offsets_unit_filter_model", "instrument_unit_id", "filter_name"),
        Index("ix_interseries_offsets_status_model", "status"),
    )
