from __future__ import annotations

from sqlalchemy.orm import Session

from grandgousier.db.models import Frame, FrameStatus, ProcessingEvent, ProcessingEventLevel


def set_frame_status(
    session: Session,
    frame: Frame,
    status: FrameStatus,
    stage: str,
    message: str,
    details: dict | None = None,
    level: ProcessingEventLevel = ProcessingEventLevel.INFO,
) -> None:
    frame.processing_status = status
    session.add(
        ProcessingEvent(
            frame=frame,
            stage=stage,
            level=level,
            message=message,
            details=details or {},
        )
    )
    session.commit()
