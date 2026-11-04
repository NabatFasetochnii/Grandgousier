from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import platform
import sys
import tomllib
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

try:
    from packaging.markers import default_environment
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
except ImportError as exc:  # pragma: no cover - exercised by CLI/process invocation
    raise SystemExit(
        "packaging is required for environment snapshot; install release tooling first: "
        "python -m pip install -r requirements-release.txt"
    ) from exc

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
DEFAULT_EXTRAS: tuple[str, ...] = ()
DEPENDENCY_CLOSURE_FORMAT = "grandgousier_dependency_closure_v1"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_project_metadata(path: Path = PYPROJECT) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _selected_extras(project: dict, extras: Iterable[str]) -> tuple[str, ...]:
    optional = project["project"].get("optional-dependencies", {})
    groups: dict[str, str] = {}
    for name in optional:
        canonical = canonicalize_name(name)
        if canonical in groups:
            raise ValueError(f"Duplicate normalized optional dependency group: {canonical}")
        groups[canonical] = name
    selected = []
    for extra in extras:
        canonical = canonicalize_name(extra)
        if canonical not in groups:
            raise ValueError(f"Unknown optional dependency group: {extra}")
        selected.append(groups[canonical])
    return tuple(sorted(set(selected), key=canonicalize_name))


def _root_requirements(project: dict, extras: Iterable[str]) -> list[tuple[Requirement, str]]:
    p = project["project"]
    requirements = [(Requirement(item), "") for item in p.get("dependencies", [])]
    optional = p.get("optional-dependencies", {})
    for extra in extras:
        if extra not in optional:
            raise ValueError(f"Unknown optional dependency group: {extra}")
        active_extra = canonicalize_name(extra)
        requirements.extend(
            (Requirement(item), active_extra) for item in optional[extra]
        )
    return requirements


def _marker_applies(req: Requirement, *, extra: str = "") -> bool:
    if req.marker is None:
        return True
    env = default_environment()
    env["extra"] = extra
    return bool(req.marker.evaluate(env))


def _marker_applies_for_extras(req: Requirement, extras: tuple[str, ...]) -> bool:
    contexts = extras or ("",)
    return any(_marker_applies(req, extra=extra) for extra in contexts)


def _requirement_record(req: Requirement) -> dict[str, object]:
    """Return a safe, deterministic representation of a dependency edge."""

    return {
        "name": canonicalize_name(req.name),
        "extras": sorted(canonicalize_name(extra) for extra in req.extras),
        "specifier": str(req.specifier),
        "marker": str(req.marker) if req.marker is not None else None,
        # Do not copy direct-reference URLs into a public snapshot: they can contain secrets.
        "url_present": req.url is not None,
    }


def _installed_distributions() -> dict[str, metadata.Distribution]:
    out: dict[str, metadata.Distribution] = {}
    for dist in metadata.distributions():
        name = dist.metadata.get("Name")
        if not name:
            continue
        out[canonicalize_name(name)] = dist
    return out


@dataclass(frozen=True)
class DependencyClosure:
    packages: list[dict]
    missing: list[str]
    states: list[dict[str, object]]
    unresolved_requirements: list[str]

    @property
    def complete(self) -> bool:
        return not self.missing and not self.unresolved_requirements


