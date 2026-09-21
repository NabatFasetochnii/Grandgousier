"""Compatibility placeholder for repository-local remote archive tooling.

The public Grandgousier distribution intentionally does not ship site-specific
remote archive transports, credentials, private endpoints, or storage-layout
parsers. Internal deployments may provide repository-local tooling outside the
``grandgousier`` package.
"""

from __future__ import annotations


class RemoteArchiveTransportUnavailable(RuntimeError):
    """Raised when private archive transport functionality is requested publicly."""


def unavailable(*_args, **_kwargs):
    """Fail explicitly rather than guessing a private deployment's transport contract."""
    raise RemoteArchiveTransportUnavailable(
        "Site-specific remote archive transport is not part of the public Grandgousier package."
    )


__all__ = ["RemoteArchiveTransportUnavailable", "unavailable"]
