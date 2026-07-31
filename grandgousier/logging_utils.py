from __future__ import annotations

import json
import os
import platform
import re
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


SENSITIVE_OPTION_NAMES = {
    "--database-url",
    "--db-url",
    "--password",
    "--token",
    "--api-key",
    "--secret",
}

SENSITIVE_ENV_KEYS = {
    "GRANDGOUSIER_DATABASE_URL",
    "DATABASE_URL",
    "PGPASSWORD",
}


class PipelineFileLogger:
    """Write per-pipeline-run human and JSONL logs.

    The logger is intentionally lightweight and independent from Python's global
    logging configuration. It is used by the pipeline runner to make each run
    inspectable even when the console output was lost.
    """

    def __init__(
        self,
        *,
        log_dir: Path,
        log_file: Path,
        jsonl_log_file: Path,
        error_log_file: Path,
        command_json_file: Path,
        environment_json_file: Path,
        min_level: str = "INFO",
    ) -> None:
        self.log_dir = log_dir
        self.log_file = log_file
        self.jsonl_log_file = jsonl_log_file
        self.error_log_file = error_log_file
        self.command_json_file = command_json_file
        self.environment_json_file = environment_json_file
        self.min_level = normalize_log_level(min_level)

    @classmethod
    def create(
        cls,
        *,
        root_dir: str | Path,
        pipeline_run_id: int | None,
        run_uid: str,
        command_json: dict[str, Any] | None = None,
        environment_json: dict[str, Any] | None = None,
        min_level: str = "INFO",
    ) -> "PipelineFileLogger":
        safe_run_uid = safe_filename(run_uid)
        if pipeline_run_id is not None:
            log_dir = Path(root_dir) / "pipeline_runs" / str(pipeline_run_id)
        else:
            log_dir = Path(root_dir) / "pipeline_runs" / safe_run_uid
        log_dir.mkdir(parents=True, exist_ok=True)

        logger = cls(
            log_dir=log_dir,
            log_file=log_dir / "run.log",
            jsonl_log_file=log_dir / "run.jsonl",
            error_log_file=log_dir / "errors.log",
            command_json_file=log_dir / "command.json",
            environment_json_file=log_dir / "environment.json",
            min_level=min_level,
        )
        write_json_file(logger.command_json_file, command_json or {})
        write_json_file(logger.environment_json_file, environment_json or {})
        return logger

    @property
    def paths(self) -> dict[str, str]:
        return {
            "log_dir": str(self.log_dir),
            "log_file": str(self.log_file),
            "jsonl_log_file": str(self.jsonl_log_file),
            "error_log_file": str(self.error_log_file),
            "command_json_file": str(self.command_json_file),
            "environment_json_file": str(self.environment_json_file),
        }

    def log(
        self,
        *,
        level: str,
        event_type: str,
        message: str,
        pipeline_run_id: int | None = None,
        pipeline_run_step_id: int | None = None,
        step_name: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        level = normalize_log_level(level)
        if not should_emit(level, self.min_level):
            return
        now = utc_iso()
        context_json = to_jsonable(context or {})
        step_part = f" step={step_name}" if step_name else ""
        run_part = f" run_id={pipeline_run_id}" if pipeline_run_id is not None else ""
        text_line = f"{now} {level:<7} {event_type}{run_part}{step_part} {message}".rstrip()
        if context_json:
            text_line += " " + json.dumps(context_json, ensure_ascii=False, sort_keys=True)
        append_line(self.log_file, text_line)
        append_jsonl(
            self.jsonl_log_file,
            {
                "time": now,
                "level": level,
                "event_type": event_type,
                "message": message,
                "pipeline_run_id": pipeline_run_id,
                "pipeline_run_step_id": pipeline_run_step_id,
                "step_name": step_name,
                "context": context_json,
            },
        )

    def exception(
        self,
        *,
        exc: BaseException,
        event_type: str,
        message: str,
        pipeline_run_id: int | None = None,
        pipeline_run_step_id: int | None = None,
        step_name: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        tb = traceback.format_exc()
        now = utc_iso()
        append_line(self.error_log_file, f"{now} {event_type} run_id={pipeline_run_id} step={step_name or '-'}")
        append_line(self.error_log_file, tb.rstrip())
        append_line(self.error_log_file, "")
        merged = dict(context or {})
        merged.update({"error_type": type(exc).__name__, "error_message": str(exc)})
        self.log(
            level="ERROR",
            event_type=event_type,
            message=message,
            pipeline_run_id=pipeline_run_id,
            pipeline_run_step_id=pipeline_run_step_id,
            step_name=step_name,
            context=merged,
        )


def normalize_log_level(level: str | None) -> str:
    level = (level or "INFO").upper().strip()
    aliases = {"WARN": "WARNING"}
    return aliases.get(level, level if level in LOG_LEVEL_ORDER else "INFO")


LOG_LEVEL_ORDER = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


def should_emit(level: str, min_level: str) -> bool:
    return LOG_LEVEL_ORDER.get(level, 20) >= LOG_LEVEL_ORDER.get(min_level, 20)


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def safe_filename(value: str) -> str:
    value = str(value or "run").strip()
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value)
    return value[:160] or "run"


def append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.write("\n")


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(to_jsonable(payload), ensure_ascii=False, sort_keys=True))
        handle.write("\n")


