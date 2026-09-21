from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class JobEventWriter:
    """Append-only JSONL event sink for server/job observability."""

    def __init__(self, path: str | Path, *, job_id: str) -> None:
        self.path = Path(path)
        self.job_id = str(job_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, *, stage: str, status: str | None = None, **details: Any) -> None:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "job_id": self.job_id,
            "event": str(event),
            "stage": str(stage),
            "status": status,
            **details,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str, sort_keys=True))
            handle.write("\n")
