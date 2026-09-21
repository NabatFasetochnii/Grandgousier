from __future__ import annotations

import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text
import yaml
import certifi

from grandgousier.db.migrations import status as migration_status
from grandgousier.db.session import make_engine
from grandgousier.resources import instrument_profile_path


_PROFILE_FILES = {
    "master": ("master.yaml",),
    "robophot": ("robophot.yaml",),
    "auto": ("master.yaml", "robophot.yaml"),
}


@dataclass(frozen=True, slots=True)
class ReadinessCheck:
    name: str
    status: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RuntimeReadinessResult:
    status: str
    checks: tuple[ReadinessCheck, ...]

    @property
    def ready(self) -> bool:
        return self.status == "READY"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "ready": self.ready,
            "checks": [check.to_dict() for check in self.checks],
        }


def check_runtime_readiness(
    *,
    database_url: str | None,
    processed_root: str | Path,
    out_root: str | Path,
    instrument_profile: str,
) -> RuntimeReadinessResult:
    """Read-only/deployment preflight for a field-archive service job.

    The only filesystem write is a temporary zero-byte writability probe inside
    ``out_root``. No migration is applied automatically.
    """

    checks: list[ReadinessCheck] = []
    root = Path(processed_root)
    if root.is_dir():
        checks.append(ReadinessCheck("processed_root", "OK", str(root)))
    else:
        checks.append(ReadinessCheck("processed_root", "FAIL", f"directory not found: {root}"))

    profile_key = str(instrument_profile or "auto").strip().casefold()
    filenames = _PROFILE_FILES.get(profile_key)
    if filenames is None:
        checks.append(
            ReadinessCheck(
                "instrument_profile",
                "FAIL",
                f"unsupported profile mode: {instrument_profile!r}",
            )
        )
    else:
        missing = [name for name in filenames if not instrument_profile_path(name).is_file()]
        if missing:
            checks.append(
                ReadinessCheck(
                    "instrument_profile",
                    "FAIL",
                    "missing package resources: " + ", ".join(missing),
                )
            )
        else:
            try:
                parsed = []
                for name in filenames:
                    path = instrument_profile_path(name)
                    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
                    if not isinstance(payload, dict) or not str(payload.get("name") or "").strip():
                        raise ValueError(f"invalid profile mapping: {path}")
                    parsed.append(str(path))
            except Exception as exc:  # noqa: BLE001
                checks.append(
                    ReadinessCheck(
                        "instrument_profile",
                        "FAIL",
                        f"cannot parse packaged profile: {type(exc).__name__}: {exc}",
                    )
                )
            else:
                checks.append(ReadinessCheck("instrument_profile", "OK", ", ".join(parsed)))

    try:
        ca_path = Path(certifi.where())
        if ca_path.is_file():
            checks.append(ReadinessCheck("ca_bundle", "OK", str(ca_path)))
        else:
            checks.append(ReadinessCheck("ca_bundle", "FAIL", f"CA bundle not found: {ca_path}"))
    except Exception as exc:  # noqa: BLE001
        checks.append(ReadinessCheck("ca_bundle", "FAIL", f"{type(exc).__name__}: {exc}"))

    out = Path(out_root)
    try:
        out.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".gg_readiness_", dir=out, delete=True):
            pass
    except OSError as exc:
        checks.append(ReadinessCheck("out_root", "FAIL", f"not writable: {out}: {exc}"))
    else:
        checks.append(ReadinessCheck("out_root", "OK", str(out)))

    engine = None
    try:
        engine = make_engine(database_url)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        checks.append(ReadinessCheck("database", "OK", "connection and SELECT 1 succeeded"))
    except Exception as exc:  # noqa: BLE001 - readiness must report, not hide, infrastructure failures.
        checks.append(ReadinessCheck("database", "FAIL", f"{type(exc).__name__}: {exc}"))
    else:
        try:
            rows = migration_status(engine)
            pending = [row.migration_id for row in rows if row.state == "PENDING"]
            drift = [row.migration_id for row in rows if row.checksum_status == "DRIFT"]
            if pending or drift:
                details = []
                if pending:
                    details.append("pending=" + ",".join(pending))
                if drift:
                    details.append("checksum_drift=" + ",".join(drift))
                checks.append(ReadinessCheck("schema_migrations", "FAIL", "; ".join(details)))
            else:
                checks.append(
                    ReadinessCheck(
                        "schema_migrations",
                        "OK",
                        f"total={len(rows)}, pending=0, checksum_drift=0",
                    )
                )
        except Exception as exc:  # noqa: BLE001
            checks.append(ReadinessCheck("schema_migrations", "FAIL", f"{type(exc).__name__}: {exc}"))
    finally:
        if engine is not None:
            engine.dispose()

    return RuntimeReadinessResult(
        status="READY" if all(check.status == "OK" for check in checks) else "FAILED",
        checks=tuple(checks),
    )
