from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

CHECKPOINT_SCHEMA = "grandgousier.stage1.checkpoint.v1"
CHECKPOINT_STATES = {"RUNNING", "INTERRUPTED", "FAILED", "COMPLETE"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def execution_fingerprint(
    *,
    source: Mapping[str, Any],
    request: Mapping[str, Any],
    product_store: Mapping[str, Any],
    batch_size: int,
    overwrite: bool,
    max_dark_temperature_delta_c: float | None,
    software_version: str,
) -> str:
    payload = {
        "schema": CHECKPOINT_SCHEMA,
        "source": dict(source),
        "request": dict(request),
        "product_store": dict(product_store),
        "batch_size": int(batch_size),
        "overwrite": bool(overwrite),
        "max_dark_temperature_delta_c": max_dark_temperature_delta_c,
        "software_version": software_version,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def item_fingerprint(*, run_fingerprint: str, item: Mapping[str, Any]) -> str:
    observation = item.get("observation") if isinstance(item.get("observation"), Mapping) else {}
    product = item.get("product") if isinstance(item.get("product"), Mapping) else {}
    assets = observation.get("assets") if isinstance(observation.get("assets"), list) else []
    identity = {
        "run_fingerprint": run_fingerprint,
        "source_observation_uid": observation.get("source_observation_uid"),
        "observation_uid": observation.get("observation_uid"),
        "native_observation_id": observation.get("native_observation_id"),
        "action": item.get("action"),
        "product": product,
        "assets": [
            {
                "role": asset.get("role"),
                "locator": asset.get("locator"),
                "checksum_sha256": asset.get("checksum_sha256"),
            }
            for asset in assets
            if isinstance(asset, Mapping)
        ],
    }
    return hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()


class CheckpointMismatchError(RuntimeError):
    pass


class CheckpointStateError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CheckpointState:
    run_id: str
    status: str
    source_id: str
    config_fingerprint: str
    started_at: str
    updated_at: str
    discovered: int
    planned: int
    attempted: int
    completed: int
    skipped: int
    failed: int


class Stage1Checkpoint:
    def __init__(self, connection: sqlite3.Connection, path: Path) -> None:
        self.connection = connection
        self.path = path

    @classmethod
    def open(
        cls,
        path: str | Path,
        *,
        source_id: str,
        config_fingerprint: str,
        resume: bool,
        restart: bool,
    ) -> "Stage1Checkpoint":
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and not resume and not restart:
            raise CheckpointStateError(
                f"Checkpoint already exists: {target}; use --resume or --restart-run"
            )
        if restart and target.exists():
            archived = target.with_name(target.name + f".{uuid.uuid4().hex}.previous")
            os.replace(target, archived)
        partial = target.with_name(target.name + ".part")
        resume_artifact_exists = target.exists() or partial.exists()
        if resume and not resume_artifact_exists:
            raise CheckpointStateError(f"Cannot resume missing checkpoint: {target}")
        if restart and partial.exists():
            partial.unlink()
        if target.exists() and partial.exists():
            partial.unlink()
        if not target.exists():
            if partial.exists():
                try:
                    with sqlite3.connect(partial) as probe:
                        valid = probe.execute("PRAGMA integrity_check").fetchone() == ("ok",)
                    if not valid:
                        raise sqlite3.DatabaseError("checkpoint partial integrity check failed")
                    os.replace(partial, target)
                except (OSError, sqlite3.DatabaseError):
                    partial.unlink(missing_ok=True)
            if not target.exists():
                connection = sqlite3.connect(partial)
                try:
                    cls._configure(connection)
                    cls._create_schema(connection)
                    connection.commit()
                finally:
                    connection.close()
                os.replace(partial, target)
        if resume and resume_artifact_exists and not target.exists():
            raise CheckpointStateError(f"Cannot resume missing checkpoint: {target}")

        existed = target.exists()
        connection = sqlite3.connect(target)
        try:
            cls._configure(connection)
            if existed:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if tables and "checkpoint_meta" not in tables:
                    raise CheckpointStateError(f"Existing SQLite file is not a Grandgousier checkpoint: {target}")
            cls._create_schema(connection)
            marker = connection.execute(
                "SELECT schema FROM checkpoint_meta WHERE id=1"
            ).fetchone()
            if marker != (CHECKPOINT_SCHEMA,):
                raise CheckpointStateError(f"Checkpoint schema marker is incompatible: {target}")
        except Exception:
            connection.close()
            raise
        checkpoint = cls(connection, target)
        existing = checkpoint._state_row()
        if existing is None:
            now = _utc_now()
            run_id = uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO run_state
                    (id, run_id, status, source_id, config_fingerprint, started_at, updated_at,
                     discovered, planned, attempted, completed, skipped, failed)
                VALUES (1, ?, 'RUNNING', ?, ?, ?, ?, 0, 0, 0, 0, 0, 0)
                """,
                (run_id, source_id, config_fingerprint, now, now),
            )
            connection.commit()
        else:
            if existing[2] != source_id or existing[3] != config_fingerprint:
                connection.close()
                raise CheckpointMismatchError(
                    "Checkpoint source/config fingerprint does not match this Stage-1 execution"
                )
            if existing[1] == "COMPLETE" and not restart:
                connection.close()
                raise CheckpointStateError("Checkpoint is complete; use --restart-run for a new execution")
            if not resume and not restart:
                raise CheckpointStateError("Checkpoint requires explicit resume")
            checkpoint._set_status("RUNNING")
        return checkpoint

    @staticmethod
    def _configure(connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS run_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                run_id TEXT NOT NULL,
                status TEXT NOT NULL,
                source_id TEXT NOT NULL,
                config_fingerprint TEXT NOT NULL,
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                discovered INTEGER NOT NULL,
                planned INTEGER NOT NULL,
                attempted INTEGER NOT NULL,
                completed INTEGER NOT NULL,
                skipped INTEGER NOT NULL,
                failed INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS checkpoint_meta (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                schema TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS completed_items (
                item_key TEXT PRIMARY KEY,
                result_json TEXT NOT NULL,
                completed_at TEXT NOT NULL
            );
            INSERT OR IGNORE INTO checkpoint_meta(id, schema) VALUES (1, 'grandgousier.stage1.checkpoint.v1');
            """
        )

    def _state_row(self):
        return self.connection.execute(
            "SELECT run_id, status, source_id, config_fingerprint, started_at, updated_at, "
            "discovered, planned, attempted, completed, skipped, failed FROM run_state WHERE id=1"
        ).fetchone()

    def state(self) -> CheckpointState:
        row = self._state_row()
        if row is None:
            raise CheckpointStateError("Checkpoint run state is missing")
        return CheckpointState(*row)

    def _set_status(self, status: str) -> None:
        if status not in CHECKPOINT_STATES:
            raise ValueError(f"Unsupported checkpoint status: {status}")
        self.connection.execute(
            "UPDATE run_state SET status=?, updated_at=? WHERE id=1", (status, _utc_now())
        )
        self.connection.commit()

    def record_discovery(self, *, discovered: int, planned: int) -> None:
        self.connection.execute(
            "UPDATE run_state SET discovered=discovered+?, planned=planned+?, updated_at=? WHERE id=1",
            (int(discovered), int(planned), _utc_now()),
        )
        self.connection.commit()

    def mark_attempted(self) -> None:
        self.connection.execute(
            "UPDATE run_state SET attempted=attempted+1, updated_at=? WHERE id=1", (_utc_now(),)
        )
        self.connection.commit()

    def get_completed(self, item_key: str) -> Mapping[str, Any] | None:
        row = self.connection.execute(
            "SELECT result_json FROM completed_items WHERE item_key=?", (item_key,)
        ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row[0])
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, Mapping) else None

    def mark_completed(self, *, item_key: str, result: Mapping[str, Any]) -> None:
        status = str(result.get("status") or "")
        skipped = 1 if status in {"SKIPPED_EXISTING", "ADOPTED_EXISTING"} else 0
        payload = json.dumps(dict(result), ensure_ascii=False, sort_keys=True)
        now = _utc_now()
        self.connection.execute("BEGIN IMMEDIATE")
        already_completed = self.connection.execute(
            "SELECT 1 FROM completed_items WHERE item_key=?", (item_key,)
        ).fetchone() is not None
        self.connection.execute(
            """
            INSERT INTO completed_items(item_key, result_json, completed_at)
            VALUES (?, ?, ?)
            ON CONFLICT(item_key) DO UPDATE SET result_json=excluded.result_json,
                completed_at=excluded.completed_at
            """,
            (item_key, payload, now),
        )
        self.connection.execute(
            "UPDATE run_state SET completed=completed+?, skipped=skipped+?, updated_at=? WHERE id=1",
            (0 if already_completed else 1, 0 if already_completed else skipped, now),
        )
        self.connection.commit()

    def mark_failed(self) -> None:
        self.connection.execute(
            "UPDATE run_state SET failed=failed+1, updated_at=? WHERE id=1", (_utc_now(),)
        )
        self.connection.commit()

    def set_terminal_status(self, status: str) -> None:
        self._set_status(status)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Stage1Checkpoint":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


__all__ = [
    "CHECKPOINT_SCHEMA",
    "CheckpointMismatchError",
    "CheckpointState",
    "CheckpointStateError",
    "Stage1Checkpoint",
    "execution_fingerprint",
    "item_fingerprint",
]
