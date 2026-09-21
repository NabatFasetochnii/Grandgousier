from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from grandgousier.db.models import Frame, FrameLocation, make_frame_location_uid


def primary_frame_location(frame: Frame) -> FrameLocation | None:
    rows = list(frame.locations or [])
    if not rows:
        return None
    primary = [row for row in rows if row.is_primary]
    if len(primary) > 1:
        raise RuntimeError(f"frame_id={frame.frame_id} has multiple primary frame_locations")
    return primary[0] if primary else rows[0]


def frame_path(frame: Frame) -> Path:
    row = primary_frame_location(frame)
    if row is None or not row.locator:
        raise ValueError(f"Frame {frame.frame_id} has no recorded location")
    if str(row.location_kind).upper() != "LOCAL_PATH":
        raise ValueError(
            f"Frame {frame.frame_id} primary location is not a local path: {row.location_kind}"
        )
    return Path(row.locator)


def find_frame_by_locator(session: Session, locator: str, *, location_kind: str = "LOCAL_PATH") -> Frame | None:
    uid = make_frame_location_uid(locator, location_kind)
    return session.scalar(
        select(Frame)
        .join(FrameLocation, FrameLocation.frame_id == Frame.frame_id)
        .where(FrameLocation.locator_uid == uid)
    )


def add_or_touch_frame_location(
    frame: Frame,
    *,
    locator: str,
    location_kind: str = "LOCAL_PATH",
    file_size_bytes: int | None = None,
    file_mtime_utc: datetime | None = None,
    content_sha256: str | None = None,
    make_primary: bool = True,
) -> FrameLocation:
    uid = make_frame_location_uid(locator, location_kind)
    now = datetime.now(timezone.utc)
    found = None
    for row in list(frame.locations or []):
        if row.locator_uid == uid:
            found = row
            break
    if found is None:
        found = FrameLocation(
            locator=str(locator),
            locator_uid=uid,
            location_kind=str(location_kind).upper(),
            is_primary=False,
            availability_status="AVAILABLE",
            file_size_bytes=file_size_bytes,
            file_mtime_utc=file_mtime_utc,
            content_sha256=content_sha256,
            last_seen_at=now,
        )
        frame.locations.append(found)
    else:
        found.locator = str(locator)
        found.location_kind = str(location_kind).upper()
        found.availability_status = "AVAILABLE"
        if file_size_bytes is not None:
            found.file_size_bytes = int(file_size_bytes)
        if file_mtime_utc is not None:
            found.file_mtime_utc = file_mtime_utc
        if content_sha256:
            found.content_sha256 = str(content_sha256)
        found.last_seen_at = now

    if make_primary:
        for row in list(frame.locations or []):
            row.is_primary = row is found
    return found


def copy_location_metadata(*, source: Frame, target: Frame, make_primary: bool = True) -> FrameLocation:
    row = primary_frame_location(source)
    if row is None:
        raise ValueError("source Frame has no location")
    return add_or_touch_frame_location(
        target,
        locator=row.locator,
        location_kind=row.location_kind,
        file_size_bytes=row.file_size_bytes,
        file_mtime_utc=row.file_mtime_utc,
        content_sha256=row.content_sha256,
        make_primary=make_primary,
    )