def write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sanitize_database_url(url: str | None) -> str | None:
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except Exception:
        return _mask_password_like_text(str(url))
    if not parts.netloc:
        return _mask_password_like_text(str(url))
    host_part = parts.netloc
    if "@" in host_part:
        userinfo, hostinfo = host_part.rsplit("@", 1)
        if ":" in userinfo:
            user, _password = userinfo.split(":", 1)
            host_part = f"{user}:***@{hostinfo}"
    return urlunsplit((parts.scheme, host_part, parts.path, parts.query, parts.fragment))


def sanitize_argv(argv: list[str] | tuple[str, ...] | None) -> list[str]:
    if argv is None:
        argv = sys.argv
    argv = [str(item) for item in argv]
    result: list[str] = []
    skip_next = False
    for index, item in enumerate(argv):
        if skip_next:
            result.append("***")
            skip_next = False
            continue
        if item in SENSITIVE_OPTION_NAMES:
            result.append(item)
            skip_next = True
            continue
        if item.startswith("--database-url="):
            result.append("--database-url=" + (sanitize_database_url(item.split("=", 1)[1]) or ""))
            continue
        if any(item.startswith(f"{opt}=") for opt in SENSITIVE_OPTION_NAMES if opt != "--database-url"):
            result.append(item.split("=", 1)[0] + "=***")
            continue
        result.append(_mask_password_like_text(item))
    return result


def build_command_snapshot(argv: list[str] | tuple[str, ...] | None = None) -> dict[str, Any]:
    sanitized = sanitize_argv(argv)
    return {
        "argv": sanitized,
        "command_line": " ".join(sanitized),
    }


def build_environment_snapshot(database_url: str | None = None) -> dict[str, Any]:
    try:
        from grandgousier import __version__
    except Exception:
        __version__ = None
    env = {}
    for key in sorted(SENSITIVE_ENV_KEYS):
        if key in os.environ:
            if "URL" in key:
                env[key] = sanitize_database_url(os.environ.get(key))
            else:
                env[key] = "***"
    return {
        "cwd": str(Path.cwd()),
        "python_executable": sys.executable,
        "python_version": sys.version,
        "platform": platform.platform(),
        "grandgousier_version": __version__,
        "database_url": sanitize_database_url(database_url),
        "selected_environment": env,
    }


def _mask_password_like_text(value: str) -> str:
    # Handles common SQLAlchemy URL fragments in strings that are not parsed cleanly.
    return re.sub(r"(://[^:/\s]+:)([^@\s]+)(@)", r"\1***\3", value)


def to_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
