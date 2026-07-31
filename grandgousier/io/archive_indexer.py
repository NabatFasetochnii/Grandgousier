from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from tqdm import tqdm

from grandgousier.db.models import (
    FileReferenceStatus,
    Frame,
    FrameStatus,
    FrameType,
    InstrumentUnit,
    ProcessingEvent,
    ProcessingEventLevel,
    make_instrument_unit_uid,
)
from grandgousier.instruments.base import InstrumentProfile
from grandgousier.io.fits_header import header_to_jsonable, read_primary_header
from grandgousier.io.frame_uid import make_frame_uid
from grandgousier.io.parsing import parse_datetime, parse_dec_deg, parse_ra_deg, to_float, to_int

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IndexResult:
    discovered: int = 0
    inserted: int = 0
    skipped_existing: int = 0
    failed: int = 0


def iter_fits_files(root: str | Path, pattern: str = "*.fits", recursive: bool = True) -> Iterable[Path]:
    root_path = Path(root)
    if recursive:
        yield from root_path.rglob(pattern)
    else:
        yield from root_path.glob(pattern)


def build_frame_from_path(path: Path, profile: InstrumentProfile) -> Frame:
    stat = path.stat()
    header = read_primary_header(path)
    hj = header_to_jsonable(header)

    image_type = profile.normalize_image_type(profile.get_header_value(header, "image_type"))
    try:
        image_type_enum = FrameType(image_type)
    except ValueError:
        image_type_enum = FrameType.UNKNOWN

    jd = to_float(profile.get_header_value(header, "jd"))
    mjd = to_float(profile.get_header_value(header, "mjd"))

    if mjd is None and jd is not None:
        mjd = jd - 2400000.5

    if jd is None and mjd is not None:
        jd = mjd + 2400000.5

    flat_status = _header_reference_status(profile.get_header_value(header, "flat_path"))
    dark_status = _header_reference_status(profile.get_header_value(header, "dark_path"))
    sexcat_status = _header_reference_status(
        profile.get_header_value(header, "sexcat_path")
        or profile.get_header_value(header, "sextractor_catalog_path")
    )

    frame = Frame(
        frame_uid=make_frame_uid(path, stat.st_size, stat.st_mtime_ns),
        raw_path=str(path),
        file_size_bytes=stat.st_size,
        file_mtime_utc=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        instrument=profile.name,
        site=_as_str(profile.get_header_value(header, "site")),
        tube=_as_str(profile.get_header_value(header, "tube")),
        camera=_as_str(profile.get_header_value(header, "camera")),
        filter_name=profile.normalize_filter(profile.get_header_value(header, "filter")),
        observing_mode=_as_str(profile.get_header_value(header, "observing_mode")),
        image_type=image_type_enum,
        date_obs=parse_datetime(profile.get_header_value(header, "date_obs")),
        flat_status=flat_status.value,
        dark_status=dark_status.value,
        sexcat_status=sexcat_status.value,
        jd=jd,
        mjd=mjd,
        exptime=to_float(profile.get_header_value(header, "exptime")),
        object_name=_as_str(profile.get_header_value(header, "object")),
        ra_center_deg=parse_ra_deg(profile.get_header_value(header, "ra_center")),
        dec_center_deg=parse_dec_deg(profile.get_header_value(header, "dec_center")),
        airmass=to_float(profile.get_header_value(header, "airmass")),
        ccd_temp=to_float(profile.get_header_value(header, "ccd_temp")),
        gain=to_float(profile.get_header_value(header, "gain")) or profile.nominal_gain_e_per_adu,
        read_noise=to_float(profile.get_header_value(header, "read_noise")) or profile.nominal_read_noise_e,
        binning_x=to_int(profile.get_header_value(header, "binning_x")),
        binning_y=to_int(profile.get_header_value(header, "binning_y")),
        processing_status=FrameStatus.HEADER_READ,
        quality_flags={},
        header_json=hj,
    )
    return frame


