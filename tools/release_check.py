from __future__ import annotations

import argparse
import email.policy
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from collections import deque
from dataclasses import dataclass, asdict
from email.parser import BytesParser
from pathlib import Path
from pathlib import PurePosixPath

from packaging.markers import Marker, default_environment
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
INIT_PY = ROOT / "grandgousier" / "__init__.py"
DIST = ROOT / "dist"
BUILD = ROOT / "build"

FORBIDDEN_WHEEL_PREFIXES = (
    "tests/",
    "docs/",
    "context/",
    "internal_tools/",
)
FORBIDDEN_WHEEL_BASENAMES = {
    "CONTEXT.md",
    "README.md",
    ".env",
}
FORBIDDEN_ARCHIVE_DIRECTORIES = {".git", ".opencode"}
REQUIRED_WHEEL_RESOURCES = {
    "grandgousier/resources/configs/master.yaml",
    "grandgousier/resources/configs/robophot.yaml",
    "grandgousier/resources/configs/grandgousier.example.yaml",
    "grandgousier/resources/migrations/migrate_all.sql",
    "grandgousier/resources/acceptance/gpx_tf17_2.json",
}


@dataclass
class Check:
    name: str
    status: str
    detail: str


@dataclass(frozen=True)
class ProjectMetadata:
    name: str
    version: str
    requires_python: SpecifierSet | None
    requires_dist: frozenset[tuple[str, tuple[str, ...], SpecifierSet, str | None, str | None]]
    provides_extra: frozenset[str]


@dataclass(frozen=True)
class DistributionMetadata:
    metadata_version: str
    name: str
    version: str
    requires_python: SpecifierSet | None
    requires_dist: frozenset[tuple[str, tuple[str, ...], SpecifierSet, str | None, str | None]]
    provides_extra: frozenset[str]
    other_headers: tuple[tuple[str, tuple[str, ...]], ...]
    body: str

    def consistency_key(self) -> tuple[object, ...]:
        return (
            self.metadata_version,
            self.name,
            self.version,
            self.requires_python,
            self.requires_dist,
            self.provides_extra,
            self.other_headers,
            self.body,
        )


