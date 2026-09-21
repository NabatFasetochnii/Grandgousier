"""Installed package resources used by Grandgousier runtime services.

The canonical runtime copies of instrument profiles and SQL migration files live
inside the Python package so wheel/sdist installations do not depend on a source
checkout. Repository-root ``configs/`` and ``scripts/`` remain compatibility
copies for developer/manual workflows and are checked for byte parity by tests.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

_RESOURCE_PACKAGE = "grandgousier.resources"


def resource_path(*parts: str) -> Path:
    """Return a filesystem path for an installed Grandgousier package resource.

    Wheels are installed unpacked by pip, so the standard ``importlib.resources``
    traversable resolves to a normal filesystem path. An explicit error is raised
    for exotic importers that do not expose package resources as filesystem paths;
    current Grandgousier runtime consumers require filesystem paths for Astropy,
    PyYAML and SQL migration tooling.
    """

    traversable = files(_RESOURCE_PACKAGE).joinpath(*parts)
    try:
        return Path(traversable)
    except TypeError as exc:  # pragma: no cover - normal wheel installs are filesystem based.
        raise RuntimeError(
            "Grandgousier package resources are not available as filesystem paths. "
            "Install the wheel/sdist with a standard Python installer instead of using zipimport."
        ) from exc


def instrument_profile_path(filename: str) -> Path:
    """Resolve a bundled instrument-profile YAML file."""

    return resource_path("configs", filename)


def migration_resource_path(filename: str) -> Path:
    """Resolve a bundled SQL migration resource."""

    return resource_path("migrations", filename)


def application_config_example_path() -> Path:
    """Resolve the bundled self-service application-config template."""

    return resource_path("configs", "grandgousier.example.yaml")


def acceptance_dataset_path(filename: str) -> Path:
    """Resolve a bundled frozen scientific acceptance specification."""

    return resource_path("acceptance", filename)
