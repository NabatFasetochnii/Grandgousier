from __future__ import annotations

import json
import os
import platform
import re
import sys
import traceback
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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

# Only these typed, non-secret PostgreSQL URL options retain their values in
# diagnostic URLs: numeric ``connect_timeout``, the enumerated ``sslmode``
# values, and enumerated ``target_session_attrs`` values. Free-form options
# (for example ``application_name`` or ``options``), credential options, and
# unknown driver-specific options are redacted because they can carry arbitrary
# user-provided text.
_SAFE_DATABASE_URL_QUERY_VALUES = {
    "connect_timeout": re.compile(r"[0-9]{1,5}"),
    "sslmode": frozenset(
        {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
    ),
    "target_session_attrs": frozenset(
        {"any", "read-write", "read-only", "primary", "standby", "prefer-standby"}
    ),
}
_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9a-fA-F]{2})")
_URL_IN_TEXT = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s\"'<>]+")
_SENSITIVE_TEXT_LABEL_ASSIGNMENT = re.compile(
    r"(?i)(?<![A-Za-z0-9_-])(?P<key_quote>['\"]?)"
    r"(?:password|passwd|pwd|(?:access|refresh|auth)[_-]?token|token|"
    r"(?:api|access|private|client|secret)[_-]?key|client[_-]?secret|secret|"
    r"client[_-]?credential(?:s)?|credential(?:s)?|authorization|bearer)"
    r"(?P=key_quote)[ \t]*[=:][ \t]*"
)
_TRAILING_URL_PUNCTUATION = ".,;:!?"
_URL_CLOSING_PAIRS = {")": "(", "]": "[", "}": "{"}


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
        safe_message = sanitize_diagnostic_text(message)
        context_json = sanitize_jsonable_context(context or {})
        step_part = f" step={step_name}" if step_name else ""
        run_part = f" run_id={pipeline_run_id}" if pipeline_run_id is not None else ""
        text_line = f"{now} {level:<7} {event_type}{run_part}{step_part} {safe_message}".rstrip()
        if context_json:
            text_line += " " + json.dumps(context_json, ensure_ascii=False, sort_keys=True)
        append_line(self.log_file, text_line)
        append_jsonl(
            self.jsonl_log_file,
            {
                "time": now,
                "level": level,
                "event_type": event_type,
                "message": safe_message,
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
        tb = sanitize_diagnostic_text(traceback.format_exc())
        safe_exception_message = sanitize_diagnostic_text(str(exc))
        safe_message = sanitize_diagnostic_text(message)
        now = utc_iso()
        append_line(self.error_log_file, f"{now} {event_type} run_id={pipeline_run_id} step={step_name or '-'}")
        append_line(self.error_log_file, tb.rstrip())
        append_line(self.error_log_file, "")
        merged = dict(context or {})
        merged.update({"error_type": type(exc).__name__, "error_message": safe_exception_message})
        self.log(
            level="ERROR",
            event_type=event_type,
            message=safe_message,
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
    """Redact credentials and non-allowlisted query values from a database URL.

    Malformed URL forms are replaced completely rather than risking partial
    disclosure. The URL fragment is always discarded.
    """
    if not url:
        return url
    try:
        if any(character.isspace() or ord(character) < 0x20 for character in url):
            return "***"
        if _INVALID_PERCENT_ESCAPE.search(url):
            return "***"
        parts = urlsplit(url)
        if not parts.scheme or not parts.netloc:
            return "***"
        hostname = parts.hostname
        if not hostname:
            return "***"
        # Accessing port validates bracket and port syntax (including range).
        _ = parts.port
        query_pairs = parse_qsl(
            parts.query,
            keep_blank_values=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=128,
        )
    except (UnicodeDecodeError, ValueError):
        return "***"

    host_part = parts.netloc
    if "@" in host_part:
        if host_part.count("@") != 1:
            return "***"
        userinfo, hostinfo = host_part.rsplit("@", 1)
        if ":" in userinfo:
            user, _password = userinfo.split(":", 1)
            host_part = f"{user}:***@{hostinfo}"
        else:
            # Userinfo without a password may itself be a token-like value.
            host_part = f"***@{hostinfo}"

    safe_query_pairs = []
    for name, value in query_pairs:
        option_name = name.casefold()
        safe_option = _SAFE_DATABASE_URL_QUERY_VALUES.get(option_name)
        if isinstance(safe_option, re.Pattern):
            safe_value = value if safe_option.fullmatch(value) else "***"
        elif safe_option is not None:
            safe_value = value if value in safe_option else "***"
        else:
            safe_value = "***"
        safe_query_pairs.append((name, safe_value))

    return urlunsplit(
        (parts.scheme, host_part, parts.path, urlencode(safe_query_pairs), "")
    )


def _sanitize_database_urls_in_text(value: str) -> str:
    """Redact database/credential URLs and labeled assignments in diagnostic text."""

    def replace_url(match: re.Match[str]) -> str:
        candidate = match.group(0)
        trailing = ""
        while candidate:
            ending = candidate[-1]
            unmatched_closer = ending in _URL_CLOSING_PAIRS and candidate.count(
                ending
            ) > candidate.count(_URL_CLOSING_PAIRS[ending])
            if ending not in _TRAILING_URL_PUNCTUATION and not unmatched_closer:
                break
            trailing = candidate[-1] + trailing
            candidate = candidate[:-1]
        if not _is_credential_bearing_url(candidate):
            return candidate + trailing
        sanitized = sanitize_database_url(candidate)
        return (sanitized if sanitized is not None else "") + trailing

    sanitized_urls = _URL_IN_TEXT.sub(replace_url, value)
    return _redact_sensitive_labeled_values(sanitized_urls)


def sanitize_diagnostic_text(value: str) -> str:
    """Sanitize known database URLs and labeled credentials in diagnostic text.

    This deliberately does not attempt to identify arbitrary unlabeled secrets.
    """
    return _sanitize_database_urls_in_text(str(value))


def _is_credential_bearing_url(value: str) -> bool:
    if value.casefold().startswith(("postgres://", "postgresql://", "postgresql+")):
        return True
    try:
        parts = urlsplit(value)
        if not parts.scheme or not parts.netloc:
            return False
        if "@" in parts.netloc:
            return True
        query_pairs = parse_qsl(parts.query, keep_blank_values=True, max_num_fields=128)
        fragment_pairs = parse_qsl(
            parts.fragment, keep_blank_values=True, max_num_fields=128
        )
    except ValueError:
        return True
    return any(_is_sensitive_context_key(name) for name, _value in (*query_pairs, *fragment_pairs))


def sanitize_jsonable_context(value: Any) -> Any:
    """Convert context to JSONable values and redact credential-bearing content.

    Mapping/list structure and non-sensitive values are preserved. Credential-like
    mapping keys are replaced with ``"***"``; ordinary strings receive the same
    URL and labeled-assignment handling as diagnostic messages.
    """
    return _sanitize_context_value(to_jsonable(value))


def _sanitize_context_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "***" if _is_sensitive_context_key(key) else _sanitize_context_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_context_value(item) for item in value]
    if isinstance(value, str):
        return sanitize_diagnostic_text(value)
    return value


def _is_sensitive_context_key(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(value).casefold())
    return any(
        marker in normalized
        for marker in (
            "password",
            "passwd",
            "pwd",
            "token",
            "secret",
            "credential",
            "authorization",
            "apikey",
            "accesskey",
            "privatekey",
            "bearer",
        )
    ) or normalized == "auth"


def _redact_sensitive_labeled_values(value: str) -> str:
    """Mask assignment values for recognized credential labels only."""
    parts: list[str] = []
    cursor = 0
    for match in _SENSITIVE_TEXT_LABEL_ASSIGNMENT.finditer(value):
        if match.start() < cursor:
            continue
        value_start = match.end()
        parts.append(value[cursor:value_start])

        if value_start >= len(value):
            cursor = value_start
            continue

        if value[value_start] in {'"', "'"}:
            quote = value[value_start]
            value_end = value_start + 1
            while value_end < len(value) and value[value_end] not in "\r\n":
                if value[value_end] == "\\":
                    value_end += 2
                    continue
                if value[value_end] == quote:
                    break
                value_end += 1
            if value_end < len(value) and value[value_end] == quote:
                parts.append(f"{quote}***{quote}")
                cursor = value_end + 1
            else:
                # An unterminated quote makes the rest of this line ambiguous;
                # fail closed for that line while preserving subsequent lines.
                line_end = value.find("\n", value_start)
                cursor = len(value) if line_end < 0 else line_end
                parts.append(f"{quote}***")
            continue

        value_end = value_start
        while (
            value_end < len(value)
            and not value[value_end].isspace()
            and value[value_end] not in ",;&)]}"
        ):
            value_end += 1
        if value_end == value_start:
            cursor = value_start
            continue

        plain_value = value[value_start:value_end]
        if plain_value.casefold() in {"bearer", "basic"}:
            credential_start = value_end
            while credential_start < len(value) and value[credential_start] in " \t":
                credential_start += 1
            credential_end = credential_start
            while (
                credential_end < len(value)
                and not value[credential_end].isspace()
                and value[credential_end] not in ",;&)]}"
            ):
                credential_end += 1
            if credential_end > credential_start:
                parts.append(
                    plain_value + value[value_end:credential_start] + "***"
                )
                cursor = credential_end
                continue

        trailing = ""
        while value_end > value_start and value[value_end - 1] in ".,!?":
            trailing = value[value_end - 1] + trailing
            value_end -= 1
        parts.append("***" + trailing)
        cursor = value_end

    parts.append(value[cursor:])
    return "".join(parts)


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
    if isinstance(value, Mapping):
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
