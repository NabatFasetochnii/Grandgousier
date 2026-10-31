from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

from grandgousier.logging_utils import PipelineFileLogger, sanitize_database_url


def test_sanitize_database_url_redacts_repeated_encoded_and_unknown_query_values():
    url = (
        "postgresql+psycopg://safe_user:SYNTHETIC_AUTH_SECRET@db.example.test:5432/safe_db"
        "?sslmode=verify-full&connect_timeout=15"
        "&password=SYNTHETIC_PASSWORD_ONE&password=SYNTHETIC_PASSWORD_TWO"
        "&%70assword=SYNTHETIC_ENCODED_PASSWORD&api_key=SYNTHETIC_API_KEY"
        "&vendor_option=SYNTHETIC_UNKNOWN_VALUE&sslmode=SYNTHETIC_INVALID_SSLMODE"
        "#SYNTHETIC_FRAGMENT_SECRET"
    )

    sanitized = sanitize_database_url(url)

    assert sanitized is not None
    assert sanitized.startswith(
        "postgresql+psycopg://safe_user:***@db.example.test:5432/safe_db?"
    )
    parts = urlsplit(sanitized)
    assert parts.fragment == ""
    assert parse_qsl(parts.query, keep_blank_values=True) == [
        ("sslmode", "verify-full"),
        ("connect_timeout", "15"),
        ("password", "***"),
        ("password", "***"),
        ("password", "***"),
        ("api_key", "***"),
        ("vendor_option", "***"),
        ("sslmode", "***"),
    ]
    for sentinel in (
        "SYNTHETIC_AUTH_SECRET",
        "SYNTHETIC_PASSWORD_ONE",
        "SYNTHETIC_PASSWORD_TWO",
        "SYNTHETIC_ENCODED_PASSWORD",
        "SYNTHETIC_API_KEY",
        "SYNTHETIC_UNKNOWN_VALUE",
        "SYNTHETIC_INVALID_SSLMODE",
        "SYNTHETIC_FRAGMENT_SECRET",
    ):
        assert sentinel not in sanitized


@pytest.mark.parametrize(
    "malformed_url",
    [
        "postgresql+psycopg://safe_user:SYNTHETIC_SECRET@[broken/safe_db?password=SYNTHETIC_QUERY",
        "postgresql+psycopg://safe_user:SYNTHETIC_SECRET@db.example.test:99999/safe_db",
        "postgresql+psycopg://safe_user:SYNTHETIC_SECRET@db.example.test/safe_db?password=%FF",
        "postgresql+psycopg://safe_user:SYNTHETIC_SECRET@db.example.test/safe_db?password=%ZZ",
    ],
)
def test_sanitize_database_url_fails_closed_for_malformed_input(malformed_url: str):
    assert sanitize_database_url(malformed_url) == "***"


def test_sanitize_database_url_keeps_empty_input_semantics():
    assert sanitize_database_url(None) is None
    assert sanitize_database_url("") == ""


def test_sanitize_database_url_redacts_userinfo_without_password_separator():
    url = "https://SYNTHETIC_USERINFO_TOKEN@api.example.test/v1/resource"

    sanitized = sanitize_database_url(url)

    assert sanitized == "https://***@api.example.test/v1/resource"
    assert "SYNTHETIC_USERINFO_TOKEN" not in sanitized


