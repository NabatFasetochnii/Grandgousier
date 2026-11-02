from __future__ import annotations

import argparse
import csv
import hashlib
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
from dataclasses import dataclass, asdict
from pathlib import Path

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
    checks.append(Check("wheel_private_paths", "OK" if not leaked else "FAIL", "none" if not leaked else ",".join(sorted(leaked)[:20])))
    missing = sorted(REQUIRED_WHEEL_RESOURCES - normalized)
    checks.append(Check("wheel_resources", "OK" if not missing else "FAIL", "all required resources present" if not missing else "missing=" + ",".join(missing)))
    dist_info = [m for m in normalized if m.endswith(".dist-info/METADATA")]
    if len(dist_info) != 1:
        checks.append(Check("wheel_metadata", "FAIL", f"METADATA files={len(dist_info)}"))
    else:
        with zipfile.ZipFile(wheel) as zf:
            meta = zf.read(dist_info[0]).decode("utf-8", errors="replace")
        ok = f"Version: {version}" in meta and "Requires-Python: >=3.11" in meta
        checks.append(Check("wheel_metadata", "OK" if ok else "FAIL", "version/requires-python OK" if ok else "version or Requires-Python mismatch"))
    return checks



def inspect_sdist(sdist: Path) -> list[Check]:
    members = _archive_members(sdist)
    normalized = []
    for member in members:
        clean = member.replace("\\", "/")
        parts = clean.split("/", 1)
        normalized.append(parts[1] if len(parts) == 2 else parts[0])
    leaked = []
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


def build_release(*, clean: bool = True) -> tuple[list[Check], list[Path]]:
    checks: list[Check] = []
    if clean:
        if DIST.exists():
            shutil.rmtree(DIST)
        clean_build_staging()
    DIST.mkdir(exist_ok=True)
    code = (
        "from setuptools import build_meta; "
        f"print(build_meta.build_sdist({str(DIST)!r})); "
        f"print(build_meta.build_wheel({str(DIST)!r}))"
    )
    proc = run([sys.executable, "-c", code])
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



def check_snapshot(path: Path | None, *, version: str) -> Check:
    if path is None:
        return Check("validated_snapshot", "WARN", "not supplied")
    if not path.is_file():
        return Check("validated_snapshot", "FAIL", f"not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        return Check("validated_snapshot", "FAIL", f"invalid JSON: {exc}")
    problems = []
    if payload.get("format") != "grandgousier_environment_snapshot_v1":
        problems.append("format")
    if str(payload.get("grandgousier_version")) != version:
        problems.append(f"version={payload.get('grandgousier_version')}")
    if payload.get("pyproject_sha256") != sha256_file(PYPROJECT):
        problems.append("pyproject_sha256")
    if payload.get("missing_required_packages"):
        problems.append("missing_required_packages")
    if problems:
        return Check("validated_snapshot", "FAIL", "mismatch=" + ",".join(problems))
    py = payload.get("python", {}).get("version", "unknown")
    platform_name = payload.get("platform", {}).get("system", "unknown")
    return Check("validated_snapshot", "OK", f"python={py}, platform={platform_name}, packages={payload.get('package_count')}")

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Build and validate Grandgousier wheel/sdist release artifacts.")
    p.add_argument("--constraints", help="Validated constraints snapshot to require/check")
    p.add_argument("--snapshot", help="environment_snapshot.json paired with the validated constraints")
    p.add_argument("--report", default="dist/release_check.json")
    args = p.parse_args(argv)

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

    build_checks, artifacts = build_release()
    checks.extend(build_checks)
    if artifacts:
        wheels = [p for p in artifacts if p.suffix == ".whl"]
        sdists = [p for p in artifacts if p.name.endswith(".tar.gz")]
        if len(wheels) == 1:
            checks.extend(inspect_wheel(wheels[0], pyproject_version))
            checks.append(install_smoke(wheels[0], pyproject_version))
        else:
            checks.append(Check("wheel", "FAIL", f"wheel artifacts={len(wheels)}"))
        if len(sdists) == 1:
            checks.extend(inspect_sdist(sdists[0]))
        else:
            checks.append(Check("sdist", "FAIL", f"sdist artifacts={len(sdists)}"))
        sums = write_checksums(artifacts)
        checks.append(Check("checksums", "OK", str(sums.relative_to(ROOT))))

    overall = "FAILED" if any(c.status == "FAIL" for c in checks) else ("READY_WITH_WARNINGS" if any(c.status == "WARN" for c in checks) else "READY")
    report = {
        "format": "grandgousier_release_check_v1",
        "status": overall,
        "version": pyproject_version,
        "python": sys.version,
        "checks": [asdict(c) for c in checks],
        "artifacts": [
            {"path": str(p.relative_to(ROOT)), "sha256": sha256_file(p), "size": p.stat().st_size}
            for p in artifacts
        ],
    }
    report_path = ROOT / args.report
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(f"Grandgousier release check: {overall}")
    for c in checks:
        print(f"{c.name:28} {c.status:5} {c.detail}")
    print(f"Report: {report_path.relative_to(ROOT)}")
    return 1 if overall == "FAILED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
