from __future__ import annotations

import importlib.util
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


release_check = _load("gg_release_check", ROOT / "tools" / "release_check.py")
snapshot = _load("gg_snapshot_environment", ROOT / "tools" / "snapshot_environment.py")


def test_release_version_consistency_matches_project():
    pyproject_version, init_version = release_check.read_versions()
    assert pyproject_version == "0.7.0.10"
    assert init_version == pyproject_version


def test_constraints_lines_are_stable_and_exact():
    lines = snapshot.constraints_lines([
        {"name": "NumPy", "canonical_name": "numpy", "version": "2.1.3"},
        {"name": "Astropy", "canonical_name": "astropy", "version": "7.1.0"},
    ])
    assert lines[-2:] == ["Astropy==7.1.0", "NumPy==2.1.3"]


def test_check_constraints_requires_core_dependency_names(tmp_path):
    path = tmp_path / "constraints.txt"
    names = [
        "astropy", "numpy", "pandas", "sqlalchemy", "psycopg", "pydantic",
        "pydantic-settings", "PyYAML", "tqdm", "matplotlib", "certifi", "requests",
    ]
    path.write_text("\n".join(f"{name}==1.0" for name in names), encoding="utf-8")
    result = release_check.check_constraints(path)
    assert result.status == "OK"


def test_check_snapshot_binds_version_and_pyproject(tmp_path):
    version, _ = release_check.read_versions()
    path = tmp_path / "environment_snapshot.json"
    path.write_text(json.dumps({
        "format": "grandgousier_environment_snapshot_v1",
        "grandgousier_version": version,
        "pyproject_sha256": release_check.sha256_file(release_check.PYPROJECT),
        "missing_required_packages": [],
        "package_count": 12,
        "python": {"version": "3.11.0"},
        "platform": {"system": "Windows"},
    }), encoding="utf-8")
    result = release_check.check_snapshot(path, version=version)
    assert result.status == "OK"
    assert "Windows" in result.detail


def test_wheel_inspection_rejects_private_paths_and_accepts_resources(tmp_path):
    wheel = tmp_path / "grandgousier-0.5.58.1-py3-none-any.whl"
    metadata = "Metadata-Version: 2.4\nName: grandgousier\nVersion: 0.5.58.1\nRequires-Python: >=3.11\n"
    with zipfile.ZipFile(wheel, "w") as zf:
        for resource in release_check.REQUIRED_WHEEL_RESOURCES:
            zf.writestr(resource, "x")
        zf.writestr("grandgousier-0.5.58.1.dist-info/METADATA", metadata)
        zf.writestr("internal_tools/private.py", "secret")
    checks = {row.name: row for row in release_check.inspect_wheel(wheel, "0.5.58.1")}
    assert checks["wheel_private_paths"].status == "FAIL"
    assert checks["wheel_resources"].status == "OK"
    assert checks["wheel_metadata"].status == "OK"


def test_sdist_inspection_requires_release_tools_and_rejects_internal(tmp_path):
    import tarfile
    import io

    sdist = tmp_path / "grandgousier-0.5.58.1.tar.gz"
    files = {
        "grandgousier-0.5.58.1/tools/release_check.py": b"x",
        "grandgousier-0.5.58.1/tools/snapshot_environment.py": b"x",
        "grandgousier-0.5.58.1/tools/acceptance_check.py": b"x",
        "grandgousier-0.5.58.1/release/README.md": b"x",
        "grandgousier-0.5.58.1/requirements-release.txt": b"x",
        "grandgousier-0.5.58.1/internal_tools/private.py": b"secret",
    }
    with tarfile.open(sdist, "w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    checks = {row.name: row for row in release_check.inspect_sdist(sdist)}
    assert checks["sdist_private_paths"].status == "FAIL"
    assert checks["sdist_release_tools"].status == "OK"


def test_install_smoke_forces_target_isolation(monkeypatch, tmp_path):
    import subprocess

    calls = []

    def fake_run(cmd, *, cwd=release_check.ROOT, env=None):
        calls.append((list(cmd), Path(cwd)))
        if "pip" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="installed", stderr="")
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="version=0.5.58.1; origin=X; resource=Y\n",
            stderr="",
        )

    monkeypatch.setattr(release_check, "run", fake_run)
    result = release_check.install_smoke(tmp_path / "grandgousier-0.5.58.1-py3-none-any.whl", "0.5.58.1")

    assert result.status == "OK"
    pip_cmd = calls[0][0]
    smoke_cmd = calls[1][0]
    assert "--ignore-installed" in pip_cmd
    assert smoke_cmd[1:3] == ["-I", "-S"]
    compile(smoke_cmd[-1], "<wheel-install-smoke>", "exec")
    assert "import_origin_outside_target" in smoke_cmd[-1]
    assert "resource_outside_target" in smoke_cmd[-1]


def test_clean_build_staging_removes_stale_build_and_egg_info(monkeypatch, tmp_path):
    build = tmp_path / "build"
    egg = tmp_path / "grandgousier.egg-info"
    (build / "lib" / "grandgousier").mkdir(parents=True)
    (build / "lib" / "grandgousier" / "__init__.py").write_text(
        '__version__ = "0.5.58.1"\n', encoding="utf-8"
    )
    egg.mkdir()
    (egg / "PKG-INFO").write_text("Version: 0.5.58.1\n", encoding="utf-8")

    monkeypatch.setattr(release_check, "ROOT", tmp_path)
    monkeypatch.setattr(release_check, "BUILD", build)

    removed = release_check.clean_build_staging()

    assert build in removed
    assert egg in removed
    assert not build.exists()
    assert not egg.exists()


def test_build_release_cleans_stale_staging_before_backend(monkeypatch, tmp_path):
    import subprocess

    dist = tmp_path / "dist"
    build = tmp_path / "build"
    egg = tmp_path / "grandgousier.egg-info"
    build.mkdir()
    egg.mkdir()
    seen = {}

    def fake_run(cmd, *, cwd=release_check.ROOT, env=None):
        seen["build_exists"] = build.exists()
        seen["egg_exists"] = egg.exists()
        # build_release expects artifacts to exist after the backend call.
        dist.mkdir(exist_ok=True)
        (dist / "grandgousier-0.6.0.tar.gz").write_bytes(b"sdist")
        (dist / "grandgousier-0.6.0-py3-none-any.whl").write_bytes(b"wheel")
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(release_check, "ROOT", tmp_path)
    monkeypatch.setattr(release_check, "DIST", dist)
    monkeypatch.setattr(release_check, "BUILD", build)
    monkeypatch.setattr(release_check, "run", fake_run)

    checks, artifacts = release_check.build_release(clean=True)

    assert seen == {"build_exists": False, "egg_exists": False}
    assert checks[0].status == "OK"
    assert len(artifacts) == 2
