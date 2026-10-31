from __future__ import annotations

import logging
import traceback

from grandgousier.logging_utils import sanitize_diagnostic_text


_UNFORMATTABLE_PACKAGE_LOG_MESSAGE = "[package log message unavailable]"
_UNFORMATTABLE_PACKAGE_EXCEPTION = "[package exception details unavailable]"


class _PackageDiagnosticSanitizingFilter(logging.Filter):
    """Redact recognized credentials from package-owned formatted log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "grandgousier" and not record.name.startswith("grandgousier."):
            return True
        try:
            record.msg = sanitize_diagnostic_text(record.getMessage())
            record.args = ()
        except Exception:
            record.msg = _UNFORMATTABLE_PACKAGE_LOG_MESSAGE
            record.args = ()
        if record.exc_info:
            try:
                exception_text = "".join(traceback.format_exception(*record.exc_info)).rstrip()
                record.exc_text = sanitize_diagnostic_text(exception_text)
            except Exception:
                record.exc_text = _UNFORMATTABLE_PACKAGE_EXCEPTION
        if record.stack_info:
            record.stack_info = sanitize_diagnostic_text(record.stack_info)
        return True


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    root_logger = logging.getLogger()
    for handler in root_logger.handlers:
        if not any(
            isinstance(existing, _PackageDiagnosticSanitizingFilter)
            for existing in handler.filters
        ):
            handler.addFilter(_PackageDiagnosticSanitizingFilter())