def test_exception_logs_sanitize_traceback_exception_and_event_message(tmp_path: Path):
    database_url = (
        "postgresql+psycopg://safe_user:SYNTHETIC_EXCEPTION_AUTH@db.example.test/safe_db"
        "?password=SYNTHETIC_EXCEPTION_QUERY&sslmode=require"
        "&private_option=SYNTHETIC_EXCEPTION_UNKNOWN#SYNTHETIC_EXCEPTION_FRAGMENT"
    )
    exception_labels = (
        "password=SYNTHETIC_EXCEPTION_PASSWORD, keep-after-password"
        "; passwd: SYNTHETIC_EXCEPTION_PASSWD; pwd=SYNTHETIC_EXCEPTION_PWD"
        "; token: SYNTHETIC_EXCEPTION_TOKEN"
        "; access_token=SYNTHETIC_EXCEPTION_ACCESS_TOKEN"
        "; api_key=SYNTHETIC_EXCEPTION_API_KEY"
        "; API-Key: SYNTHETIC_EXCEPTION_API_KEY_DASHED"
        "; secret=SYNTHETIC_EXCEPTION_SECRET"
        "; client_secret='SYNTHETIC_EXCEPTION_CLIENT_SECRET'"
        '; "client_secret": "SYNTHETIC_EXCEPTION_JSON_CLIENT_SECRET"'
        "; credential=SYNTHETIC_EXCEPTION_CREDENTIAL"
        "; authorization: Bearer SYNTHETIC_EXCEPTION_BEARER"
    )
    exception_text = f"Connection failed for {database_url}; {exception_labels}"
    event_message = (
        f"Retrying database request for {database_url}; "
        "pwd=SYNTHETIC_EVENT_PWD; access_token: SYNTHETIC_EVENT_ACCESS_TOKEN; "
        "API-Key=SYNTHETIC_EVENT_API_KEY; credential: SYNTHETIC_EVENT_CREDENTIAL; "
        "password matters in this step; keep-event-text; "
        "see https://docs.example.test/diagnostics?topic=logging"
    )
    logger = PipelineFileLogger.create(
        root_dir=tmp_path,
        pipeline_run_id=7,
        run_uid="synthetic-exception-test",
        min_level="INFO",
    )

    try:
        raise RuntimeError(exception_text)
    except RuntimeError as exc:
        logger.exception(
            exc=exc,
            event_type="synthetic_failure",
            message=event_message,
            pipeline_run_id=7,
            step_name="synthetic-step",
            context={"safe_diagnostic": "preserved-safe-context", "attempt": 2},
        )

    error_text = logger.error_log_file.read_text(encoding="utf-8")
    run_text = logger.log_file.read_text(encoding="utf-8")
    jsonl_records = [
        json.loads(line)
        for line in logger.jsonl_log_file.read_text(encoding="utf-8").splitlines()
    ]
    all_logged_text = error_text + run_text + json.dumps(jsonl_records)

    for sentinel in (
        "SYNTHETIC_EXCEPTION_AUTH",
        "SYNTHETIC_EXCEPTION_QUERY",
        "SYNTHETIC_EXCEPTION_UNKNOWN",
        "SYNTHETIC_EXCEPTION_FRAGMENT",
        "SYNTHETIC_EXCEPTION_PASSWORD",
        "SYNTHETIC_EXCEPTION_PASSWD",
        "SYNTHETIC_EXCEPTION_PWD",
        "SYNTHETIC_EXCEPTION_TOKEN",
        "SYNTHETIC_EXCEPTION_ACCESS_TOKEN",
        "SYNTHETIC_EXCEPTION_API_KEY",
        "SYNTHETIC_EXCEPTION_API_KEY_DASHED",
        "SYNTHETIC_EXCEPTION_SECRET",
        "SYNTHETIC_EXCEPTION_CLIENT_SECRET",
        "SYNTHETIC_EXCEPTION_JSON_CLIENT_SECRET",
        "SYNTHETIC_EXCEPTION_CREDENTIAL",
        "SYNTHETIC_EXCEPTION_BEARER",
        "SYNTHETIC_EVENT_PWD",
        "SYNTHETIC_EVENT_ACCESS_TOKEN",
        "SYNTHETIC_EVENT_API_KEY",
        "SYNTHETIC_EVENT_CREDENTIAL",
    ):
        assert sentinel not in all_logged_text

    assert "RuntimeError" in error_text
    assert "Connection failed for postgresql+psycopg://safe_user:***@db.example.test/safe_db" in error_text
    assert "password=***, keep-after-password" in error_text
    assert "client_secret='***'" in error_text
    assert '"client_secret": "***"' in error_text
    assert "authorization: Bearer ***" in error_text
    assert "preserved-safe-context" in run_text
    assert (
        "Retrying database request for postgresql+psycopg://safe_user:***@db.example.test/safe_db"
        in run_text
    )
    assert "pwd=***; access_token: ***; API-Key=***; credential: ***" in run_text
    assert "password matters in this step; keep-event-text" in run_text
    assert "https://docs.example.test/diagnostics?topic=logging" in run_text
    assert len(jsonl_records) == 1
    record = jsonl_records[0]
    assert record["event_type"] == "synthetic_failure"
    assert "Retrying database request" in record["message"]
    assert "pwd=***; access_token: ***; API-Key=***; credential: ***" in record["message"]
    assert "https://docs.example.test/diagnostics?topic=logging" in record["message"]
    assert record["context"]["error_type"] == "RuntimeError"
    assert "Connection failed for postgresql+psycopg://safe_user:***@db.example.test/safe_db" in record[
        "context"]["error_message"]
    assert record["context"]["safe_diagnostic"] == "preserved-safe-context"
    assert record["context"]["attempt"] == 2