def dependency_closure(project: dict, extras: Iterable[str]) -> DependencyClosure:
    installed = _installed_distributions()
    roots = _root_requirements(project, extras)
    queue: deque[tuple[str, tuple[str, ...]]] = deque()
    missing: set[str] = set()
    unresolved: set[str] = set()
    seen: set[tuple[str, tuple[str, ...]]] = set()
    requirements_by_name: dict[str, list[dict[str, object]]] = {}

    for req, active_extra in roots:
        if _marker_applies(req, extra=active_extra):
            queue.append(
                (
                    canonicalize_name(req.name),
                    tuple(sorted(canonicalize_name(extra) for extra in req.extras)),
                )
            )

    while queue:
        name, requested_extras = queue.popleft()
        state = (name, requested_extras)
        if state in seen:
            continue
        seen.add(state)
        dist = installed.get(name)
        if dist is None:
            missing.add(name)
            continue
        records = requirements_by_name.setdefault(name, [])
        parsed_requirements = []
        for index, raw in enumerate(dist.requires or []):
            try:
                req = Requirement(raw)
            except Exception:
                unresolved.add(f"{name}:Requires-Dist[{index}]")
                continue
            parsed_requirements.append(req)
            record = _requirement_record(req)
            if record not in records:
                records.append(record)

        for req in parsed_requirements:
            if _marker_applies_for_extras(req, requested_extras):
                queue.append(
                    (
                        canonicalize_name(req.name),
                        tuple(sorted(canonicalize_name(extra) for extra in req.extras)),
                    )
                )

    rows: list[dict] = []
    for name in sorted({name for name, _ in seen}):
        dist = installed.get(name)
        if dist is None:
            continue
        direct_url = None
        try:
            direct_url = dist.read_text("direct_url.json")
        except Exception:
            pass
        rows.append(
            {
                "name": dist.metadata.get("Name") or name,
                "canonical_name": name,
                "version": dist.version,
                "direct_url": json.loads(direct_url) if direct_url else None,
                "requires": sorted(
                    requirements_by_name.get(name, []),
                    key=lambda item: (
                        str(item["name"]),
                        tuple(item["extras"]),
                        str(item["specifier"]),
                        str(item["marker"]),
                        bool(item["url_present"]),
                    ),
                ),
            }
        )
    states = [
        {"canonical_name": name, "extras": list(requested_extras)}
        for name, requested_extras in sorted(seen)
    ]
    return DependencyClosure(
        packages=rows,
        missing=sorted(missing),
        states=states,
        unresolved_requirements=sorted(unresolved),
    )


def constraints_lines(packages: Iterable[dict]) -> list[str]:
    lines = [
        "# Grandgousier validated-environment constraints snapshot.",
        "# Generated by tools/snapshot_environment.py; do not edit package versions manually.",
    ]
    for row in sorted(packages, key=lambda item: item["canonical_name"]):
        lines.append(f"{row['name']}=={row['version']}")
    return lines


def write_snapshot(out_dir: Path, *, extras: tuple[str, ...]) -> tuple[Path, Path]:
    project = load_project_metadata()
    extras = _selected_extras(project, extras)
    closure = dependency_closure(project, extras)
    project_meta = project["project"]
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "environment_snapshot.json"
    constraints_path = out_dir / "constraints.txt"
    payload = {
        "format": "grandgousier_environment_snapshot_v2",
        "grandgousier_version": project_meta["version"],
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "executable": sys.executable,
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "platform": platform.platform(),
        },
        "selected_extras": list(extras),
        "pyproject_sha256": sha256_file(PYPROJECT),
        "package_count": len(closure.packages),
        "missing_required_packages": closure.missing,
        "dependency_closure": {
            "format": DEPENDENCY_CLOSURE_FORMAT,
            "complete": closure.complete,
            "states": closure.states,
            "unresolved_requirements": closure.unresolved_requirements,
        },
        "packages": closure.packages,
    }
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    constraints_path.write_text(
        "\n".join(constraints_lines(closure.packages)) + "\n", encoding="utf-8"
    )
    return json_path, constraints_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Snapshot the installed Grandgousier dependency closure.")
    p.add_argument("--out-dir", default="release/environment", help="Output directory")
    p.add_argument(
        "--extra",
        action="append",
        dest="extras",
        help="Optional-dependency group to include; repeatable. Defaults to core dependencies only.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    extras = tuple(args.extras) if args.extras else DEFAULT_EXTRAS
    try:
        json_path, constraints_path = write_snapshot(Path(args.out_dir), extras=extras)
    except ValueError as exc:
        print(f"Environment snapshot failed: {exc}", file=sys.stderr)
        return 2
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    print(f"Environment snapshot: {json_path}")
    print(f"Constraints: {constraints_path}")
    print(f"Packages: {payload['package_count']}")
    closure = payload["dependency_closure"]
    if payload["missing_required_packages"]:
        print("Missing: " + ",".join(payload["missing_required_packages"]), file=sys.stderr)
    if closure["unresolved_requirements"]:
        print(
            "Unresolved requirements: " + ",".join(closure["unresolved_requirements"]),
            file=sys.stderr,
        )
    if not closure["complete"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