def get_or_create_instrument_unit(
    session: Session, profile: InstrumentProfile, frame: Frame
) -> InstrumentUnit:
    unit_uid = make_instrument_unit_uid(
        instrument=frame.instrument,
        site=frame.site,
        tube=frame.tube,
        camera=frame.camera,
        filter_name=frame.filter_name,
        observing_mode=frame.observing_mode,
    )
    unit = session.query(InstrumentUnit).filter(InstrumentUnit.unit_uid == unit_uid).one_or_none()
    if unit is not None:
        return unit

    unit = InstrumentUnit(
        unit_uid=unit_uid,
        instrument=frame.instrument,
        site=frame.site,
        tube=frame.tube,
        camera=frame.camera,
        filter_name=frame.filter_name,
        observing_mode=frame.observing_mode,
        profile_name=profile.name,
        profile_version=profile.version,
        pixel_scale_arcsec=profile.pixel_scale_arcsec,
        saturation_level_adu=profile.saturation_level_adu,
        nominal_gain_e_per_adu=profile.nominal_gain_e_per_adu,
        nominal_read_noise_e=profile.nominal_read_noise_e,
        config_json=profile.raw_config or {},
    )
    session.add(unit)
    session.flush()
    return unit


def index_archive(
    session: Session,
    root: str | Path,
    profile: InstrumentProfile,
    pattern: str = "*.fits",
    recursive: bool = True,
    dry_run: bool = False,
    limit: int | None = None,
) -> IndexResult:
    paths = iter_fits_files(root, pattern=pattern, recursive=recursive)
    return index_archive_paths(
        session=session,
        paths=paths,
        profile=profile,
        dry_run=dry_run,
        limit=limit,
    )


def index_archive_paths(
    session: Session,
    paths: Iterable[str | Path],
    profile: InstrumentProfile,
    dry_run: bool = False,
    limit: int | None = None,
    progress_desc: str = "Indexing FITS",
) -> IndexResult:
    """Index an explicit iterable of FITS paths.

    This is the list-based sibling of :func:`index_archive`.  It is useful for
    one-command archive processing where a mixed directory is first split by
    automatically detected instrument profile and each subgroup must be indexed
    with its own profile.
    """

    discovered = inserted = skipped_existing = failed = 0

    for raw_path in tqdm(paths, desc=progress_desc, unit="file"):
        if limit is not None and discovered >= limit:
            break
        path = Path(raw_path)
        discovered += 1
        try:
            frame = build_frame_from_path(path, profile)
            exists = session.query(Frame.frame_id).filter(Frame.frame_uid == frame.frame_uid).first()
            if exists:
                skipped_existing += 1
                continue
            if not dry_run:
                frame.instrument_unit = get_or_create_instrument_unit(session, profile, frame)
                session.add(frame)
                session.commit()
            inserted += 1
        except IntegrityError:
            session.rollback()
            skipped_existing += 1
        except Exception as exc:
            session.rollback()
            failed += 1
            logger.exception("Failed to index %s", path)
            if not dry_run:
                session.add(
                    ProcessingEvent(
                        frame_id=None,
                        stage="index_archive",
                        level=ProcessingEventLevel.ERROR,
                        message=f"Failed to index FITS file: {path}",
                        details={"path": str(path), "error": repr(exc)},
                    )
                )
                session.commit()

    return IndexResult(discovered, inserted, skipped_existing, failed)


def _as_str(value) -> str | None:
    if value in (None, ""):
        return None
    return str(value).strip()


def _header_reference_status(value) -> FileReferenceStatus:
    if _nonempty_header_value(value):
        return FileReferenceStatus.FROM_HEADER
    return FileReferenceStatus.MISSING_IN_HEADER


def _nonempty_header_value(value) -> bool:
    if value is None:
        return False
    text = str(value).strip()
    if not text:
        return False
    if text.upper() in {"NONE", "NULL", "N/A", "UNKNOWN", "-"}:
        return False
    return True