def test_regular_log_messages_redact_database_urls_and_labeled_credentials(
    tmp_path: Path,
):
    database_url = (
        "postgresql+psycopg://safe_user:SYNTHETIC_LOG_AUTH@db.example.test/safe_db"
        "?password=SYNTHETIC_LOG_QUERY#SYNTHETIC_LOG_FRAGMENT"
    )
    message = (
        f"request to {database_url}; pwd=SYNTHETIC_LOG_PWD; "
        "access_token=SYNTHETIC_LOG_TOKEN; keep-this-message"
    )
    logger = PipelineFileLogger.create(
        root_dir=tmp_path,
        pipeline_run_id=8,
        run_uid="synthetic-regular-log-test",
        min_level="INFO",
    )

    logger.log(
        level="WARNING",
        event_type="synthetic_warning",
        message=message,
        pipeline_run_id=8,
    )

    run_text = logger.log_file.read_text(encoding="utf-8")
    jsonl_record = json.loads(logger.jsonl_log_file.read_text(encoding="utf-8"))
    all_logged_text = run_text + json.dumps(jsonl_record)

    for sentinel in (
        "SYNTHETIC_LOG_AUTH",
        "SYNTHETIC_LOG_QUERY",
        "SYNTHETIC_LOG_FRAGMENT",
        "SYNTHETIC_LOG_PWD",
        "SYNTHETIC_LOG_TOKEN",
    ):
        assert sentinel not in all_logged_text

    assert (
        "request to postgresql+psycopg://safe_user:***@db.example.test/safe_db"
        in run_text
    )
    assert "pwd=***; access_token=***; keep-this-message" in run_text
    assert jsonl_record["message"] == (
        "request to postgresql+psycopg://safe_user:***@db.example.test/safe_db"
        "?password=***; pwd=***; access_token=***; keep-this-message"
    )


def test_log_and_exception_recursively_sanitize_context_without_losing_safe_values(
    tmp_path: Path,
):
    database_url = (
        "postgresql+psycopg://safe_user:SYNTHETIC_CONTEXT_URL_PASSWORD@db.example.test/safe_db"
        "?password=SYNTHETIC_CONTEXT_URL_QUERY&sslmode=require"
    )
    context = {
        "password": "SYNTHETIC_CONTEXT_PASSWORD_KEY",
        "safe_count": 3,
        "nested": [
            {
                "Api-Key": "SYNTHETIC_CONTEXT_API_KEY",
                "message": (
                    f"database request {database_url}; "
                    "token=SYNTHETIC_CONTEXT_TOKEN_IN_TEXT; preserve-this-value; "
                    "https://docs.example.test/diagnostics?topic=logging"
                ),
                "service_url": (
                    "https://safe_user:SYNTHETIC_CONTEXT_HTTP_PASSWORD@api.example.test/v1"
                    "?access_token=SYNTHETIC_CONTEXT_HTTP_TOKEN&scope=read"
                ),
                "inner": {"refresh_token": "SYNTHETIC_CONTEXT_REFRESH_TOKEN", "enabled": True},
            }
        ],
    }
    logger = PipelineFileLogger.create(
        root_dir=tmp_path,
        pipeline_run_id=9,
        run_uid="synthetic-context-test",
    )

    logger.log(
        level="INFO",
        event_type="synthetic_context",
        message="context was supplied",
        pipeline_run_id=9,
        context=context,
    )
    try:
        raise RuntimeError("SYNTHETIC_EXCEPTION_CONTEXT_URL postgresql://u:SYNTHETIC_EXCEPTION_CONTEXT_PASSWORD@db.example.test/db")
    except RuntimeError as exc:
        logger.exception(
            exc=exc,
            event_type="synthetic_context_exception",
            message="exception context was supplied",
            pipeline_run_id=9,
            context=context,
        )

    run_text = logger.log_file.read_text(encoding="utf-8")
    error_text = logger.error_log_file.read_text(encoding="utf-8")
    records = [
        json.loads(line)
        for line in logger.jsonl_log_file.read_text(encoding="utf-8").splitlines()
    ]
    complete_output = run_text + error_text + json.dumps(records, ensure_ascii=False)
    for sentinel in (
        "SYNTHETIC_CONTEXT_URL_PASSWORD",
        "SYNTHETIC_CONTEXT_URL_QUERY",
        "SYNTHETIC_CONTEXT_PASSWORD_KEY",
        "SYNTHETIC_CONTEXT_API_KEY",
        "SYNTHETIC_CONTEXT_TOKEN_IN_TEXT",
        "SYNTHETIC_CONTEXT_HTTP_PASSWORD",
        "SYNTHETIC_CONTEXT_HTTP_TOKEN",
        "SYNTHETIC_CONTEXT_REFRESH_TOKEN",
        "SYNTHETIC_EXCEPTION_CONTEXT_PASSWORD",
    ):
        assert sentinel not in complete_output

    assert len(records) == 2
    safe_context = records[0]["context"]
    assert safe_context == {
        "password": "***",
        "safe_count": 3,
        "nested": [
            {
                "Api-Key": "***",
                "message": (
                    "database request postgresql+psycopg://safe_user:***@db.example.test/safe_db"
                    "?password=***&sslmode=require; token=***; preserve-this-value; "
                    "https://docs.example.test/diagnostics?topic=logging"
                ),
                "service_url": (
                    "https://safe_user:***@api.example.test/v1"
                    "?access_token=***&scope=%2A%2A%2A"
                ),
                "inner": {"refresh_token": "***", "enabled": True},
            }
        ],
    }
    assert records[1]["context"] == {
        **safe_context,
        "error_type": "RuntimeError",
        "error_message": (
            "SYNTHETIC_EXCEPTION_CONTEXT_URL "
            "postgresql://u:***@db.example.test/db"
        ),
    }
    assert "preserve-this-value" in run_text
