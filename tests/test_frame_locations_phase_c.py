from __future__ import annotations

from datetime import datetime, timezone

from grandgousier.db.models import Frame, FrameType


def test_pending_file_metadata_is_preserved_when_locator_is_assigned_last() -> None:
    frame = Frame(
        frame_uid="frame-phase-c-order",
        instrument="MASTER",
        image_type=FrameType.SCIENCE,
    )
    mtime = datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc)

    frame.file_size_bytes = 123456
    frame.file_mtime_utc = mtime
    frame.raw_path = r"C:\archive\moved\science.fits"

    assert frame.raw_path == r"C:\archive\moved\science.fits"
    assert frame.file_size_bytes == 123456
    assert frame.file_mtime_utc == mtime
    assert len(frame.locations) == 1
    assert frame.locations[0].file_size_bytes == 123456
    assert frame.locations[0].file_mtime_utc == mtime
    assert frame.locations[0].is_primary is True