def run(cmd: list[str], *, cwd: Path = ROOT, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_versions() -> tuple[str, str]:
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    project_version = str(project["project"]["version"])
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', INIT_PY.read_text(encoding="utf-8"), re.M)
    if not m:
        raise RuntimeError("Could not find __version__ in grandgousier/__init__.py")
    return project_version, m.group(1)


def _archive_members(path: Path) -> list[str]:
    if path.suffix == ".whl" or path.suffix == ".zip":
        with zipfile.ZipFile(path) as zf:
            return zf.namelist()
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as tf:
            return tf.getnames()
    raise ValueError(f"Unsupported archive: {path}")


def _has_forbidden_archive_directory(member: str) -> bool:
    return any(
        part.casefold() in FORBIDDEN_ARCHIVE_DIRECTORIES
        for part in member.replace("\\", "/").split("/")
    )


def _canonical_name(value: str) -> str:
    # The validate keyword is not available in every packaging version we support.
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", value):
        raise ValueError(f"invalid distribution name {value!r}")
    return canonicalize_name(value)


def _normalized_specifiers(value: str) -> SpecifierSet:
    return SpecifierSet(value)


def _combine_marker_terms(operator: str, terms: list[tuple[object, ...]]) -> tuple[object, ...]:
    flattened: list[tuple[object, ...]] = []
    for term in terms:
        if len(term) == 2 and term[0] == operator and isinstance(term[1], tuple):
            flattened.extend(term[1])
        else:
            flattened.append(term)
    if len(flattened) == 1:
        return flattened[0]
    return (operator, tuple(sorted(flattened, key=repr)))


def _normalized_marker_node(node: object) -> tuple[object, ...]:
    if isinstance(node, tuple) and len(node) == 3:
        left, operator, right = node
        operator_value = str(operator)
        left_type = type(left).__name__
        right_type = type(right).__name__
        left_value = str(left)
        right_value = str(right)
        if operator_value in {"==", "!="}:
            if left_type == "Variable" and left_value == "extra" and right_type == "Value":
                right_value = _canonical_name(right_value)
            if right_type == "Variable" and right_value == "extra" and left_type == "Value":
                left_value = _canonical_name(left_value)
            operands = tuple(sorted(((left_type, left_value), (right_type, right_value))))
            return ("comparison", operator_value, *operands)
        return ("comparison", left_type, left_value, operator_value, right_type, right_value)

    if not isinstance(node, list) or not node or len(node) % 2 == 0:
        raise ValueError("unsupported packaging marker representation")
    terms = [_normalized_marker_node(node[index]) for index in range(0, len(node), 2)]
    operators = [node[index] for index in range(1, len(node), 2)]
    if any(operator not in {"and", "or"} for operator in operators):
        raise ValueError("unsupported packaging marker operator")

    disjunction: list[tuple[object, ...]] = []
    conjunction: list[tuple[object, ...]] = []
    for index, term in enumerate(terms):
        conjunction.append(term)
        if index == len(operators) or operators[index] == "or":
            disjunction.append(_combine_marker_terms("and", conjunction))
            conjunction = []
    return _combine_marker_terms("or", disjunction)


def _normalized_marker(marker: Marker | None) -> str | None:
    if marker is None:
        return None
    marker_tree = getattr(marker, "_markers", None)
    if marker_tree is None:
        raise ValueError("packaging marker tree is unavailable")
    return repr(_normalized_marker_node(marker_tree))


def _normalized_requirement(
    raw: str, *, extra: str | None = None
) -> tuple[str, tuple[str, ...], SpecifierSet, str | None, str | None]:
    requirement = Requirement(raw)
    marker = requirement.marker
    if extra is not None:
        extra_marker = f"extra == {extra!r}"
        marker = Marker(f"({marker}) and ({extra_marker})") if marker else Marker(extra_marker)
    return (
        _canonical_name(requirement.name),
        tuple(sorted(_canonical_name(item) for item in requirement.extras)),
        requirement.specifier,
        requirement.url,
        _normalized_marker(marker),
    )


def read_project_metadata() -> ProjectMetadata:
    """Read and normalize PEP 621 dependency declarations from pyproject.toml."""

    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    name = _canonical_name(str(project["name"]))
    version = str(Version(str(project["version"])))
    requires_python_value = project.get("requires-python")
    requires_python = (
        _normalized_specifiers(str(requires_python_value))
        if requires_python_value is not None
        else None
    )

    requirements = {
        _normalized_requirement(str(value)) for value in project.get("dependencies", [])
    }
    extras: set[str] = set()
    optional = project.get("optional-dependencies", {})
    if not isinstance(optional, dict):
        raise ValueError("project.optional-dependencies must be a table")
    for group, values in optional.items():
        extra = _canonical_name(str(group))
        if extra in extras:
            raise ValueError(f"duplicate normalized optional dependency group: {extra}")
        extras.add(extra)
        if not isinstance(values, list):
            raise ValueError(f"optional dependency group {group!r} must be an array")
        requirements.update(
            _normalized_requirement(str(value), extra=extra) for value in values
        )

    return ProjectMetadata(
        name=name,
        version=version,
        requires_python=requires_python,
        requires_dist=frozenset(requirements),
        provides_extra=frozenset(extras),
    )


def _parse_distribution_metadata(data: bytes, *, location: str) -> DistributionMetadata:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{location}: metadata is not UTF-8: {exc}") from exc
    message = BytesParser(policy=email.policy.default).parsebytes(text.encode("utf-8"))
    if message.defects:
        defects = ", ".join(type(defect).__name__ for defect in message.defects)
        raise ValueError(f"{location}: malformed metadata headers ({defects})")

    def single_header(name: str, *, required: bool = False) -> str | None:
        values = message.get_all(name, [])
        if len(values) > 1 or (required and len(values) != 1):
            expectation = "exactly one" if required else "at most one"
            raise ValueError(f"{location}: expected {expectation} {name} header")
        value = str(values[0]).strip() if values else None
        if required and not value:
            raise ValueError(f"{location}: empty {name} header")
        return value

    metadata_version = single_header("Metadata-Version", required=True)
    if not re.fullmatch(r"\d+\.\d+", metadata_version or ""):
        raise ValueError(f"{location}: invalid Metadata-Version {metadata_version!r}")
    name_value = single_header("Name", required=True)
    version_value = single_header("Version", required=True)
    try:
        name = _canonical_name(name_value or "")
        version = str(Version(version_value or ""))
    except Exception as exc:
        raise ValueError(f"{location}: invalid Name or Version: {exc}") from exc

    requires_python_value = single_header("Requires-Python")
    try:
        requires_python = (
            _normalized_specifiers(requires_python_value)
            if requires_python_value is not None
            else None
        )
        requires_dist = frozenset(
            _normalized_requirement(str(value))
            for value in message.get_all("Requires-Dist", [])
        )
        extras = [_canonical_name(str(value).strip()) for value in message.get_all("Provides-Extra", [])]
    except Exception as exc:
        raise ValueError(f"{location}: invalid dependency metadata: {exc}") from exc
    if len(extras) != len(set(extras)):
        raise ValueError(f"{location}: duplicate normalized Provides-Extra values")

    excluded_headers = {
        "metadata-version",
        "name",
        "version",
        "requires-python",
        "requires-dist",
        "provides-extra",
    }
    headers: dict[str, list[str]] = {}
    for key, value in message.raw_items():
        normalized_key = key.lower()
        if normalized_key not in excluded_headers:
            headers.setdefault(normalized_key, []).append(" ".join(str(value).split()))
    other_headers = tuple(
        sorted((key, tuple(sorted(values))) for key, values in headers.items())
    )
    body = message.get_payload()
    if not isinstance(body, str):
        raise ValueError(f"{location}: malformed metadata body")
    body = body.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    return DistributionMetadata(
        metadata_version=metadata_version,
        name=name,
        version=version,
        requires_python=requires_python,
        requires_dist=requires_dist,
        provides_extra=frozenset(extras),
        other_headers=other_headers,
        body=body,
    )


def _read_wheel_metadata(
    wheel: Path,
) -> tuple[DistributionMetadata | None, str | None, int]:
    try:
        with zipfile.ZipFile(wheel) as archive:
            members = [
                info
                for info in archive.infolist()
                if info.filename.replace("\\", "/").endswith(".dist-info/METADATA")
                and not info.is_dir()
            ]
            if len(members) != 1:
                return None, f"METADATA files={len(members)}", len(members)
            info = members[0]
            metadata = _parse_distribution_metadata(
                archive.read(info), location=info.filename
            )
            return metadata, None, 1
    except Exception as exc:
        return None, f"could not read wheel metadata: {type(exc).__name__}: {exc}", 0


def _read_sdist_metadata(
    sdist: Path,
) -> list[tuple[str, DistributionMetadata | None, str | None]]:
    entries: list[tuple[str, bytes]] = []
    try:
        if sdist.name.endswith(".tar.gz"):
            with tarfile.open(sdist, "r:gz") as archive:
                for info in archive.getmembers():
                    if info.isfile() and PurePosixPath(info.name.replace("\\", "/")).name == "PKG-INFO":
                        stream = archive.extractfile(info)
                        if stream is None:
                            entries.append((info.name, b""))
                        else:
                            with stream:
                                entries.append((info.name, stream.read()))
        elif sdist.suffix == ".zip":
            with zipfile.ZipFile(sdist) as archive:
                for info in archive.infolist():
                    if not info.is_dir() and PurePosixPath(info.filename.replace("\\", "/")).name == "PKG-INFO":
                        entries.append((info.filename, archive.read(info)))
        else:
            return [(str(sdist), None, f"unsupported sdist archive: {sdist}")]
    except Exception as exc:
        return [(str(sdist), None, f"could not read sdist metadata: {type(exc).__name__}: {exc}")]

    parsed = []
    for name, data in entries:
        try:
            parsed.append((name, _parse_distribution_metadata(data, location=name), None))
        except Exception as exc:
            parsed.append((name, None, str(exc)))
    return parsed


def _comparison_check(
    name: str,
    source: ProjectMetadata | None,
    source_error: str | None,
    artifacts: list[tuple[str, DistributionMetadata | None, str | None]],
    field: str,
    *,
    artifact_label: str = "artifact",
) -> Check:
    if source is None:
        return Check(name, "FAIL", f"source metadata unavailable: {source_error}")
    if not artifacts:
        return Check(
            name,
            "FAIL",
            f"source={len(getattr(source, field))},{artifact_label}=0; metadata missing",
        )
    errors = [f"{path}: {error}" for path, metadata, error in artifacts if metadata is None]
    if errors:
        return Check(name, "FAIL", "; ".join(errors))
    expected = getattr(source, field)
    mismatches = [
        f"{path}={len(getattr(metadata, field))}"
        for path, metadata, _ in artifacts
        if getattr(metadata, field) != expected
    ]
    artifact_counts = [
        len(getattr(metadata, field))
        for _, metadata, _ in artifacts
        if metadata is not None
    ]
    if mismatches:
        return Check(
            name,
            "FAIL",
            f"source={len(expected)},{artifact_label} metadata differs: "
            + ",".join(mismatches),
        )
    artifact_summary = (
        f"{artifact_label}={artifact_counts[0]}"
        if len(artifact_counts) == 1
        else f"{artifact_label} copies={len(artifact_counts)},counts={artifact_counts}"
    )
    return Check(name, "OK", f"source={len(expected)},{artifact_summary}")


def inspect_wheel(wheel: Path, version: str) -> list[Check]:
    checks: list[Check] = []
    members = _archive_members(wheel)
    normalized = {m.replace("\\", "/") for m in members}
    leaked = []
    for member in normalized:
        low = member.casefold()
        if any(low.startswith(prefix.casefold()) for prefix in FORBIDDEN_WHEEL_PREFIXES):
            leaked.append(member)
        if Path(member).name in FORBIDDEN_WHEEL_BASENAMES:
            leaked.append(member)
        if _has_forbidden_archive_directory(member):
            leaked.append(member)
    checks.append(Check("wheel_private_paths", "OK" if not leaked else "FAIL", "none" if not leaked else ",".join(sorted(leaked)[:20])))
    missing = sorted(REQUIRED_WHEEL_RESOURCES - normalized)
    checks.append(Check("wheel_resources", "OK" if not missing else "FAIL", "all required resources present" if not missing else "missing=" + ",".join(missing)))
    metadata, metadata_error, metadata_count = _read_wheel_metadata(wheel)
    if metadata_error:
        checks.append(Check("wheel_metadata", "FAIL", metadata_error))
    else:
        try:
            expected_version = str(Version(version))
        except Exception as exc:
            expected_version = ""
            metadata_error = f"invalid expected version: {exc}"
        problems = []
        if metadata is None:
            problems.append(metadata_error or "metadata unavailable")
        else:
            if metadata.name != _canonical_name("grandgousier"):
                problems.append(f"name={metadata.name}")
            if metadata.version != expected_version:
                problems.append(f"version={metadata.version},expected={expected_version}")
            if metadata.requires_python != SpecifierSet(">=3.11"):
                problems.append(f"Requires-Python={metadata.requires_python}")
        checks.append(
            Check(
                "wheel_metadata",
                "FAIL" if problems else "OK",
                "; ".join(problems) if problems else "version/requires-python OK",
            )
        )

    try:
        source = read_project_metadata()
        source_error = None
    except Exception as exc:
        source = None
        source_error = f"{type(exc).__name__}: {exc}"
    wheel_entry = (
        (str(wheel), metadata, metadata_error)
        if metadata is not None
        else (str(wheel), None, metadata_error or f"METADATA files={metadata_count}")
    )
    for check_name, field in (
        ("wheel_dependencies_match_source", "requires_dist"),
        ("wheel_extras_match_source", "provides_extra"),
    ):
        checks.append(
            _comparison_check(
                check_name,
                source,
                source_error,
                [wheel_entry],
                field,
                artifact_label="wheel",
            )
        )
    return checks



def inspect_sdist(sdist: Path, *, version: str | None = None) -> list[Check]:
    members = _archive_members(sdist)
    normalized = []
    leaked = []
    for member in members:
        clean = member.replace("\\", "/")
        parts = clean.split("/", 1)
        normalized.append(parts[1] if len(parts) == 2 else parts[0])
        if _has_forbidden_archive_directory(clean):
            leaked.append(clean)
    for member in normalized:
        low = member.casefold()
        if low.startswith("internal_tools/") or low.startswith("docs/context/") or low.startswith("context/"):
            leaked.append(member)
        if Path(member).name in {"CONTEXT.md", ".env"}:
            leaked.append(member)
    checks = [
        Check(
            "sdist_private_paths",
            "OK" if not leaked else "FAIL",
            "none" if not leaked else ",".join(sorted(set(leaked))[:20]),
        )
    ]
    required = {"tools/release_check.py", "tools/snapshot_environment.py", "tools/acceptance_check.py", "release/README.md", "requirements-release.txt"}
    missing = sorted(required - set(normalized))
    checks.append(
        Check(
            "sdist_release_tools",
            "OK" if not missing else "FAIL",
            "all release tools present" if not missing else "missing=" + ",".join(missing),
        )
    )
    run_specific_tool = "tools/check_m5_test_source_fields.py"
    tool_included = run_specific_tool in normalized
    checks.append(
        Check(
            "sdist_run_specific_tools",
            "FAIL" if tool_included else "OK",
            "run-specific checker excluded" if not tool_included else run_specific_tool,
        )
    )

    metadata_copies = _read_sdist_metadata(sdist)
    if not metadata_copies:
        checks.append(Check("sdist_metadata_copies_consistent", "FAIL", "PKG-INFO files=0"))
    else:
        invalid = [f"{path}: {error}" for path, metadata, error in metadata_copies if metadata is None]
        if invalid:
            checks.append(
                Check("sdist_metadata_copies_consistent", "FAIL", "; ".join(invalid))
            )
        else:
            reference = metadata_copies[0][1]
            assert reference is not None
            inconsistent = [
                path
                for path, metadata, _ in metadata_copies[1:]
                if metadata is None or metadata.consistency_key() != reference.consistency_key()
            ]
            checks.append(
                Check(
                    "sdist_metadata_copies_consistent",
                    "FAIL" if inconsistent else "OK",
                    "inconsistent=" + ",".join(inconsistent)
                    if inconsistent
                    else f"equivalent PKG-INFO copies={len(metadata_copies)}",
                )
            )

    try:
        source = read_project_metadata()
        source_error = None
    except Exception as exc:
        source = None
        source_error = f"{type(exc).__name__}: {exc}"
    for check_name, field in (
        ("sdist_dependencies_match_source", "requires_dist"),
        ("sdist_extras_match_source", "provides_extra"),
    ):
        checks.append(
            _comparison_check(
                check_name,
                source,
                source_error,
                metadata_copies,
                field,
                artifact_label="sdist",
            )
        )

    expected_version = version or (source.version if source is not None else None)
    if source is None:
        version_check = Check(
            "sdist_metadata_version", "FAIL", f"source metadata unavailable: {source_error}"
        )
    elif not metadata_copies:
        version_check = Check("sdist_metadata_version", "FAIL", "PKG-INFO files=0")
    else:
        try:
            normalized_expected = str(Version(expected_version or ""))
        except Exception as exc:
            normalized_expected = ""
            source_error = f"invalid expected version: {exc}"
        invalid = [f"{path}: {error}" for path, metadata, error in metadata_copies if metadata is None]
        mismatches = [
            f"{path}: name={metadata.name},version={metadata.version}"
            for path, metadata, _ in metadata_copies
            if metadata is not None
            and (metadata.name != source.name or metadata.version != normalized_expected)
        ]
        problems = invalid + mismatches
        if not expected_version:
            problems.append(source_error or "expected version unavailable")
        version_check = Check(
            "sdist_metadata_version",
            "FAIL" if problems else "OK",
            "; ".join(problems)
            if problems
            else f"name={source.name},version={normalized_expected}",
        )
    checks.append(version_check)
    return checks


def compare_wheel_sdist_metadata(wheel: Path, sdist: Path) -> list[Check]:
    wheel_metadata, wheel_error, _ = _read_wheel_metadata(wheel)
    sdist_metadata = _read_sdist_metadata(sdist)
    checks = []
    for check_name, field in (
        ("wheel_sdist_dependencies_match", "requires_dist"),
        ("wheel_sdist_extras_match", "provides_extra"),
    ):
        if wheel_metadata is None:
            checks.append(
                Check(check_name, "FAIL", f"wheel metadata unavailable: {wheel_error}")
            )
            continue
        if not sdist_metadata:
            checks.append(Check(check_name, "FAIL", "sdist PKG-INFO files=0"))
            continue
        invalid = [
            f"{path}: {error}"
            for path, metadata, error in sdist_metadata
            if metadata is None
        ]
        if invalid:
            checks.append(Check(check_name, "FAIL", "; ".join(invalid)))
            continue
        mismatches = [
            f"{path}={len(getattr(metadata, field))}"
            for path, metadata, _ in sdist_metadata
            if getattr(metadata, field) != getattr(wheel_metadata, field)
        ]
        if mismatches:
            checks.append(
                Check(
                    check_name,
                    "FAIL",
                    f"wheel={len(getattr(wheel_metadata, field))},sdist differs: "
                    + ",".join(mismatches),
                )
            )
        else:
            checks.append(
                Check(
                    check_name,
                    "OK",
                    f"wheel={len(getattr(wheel_metadata, field))},"
                    f"sdist copies={len(sdist_metadata)}",
                )
            )
    return checks

def install_smoke(wheel: Path, version: str) -> Check:
    """Install/import the wheel from a truly isolated temporary target.

    The checker may itself run inside a venv that already contains an older
    Grandgousier. Force extraction into the target and use ``-I -S`` for the
    child interpreter so neither user/site packages nor PYTHONPATH can shadow
    the wheel under test.
    """

    with tempfile.TemporaryDirectory(prefix="gg_release_install_") as td:
        target = Path(td) / "site"
        target.mkdir()
        proc = run([
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--ignore-installed",
            "--target",
            str(target),
            str(wheel),
        ])
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()[-1600:]
            return Check("wheel_install_smoke", "FAIL", detail)

        code = "\n".join([
            "import sys",
            "from pathlib import Path",
            f"target = Path({str(target)!r}).resolve()",
            "sys.path.insert(0, str(target))",
            "import grandgousier",
            "from grandgousier.resources import instrument_profile_path",
            "origin = Path(grandgousier.__file__).resolve()",
            "resource = instrument_profile_path('master.yaml').resolve()",
            "actual_version = str(grandgousier.__version__)",
            f"expected_version = {version!r}",
            "def under_target(path): return path == target or target in path.parents",
            "problems = []",
            "problems += [f'import_origin_outside_target={origin}'] if not under_target(origin) else []",
            "problems += [f'version_expected={expected_version},actual={actual_version}'] if actual_version != expected_version else []",
            "problems += [f'missing_resource={resource}'] if not resource.is_file() else []",
            "problems += [f'resource_outside_target={resource}'] if not under_target(resource) else []",
            "if problems: raise SystemExit('; '.join(problems))",
            "print(f'version={actual_version}; origin={origin}; resource={resource}')",
        ])
        smoke = run([sys.executable, "-I", "-S", "-c", code], cwd=Path(td))
        if smoke.returncode != 0:
            detail = (smoke.stderr or smoke.stdout).strip()[-1600:]
            return Check("wheel_install_smoke", "FAIL", detail)
        return Check("wheel_install_smoke", "OK", smoke.stdout.strip())

def clean_build_staging() -> list[Path]:
    """Remove local setuptools staging that can leak stale package files into a release.

    Overlay ZIPs preserve source mtimes.  A previously built ``build/lib`` may
    therefore look newer than freshly overlaid sources, causing setuptools to
    reuse stale Python modules while still emitting new wheel metadata.
    Release builds must start from source, never from old staging.
    """

    removed: list[Path] = []
    candidates = [BUILD, *sorted(ROOT.glob("*.egg-info"))]
    for path in candidates:
        if not path.exists():
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        removed.append(path)
    return removed


def _valid_build_backend(value: object) -> bool:
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    module, separator, target = value.partition(":")

    def dotted_identifier(candidate: str) -> bool:
        return bool(candidate) and all(part.isidentifier() for part in candidate.split("."))

    return dotted_identifier(module) and (not separator or dotted_identifier(target))


def _installed_distribution_version(name: str) -> Version:
    return Version(importlib.metadata.version(name))


def _display_build_requirement(requirement: Requirement) -> str:
    name = canonicalize_name(requirement.name)
    extras = (
        f"[{','.join(sorted(canonicalize_name(extra) for extra in requirement.extras))}]"
        if requirement.extras
        else ""
    )
    if requirement.url is not None:
        return f"{name}{extras} @ <direct reference>"
    return str(requirement)


def check_build_backend_requirements() -> Check:
    """Verify configured PEP 517 requirements in this interpreter, without installing anything."""

    check_name = "BUILD_BACKEND_REQUIREMENTS_SATISFIED"
    backend = "<unavailable>"
    installed: list[str] = []
    declared: list[str] = []
    errors: list[str] = []
    try:
        project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    except Exception:
        return Check(
            check_name,
            "FAIL",
            f"{check_name}=NO; backend={backend}; declared_requires=<unavailable>; "
            "installed=<none>; errors=pyproject could not be parsed",
        )

    build_system = project.get("build-system")
    if not isinstance(build_system, dict):
        errors.append("[build-system] is missing or is not a table")
    else:
        backend_value = build_system.get("build-backend")
        if _valid_build_backend(backend_value):
            backend = str(backend_value)
        else:
            backend = repr(backend_value) if backend_value is not None else "<missing>"
            errors.append("invalid or missing build-backend declaration")

        raw_requirements = build_system.get("requires")
        if not isinstance(raw_requirements, list):
            declared.append("<unavailable>")
            errors.append("build-system.requires is missing or is not an array")
        else:
            parsed: list[tuple[int, Requirement]] = []
            for index, raw in enumerate(raw_requirements):
                if not isinstance(raw, str):
                    declared.append(f"requires[{index}]=<non-string>")
                    errors.append(f"requires[{index}] is not a string")
                    continue
                try:
                    requirement = Requirement(raw)
                except Exception:
                    declared.append(f"requires[{index}]=<malformed requirement>")
                    errors.append(f"requires[{index}] is malformed")
                    continue
                declared.append(
                    f"requires[{index}]={_display_build_requirement(requirement)}"
                )
                parsed.append((index, requirement))

            for index, requirement in parsed:
                try:
                    applicable = (
                        requirement.marker.evaluate() if requirement.marker is not None else True
                    )
                except Exception:
                    errors.append(f"requires[{index}] marker is unverifiable")
                    continue
                name = canonicalize_name(requirement.name)
                if not applicable:
                    installed.append(f"{name}=<skipped: marker false>")
                    continue
                if requirement.url is not None:
                    installed.append(f"{name}=<unverifiable direct reference>")
                    errors.append(
                        f"requires[{index}] uses an unsupported direct reference"
                    )
                    continue
                if requirement.extras:
                    installed.append(f"{name}=<unverifiable extras>")
                    errors.append(
                        f"requires[{index}] requests extras whose installation cannot be verified"
                    )
                    continue
                try:
                    version = _installed_distribution_version(requirement.name)
                except importlib.metadata.PackageNotFoundError:
                    installed.append(f"{name}=<missing>")
                    errors.append(f"requires[{index}] distribution {name} is not installed")
                    continue
                except Exception:
                    installed.append(f"{name}=<unverifiable>")
                    errors.append(f"requires[{index}] installed version for {name} is unverifiable")
                    continue

                installed.append(f"{name}={version}")
                if not requirement.specifier.contains(version):
                    errors.append(
                        f"requires[{index}] {requirement} is incompatible with installed {version}"
                    )

    detail = (
        f"{check_name}={'NO' if errors else 'YES'}; backend={backend}; "
        f"declared_requires={json.dumps(declared, separators=(',', ':'))}; "
        f"installed={','.join(installed) if installed else '<none>'}"
    )
    if errors:
        detail += "; errors=" + "; ".join(errors)
    return Check(check_name, "FAIL" if errors else "OK", detail)


def build_release(*, clean: bool = True) -> tuple[list[Check], list[Path]]:
    requirements_check = check_build_backend_requirements()
    checks = [requirements_check]
    if requirements_check.status != "OK":
        return checks, []

    if clean:
        if DIST.exists():
            shutil.rmtree(DIST)
        clean_build_staging()
    DIST.mkdir(exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "build",
        "--sdist",
        "--wheel",
        "--no-isolation",
        "--outdir",
        str(DIST),
    ]
    proc = run(command)
    if proc.returncode != 0:
        checks.append(Check("build", "FAIL", (proc.stderr or proc.stdout).strip()[-2000:]))
        return checks, []
    artifacts = sorted([*DIST.glob("*.whl"), *DIST.glob("*.tar.gz")])
    checks.append(Check("build", "OK" if len(artifacts) == 2 else "FAIL", ",".join(p.name for p in artifacts)))
    return checks, artifacts


def write_checksums(artifacts: list[Path]) -> Path:
    out = DIST / "SHA256SUMS.txt"
    lines = [f"{sha256_file(path)}  {path.name}" for path in artifacts]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def check_constraints(path: Path | None) -> Check:
    if path is None:
        return Check("validated_constraints", "WARN", "not supplied")
    if not path.is_file():
        return Check("validated_constraints", "FAIL", f"not found: {path}")
    versions = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "==" not in line:
            continue
        name, version = line.split("==", 1)
        versions[re.sub(r"[-_.]+", "-", name.strip().lower())] = version.strip()
    required = {
        "astropy", "numpy", "pandas", "sqlalchemy", "psycopg", "pydantic", "pydantic-settings",
        "pyyaml", "tqdm", "matplotlib", "certifi", "requests",
    }
    missing = sorted(required - set(versions))
    return Check("validated_constraints", "OK" if not missing else "FAIL", f"packages={len(versions)}" if not missing else "missing=" + ",".join(missing))



SNAPSHOT_CLOSURE_FORMAT = "grandgousier_dependency_closure_v1"


def _snapshot_requirement_record(record: object) -> tuple[str, tuple[str, ...], Marker | None]:
    if not isinstance(record, dict):
        raise ValueError("requirement record is not an object")
    name = record.get("name")
    extras = record.get("extras")
    specifier = record.get("specifier")
    marker_value = record.get("marker")
    if not isinstance(name, str) or _canonical_name(name) != name:
        raise ValueError("requirement name is invalid or not canonical")
    if (
        not isinstance(extras, list)
        or any(not isinstance(extra, str) for extra in extras)
        or extras != sorted(set(extras))
        or any(_canonical_name(extra) != extra for extra in extras)
    ):
        raise ValueError("requirement extras are invalid or not canonical")
    if not isinstance(specifier, str) or not isinstance(record.get("url_present"), bool):
        raise ValueError("requirement specifier/reference fields are invalid")
    SpecifierSet(specifier)
    if marker_value is None:
        marker = None
    elif isinstance(marker_value, str):
        marker = Marker(marker_value)
    else:
        raise ValueError("requirement marker is invalid")
    return name, tuple(extras), marker


def _snapshot_marker_applies(marker: Marker | None, *, extra: str) -> bool:
    if marker is None:
        return True
    environment = default_environment()
    environment["extra"] = extra
    return bool(marker.evaluate(environment))


def _snapshot_marker_applies_for_extras(
    marker: Marker | None, extras: tuple[str, ...]
) -> bool:
    contexts = extras or ("",)
    return any(
        _snapshot_marker_applies(marker, extra=extra)
        for extra in contexts
    )


def _snapshot_root_requirements(
    project: dict, selected_extras: list[str]
) -> list[tuple[Requirement, str]]:
    project_metadata = project["project"]
    optional = project_metadata.get("optional-dependencies", {})
    if not isinstance(optional, dict):
        raise ValueError("project.optional-dependencies is not a table")
    groups: dict[str, list[str]] = {}
    for group, values in optional.items():
        if not isinstance(group, str) or not isinstance(values, list):
            raise ValueError("optional dependency group is malformed")
        canonical = _canonical_name(group)
        if canonical in groups:
            raise ValueError("duplicate normalized optional dependency group")
        if any(not isinstance(value, str) for value in values):
            raise ValueError(f"optional dependency group {canonical} is malformed")
        groups[canonical] = values

    selected: list[str] = []
    for extra in selected_extras:
        canonical = _canonical_name(extra)
        if canonical not in groups:
            raise ValueError(f"unknown selected optional dependency group {canonical}")
        selected.append(canonical)
    if len(selected) != len(set(selected)):
        raise ValueError("selected optional dependency groups contain duplicates")

    requirements = []
    core = project_metadata.get("dependencies", [])
    if not isinstance(core, list) or any(not isinstance(value, str) for value in core):
        raise ValueError("project.dependencies is malformed")
    requirements.extend((Requirement(value), "") for value in core)
    for canonical in sorted(selected):
        values = groups[canonical]
        requirements.extend((Requirement(value), canonical) for value in values)
    return requirements


def check_snapshot(path: Path | None, *, version: str) -> Check:
    if path is None:
        return Check("validated_snapshot", "WARN", "not supplied")
    if not path.is_file():
        return Check("validated_snapshot", "FAIL", f"not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        return Check("validated_snapshot", "FAIL", f"invalid JSON: {exc}")
    if not isinstance(payload, dict):
        return Check("validated_snapshot", "FAIL", "snapshot root is not an object")
    problems = []
    if payload.get("format") != "grandgousier_environment_snapshot_v2":
        problems.append("format")
    if str(payload.get("grandgousier_version")) != version:
        problems.append(f"version={payload.get('grandgousier_version')}")
    if payload.get("pyproject_sha256") != sha256_file(PYPROJECT):
        problems.append("pyproject_sha256")
    missing = payload.get("missing_required_packages")
    if not isinstance(missing, list) or any(not isinstance(name, str) for name in missing):
        problems.append("missing_required_packages_invalid")
    elif missing:
        problems.append("missing_required_packages=" + ",".join(sorted(set(missing))))

    closure = payload.get("dependency_closure")
    if not isinstance(closure, dict):
        problems.append("dependency_closure_missing")
    else:
        if closure.get("format") != SNAPSHOT_CLOSURE_FORMAT:
            problems.append("dependency_closure_format")
        if closure.get("complete") is not True:
            problems.append("dependency_closure_incomplete")
        unresolved = closure.get("unresolved_requirements")
        if not isinstance(unresolved, list) or any(not isinstance(item, str) for item in unresolved):
            problems.append("unresolved_requirements_invalid")
        elif unresolved:
            problems.append("unresolved_requirements=" + ",".join(sorted(set(unresolved))))

    packages_value = payload.get("packages")
    package_rows: dict[str, dict] = {}
    package_requirements: dict[str, list[tuple[str, tuple[str, ...], Marker | None]]] = {}
    if not isinstance(packages_value, list):
        problems.append("packages_invalid")
    else:
        for index, row in enumerate(packages_value):
            if not isinstance(row, dict):
                problems.append(f"package_record_invalid={index}")
                continue
            name = row.get("canonical_name")
            display_name = row.get("name")
            try:
                canonical = _canonical_name(name) if isinstance(name, str) else ""
            except ValueError:
                canonical = ""
            if not canonical or canonical != name:
                problems.append(f"package_name_invalid={index}")
                continue
            if canonical in package_rows:
                problems.append(f"package_duplicate={canonical}")
                continue
            try:
                if not isinstance(display_name, str) or _canonical_name(display_name) != canonical:
                    raise ValueError("display name does not match canonical name")
                raw_requirements = row.get("requires")
                if not isinstance(raw_requirements, list):
                    raise ValueError("requires is missing or not a list")
                package_requirements[canonical] = [
                    _snapshot_requirement_record(item) for item in raw_requirements
                ]
            except Exception:
                problems.append(f"package_requirements_invalid={canonical}")
                package_requirements[canonical] = []
            package_rows[canonical] = row

    package_count = payload.get("package_count")
    if (
        not isinstance(package_count, int)
        or isinstance(package_count, bool)
        or not isinstance(packages_value, list)
        or package_count != len(packages_value)
    ):
        problems.append("package_count_mismatch")

    selected_extras = payload.get("selected_extras")
    if not isinstance(selected_extras, list) or any(
        not isinstance(extra, str) for extra in selected_extras
    ):
        problems.append("selected_extras_invalid")
        selected_extras = []

    recorded_states: set[tuple[str, tuple[str, ...]]] = set()
    if isinstance(closure, dict):
        raw_states = closure.get("states")
        if not isinstance(raw_states, list):
            problems.append("closure_states_invalid")
        else:
            for index, state in enumerate(raw_states):
                if not isinstance(state, dict):
                    problems.append(f"closure_state_invalid={index}")
                    continue
                name = state.get("canonical_name")
                extras = state.get("extras")
                try:
                    if not isinstance(name, str) or _canonical_name(name) != name:
                        raise ValueError
                    if (
                        not isinstance(extras, list)
                        or any(not isinstance(extra, str) for extra in extras)
                        or extras != sorted(set(extras))
                        or any(_canonical_name(extra) != extra for extra in extras)
                    ):
                        raise ValueError
                except ValueError:
                    problems.append(f"closure_state_invalid={index}")
                    continue
                state_key = (name, tuple(extras))
                if state_key in recorded_states:
                    problems.append(f"closure_state_duplicate={name}")
                recorded_states.add(state_key)

    try:
        project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
        root_requirements = _snapshot_root_requirements(project, selected_extras)
        expected_states: set[tuple[str, tuple[str, ...]]] = set()
        queue: deque[tuple[str, tuple[str, ...]]] = deque()
        for requirement, active_extra in root_requirements:
            if requirement.marker is None or _snapshot_marker_applies(
                requirement.marker, extra=active_extra
            ):
                queue.append(
                    (
                        canonicalize_name(requirement.name),
                        tuple(sorted(canonicalize_name(extra) for extra in requirement.extras)),
                    )
                )
        while queue:
            name, extras = queue.popleft()
            state = (name, extras)
            if state in expected_states:
                continue
            expected_states.add(state)
            if name not in package_rows:
                continue
            for dependency_name, dependency_extras, marker in package_requirements.get(name, []):
                if _snapshot_marker_applies_for_extras(marker, extras):
                    queue.append((dependency_name, dependency_extras))

        missing_packages = sorted({name for name, _ in expected_states} - set(package_rows))
        extra_packages = sorted(set(package_rows) - {name for name, _ in expected_states})
        if missing_packages:
            problems.append("closure_packages_missing=" + ",".join(missing_packages))
        if extra_packages:
            problems.append("closure_packages_unexpected=" + ",".join(extra_packages))
        missing_states = sorted(expected_states - recorded_states)
        unexpected_states = sorted(recorded_states - expected_states)
        if missing_states:
            problems.append(
                "closure_states_missing="
                + ",".join(name + (f"[{','.join(extras)}]" if extras else "") for name, extras in missing_states)
            )
        if unexpected_states:
            problems.append(
                "closure_states_unexpected="
                + ",".join(name + (f"[{','.join(extras)}]" if extras else "") for name, extras in unexpected_states)
            )
    except Exception as exc:
        problems.append(f"closure_unverifiable={type(exc).__name__}")

    if problems:
        return Check("validated_snapshot", "FAIL", "mismatch=" + ";".join(problems))
    python_info = payload.get("python")
    platform_info = payload.get("platform")
    py = python_info.get("version", "unknown") if isinstance(python_info, dict) else "unknown"
    platform_name = (
        platform_info.get("system", "unknown")
        if isinstance(platform_info, dict)
        else "unknown"
    )
    return Check(
        "validated_snapshot",
        "OK",
        f"python={py}, platform={platform_name}, packages={package_count}",
    )

def validate_artifacts(artifacts: list[Path], version: str) -> list[Check]:
    """Run the shared wheel/sdist validation gates without changing artifacts."""

    checks: list[Check] = []
    wheels = [path for path in artifacts if path.suffix == ".whl"]
    sdists = [path for path in artifacts if path.name.endswith(".tar.gz")]
    if len(wheels) == 1:
        checks.extend(inspect_wheel(wheels[0], version))
        checks.append(install_smoke(wheels[0], version))
    else:
        checks.append(Check("wheel", "FAIL", f"wheel artifacts={len(wheels)}"))
        checks.extend(
            Check(name, "FAIL", "wheel artifact unavailable")
            for name in (
                "wheel_metadata",
                "wheel_dependencies_match_source",
                "wheel_extras_match_source",
            )
        )
    if len(sdists) == 1:
        checks.extend(inspect_sdist(sdists[0], version=version))
    else:
        checks.append(Check("sdist", "FAIL", f"sdist artifacts={len(sdists)}"))
        checks.extend(
            Check(name, "FAIL", "sdist artifact unavailable")
            for name in (
                "sdist_dependencies_match_source",
                "sdist_extras_match_source",
                "sdist_metadata_copies_consistent",
                "sdist_metadata_version",
            )
        )
    if len(wheels) == 1 and len(sdists) == 1:
        checks.extend(compare_wheel_sdist_metadata(wheels[0], sdists[0]))
    else:
        checks.extend(
            Check(name, "FAIL", "wheel or sdist artifact unavailable")
            for name in (
                "wheel_sdist_dependencies_match",
                "wheel_sdist_extras_match",
            )
        )
    return checks


def _rooted_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Build and validate Grandgousier wheel/sdist release artifacts.")
    p.add_argument("--constraints", help="Validated constraints snapshot to require/check")
    p.add_argument("--snapshot", help="environment_snapshot.json paired with the validated constraints")
    p.add_argument("--report", default="dist/release_check.json")
    p.add_argument(
        "--verify-existing",
        action="store_true",
        help="Validate exact existing wheel and sdist paths without building or modifying them",
    )
    p.add_argument("--wheel", help="Exact wheel path required with --verify-existing")
    p.add_argument("--sdist", help="Exact sdist path required with --verify-existing")
    args = p.parse_args(argv)

    if args.verify_existing:
        if not args.wheel or not args.sdist:
            p.error("--verify-existing requires both --wheel and --sdist")
        wheel_path = _rooted_path(args.wheel)
        sdist_path = _rooted_path(args.sdist)
        report_path = _rooted_path(args.report)
        same_report = report_path.resolve() in {wheel_path.resolve(), sdist_path.resolve()}
        if report_path.exists():
            same_report = same_report or any(
                artifact.exists() and os.path.samefile(report_path, artifact)
                for artifact in (wheel_path, sdist_path)
            )
        if same_report:
            p.error("--report must not point to either artifact")
        if report_path.exists() or report_path.is_symlink():
            p.error("--verify-existing refuses to overwrite an existing --report path")
    elif args.wheel or args.sdist:
        p.error("--wheel and --sdist are only accepted with --verify-existing")

    checks: list[Check] = []
    try:
        pyproject_version, init_version = read_versions()
    except Exception as exc:
        checks.append(Check("version_consistency", "FAIL", f"{type(exc).__name__}: {exc}"))
        pyproject_version = init_version = "UNKNOWN"
    else:
        checks.append(Check("version_consistency", "OK" if pyproject_version == init_version else "FAIL", f"pyproject={pyproject_version}, init={init_version}"))

    checks.append(check_constraints(Path(args.constraints) if args.constraints else None))
    checks.append(check_snapshot(Path(args.snapshot) if args.snapshot else None, version=pyproject_version))
    pipfile = ROOT / "Pipfile.lock"
    if pipfile.is_file():
        try:
            lock = json.loads(pipfile.read_text(encoding="utf-8"))
            lock_py = str(lock.get("_meta", {}).get("requires", {}).get("python_version") or "")
        except Exception as exc:
            checks.append(Check("pipfile_lock", "OK", f"legacy/non-authoritative and unreadable: {exc}"))
        else:
            checks.append(Check("pipfile_lock", "OK", f"legacy/non-authoritative; python_version={lock_py or 'unknown'}"))

    artifacts: list[Path] = []
    if args.verify_existing:
        exact_artifacts = [wheel_path, sdist_path]
        missing = [str(path) for path in exact_artifacts if not path.is_file()]
        if missing:
            checks.append(
                Check("existing_artifacts", "FAIL", "not found=" + ",".join(missing))
            )
        else:
            checks.append(
                Check(
                    "existing_artifacts",
                    "OK",
                    f"wheel={wheel_path.name},sdist={sdist_path.name}",
                )
            )
            artifacts = exact_artifacts
            checks.extend(validate_artifacts(artifacts, pyproject_version))
    else:
        build_checks, artifacts = build_release()
        checks.extend(build_checks)
        if artifacts:
            checks.extend(validate_artifacts(artifacts, pyproject_version))
            sums = write_checksums(artifacts)
            checks.append(Check("checksums", "OK", str(sums.relative_to(ROOT))))

    overall = "FAILED" if any(c.status == "FAIL" for c in checks) else ("READY_WITH_WARNINGS" if any(c.status == "WARN" for c in checks) else "READY")
    report = {
        "format": "grandgousier_release_check_v1",
        "mode": "verify-existing" if args.verify_existing else "build-and-check",
        "status": overall,
        "version": pyproject_version,
        "python": sys.version,
        "checks": [asdict(c) for c in checks],
        "artifacts": [
            {
                "path": str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p),
                "sha256": sha256_file(p),
                "size": p.stat().st_size,
            }
            for p in artifacts
        ],
    }
    report_path = _rooted_path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(f"Grandgousier release check: {overall}")
    for c in checks:
        print(f"{c.name:28} {c.status:5} {c.detail}")
    print(f"Report: {report_path.relative_to(ROOT)}")
    return 1 if overall == "FAILED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
