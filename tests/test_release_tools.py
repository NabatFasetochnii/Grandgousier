from __future__ import annotations

import importlib.util
import io
import json
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

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
    assert pyproject_version == "0.7.0.11"
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
        "grandgousier-0.5.58.1/tools/check_m5_test_source_fields.py": b"run-specific",
    }
    with tarfile.open(sdist, "w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    checks = {row.name: row for row in release_check.inspect_sdist(sdist)}
    assert checks["sdist_private_paths"].status == "FAIL"
    assert checks["sdist_release_tools"].status == "OK"
    assert checks["sdist_run_specific_tools"].status == "FAIL"


def test_manifest_and_sdist_check_exclude_run_specific_checker(tmp_path):
    import io
    import tarfile

    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8").splitlines()
    assert "exclude tools/check_m5_test_source_fields.py" in manifest

    sdist = tmp_path / "grandgousier-0.7.0.11.tar.gz"
    required = [
        "tools/release_check.py",
        "tools/snapshot_environment.py",
        "tools/acceptance_check.py",
        "release/README.md",
        "requirements-release.txt",
    ]
    with tarfile.open(sdist, "w:gz") as archive:
        for member in required:
            payload = b"x"
            info = tarfile.TarInfo(f"grandgousier-0.7.0.11/{member}")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    checks = {
        row.name: row
        for row in release_check.inspect_sdist(sdist)
    }
    assert checks["sdist_run_specific_tools"].status == "OK"


def test_canonical_docs_do_not_require_omitted_private_context_files():
    canonical_docs = [
        ROOT / "AGENTS.md",
        ROOT / "README.md",
        ROOT / "README_PUBLIC.md",
        ROOT / "docs" / "README.md",
        ROOT / "docs" / "CURRENT_ARCHITECTURE.md",
        ROOT / "docs" / "ROADMAP.md",
    ]
    for path in canonical_docs:
        content = path.read_text(encoding="utf-8")
        assert "CONTEXT.md" not in content
        assert "context/project_state.yaml" not in content


def test_release_install_docs_use_github_release_wheel():
    wheel_url = (
        "https://github.com/NabatFasetochnii/Grandgousier/releases/download/v0.7.0.11/"
        "grandgousier-0.7.0.11-py3-none-any.whl"
    )
    assert wheel_url in (ROOT / "README_PUBLIC.md").read_text(encoding="utf-8")
    assert wheel_url in (ROOT / "release" / "README.md").read_text(encoding="utf-8")
    assert "python -m pip install grandgousier\n" not in (
        ROOT / "README_PUBLIC.md"
    ).read_text(encoding="utf-8")


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


def _write_project(
    path: Path,
    *,
    dependencies: tuple[str, ...] = ("alpha>=1",),
    optional: dict[str, tuple[str, ...]] | None = None,
    version: str = "1.2.3",
) -> None:
    dependency_lines = ",\n".join(json.dumps(item) for item in dependencies)
    optional_lines = []
    for group, values in (optional or {}).items():
        rendered = ",\n".join(json.dumps(item) for item in values)
        optional_lines.append(f"{json.dumps(group)} = [{rendered}]")
    path.write_text(
        "[project]\n"
        'name = "grandgousier"\n'
        f'version = "{version}"\n'
        'requires-python = ">=3.11"\n'
        f"dependencies = [{dependency_lines}]\n\n"
        "[project.optional-dependencies]\n"
        + "\n".join(optional_lines)
        + "\n",
        encoding="utf-8",
    )


def _metadata_text(
    *,
    requires: tuple[str, ...] = ("alpha>=1",),
    extras: tuple[str, ...] = (),
    version: str = "1.2.3",
    name: str = "grandgousier",
    requires_python: str = ">=3.11",
    metadata_version: str = "2.4",
) -> str:
    lines = [
        f"Metadata-Version: {metadata_version}",
        f"Name: {name}",
        f"Version: {version}",
        f"Requires-Python: {requires_python}",
    ]
    lines.extend(f"Requires-Dist: {value}" for value in requires)
    lines.extend(f"Provides-Extra: {value}" for value in extras)
    return "\n".join(lines) + "\n\n"


def _metadata_for_project(path: Path) -> str:
    project = tomllib.loads(path.read_text(encoding="utf-8"))["project"]
    lines = [
        "Metadata-Version: 2.4",
        f"Name: {project['name']}",
        f"Version: {project['version']}",
        f"Requires-Python: {project['requires-python']}",
    ]
    lines.extend(f"Requires-Dist: {Requirement(raw)}" for raw in project["dependencies"])
    for group, values in project.get("optional-dependencies", {}).items():
        normalized_group = canonicalize_name(group)
        for raw in values:
            requirement = Requirement(raw)
            base = str(requirement).split(";", 1)[0].strip()
            marker = (
                Marker(f"({requirement.marker}) and extra == {normalized_group!r}")
                if requirement.marker
                else Marker(f"extra == {normalized_group!r}")
            )
            lines.append(f"Requires-Dist: {base}; {marker}")
        lines.append(f"Provides-Extra: {group}")
    return "\n".join(lines) + "\n\n"


def _write_wheel(path: Path, metadata: str) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for resource in release_check.REQUIRED_WHEEL_RESOURCES:
            archive.writestr(resource, "x")
        archive.writestr("grandgousier-1.2.3.dist-info/METADATA", metadata)
    return path


def _write_sdist(
    path: Path,
    metadata_copies: tuple[bytes, ...],
    *,
    root: str = "grandgousier-1.2.3",
    extra_members: tuple[str, ...] = (),
) -> Path:
    with tarfile.open(path, "w:gz") as archive:
        for member in extra_members:
            info = tarfile.TarInfo(member)
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))
        for index, payload in enumerate(metadata_copies):
            member = f"{root}/PKG-INFO"
            if index:
                member = f"{root}/copy-{index}/PKG-INFO"
            info = tarfile.TarInfo(member)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return path


def _checks(rows):
    return {row.name: row for row in rows}


def test_metadata_gate_reports_source_26_wheel_26_sdist_zero(monkeypatch, tmp_path):
    source = release_check.read_project_metadata()
    assert len(source.requires_dist) == 26
    assert len(source.provides_extra) == 5
    assert source.version == "0.7.0.11"

    wheel = _write_wheel(
        tmp_path / "grandgousier-0.7.0.11-py3-none-any.whl",
        _metadata_for_project(release_check.PYPROJECT),
    )
    wheel_checks = _checks(release_check.inspect_wheel(wheel, source.version))
    assert wheel_checks["wheel_dependencies_match_source"].status == "OK"
    assert wheel_checks["wheel_extras_match_source"].status == "OK"
    assert wheel_checks["wheel_dependencies_match_source"].detail == "source=26,wheel=26"

    zero_dependency_metadata = _metadata_text(
        requires=(),
        version=source.version,
        extras=tuple(sorted(source.provides_extra)),
    ).encode()
    sdist = _write_sdist(
        tmp_path / "grandgousier-0.7.0.11.tar.gz",
        (zero_dependency_metadata, zero_dependency_metadata),
        root=f"grandgousier-{source.version}",
    )
    sdist_checks = _checks(release_check.inspect_sdist(sdist, version=source.version))
    assert sdist_checks["sdist_metadata_copies_consistent"].status == "OK"
    assert sdist_checks["sdist_dependencies_match_source"].status == "FAIL"
    assert sdist_checks["sdist_dependencies_match_source"].detail == (
        "source=26,sdist metadata differs: "
        "grandgousier-0.7.0.11/PKG-INFO=0,"
        "grandgousier-0.7.0.11/copy-1/PKG-INFO=0"
    )
    assert sdist_checks["sdist_extras_match_source"].status == "OK"
    assert sdist_checks["sdist_metadata_version"].status == "OK"


def test_wheel_private_path_gate_rejects_git_and_opencode_members(tmp_path):
    wheel = _write_wheel(tmp_path / "test.whl", _metadata_text())
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr(".git/config", "x")
        archive.writestr("grandgousier/.opencode/settings.json", "x")

    checks = _checks(release_check.inspect_wheel(wheel, "1.2.3"))
    assert checks["wheel_private_paths"].status == "FAIL"
    assert ".git/config" in checks["wheel_private_paths"].detail
    assert "grandgousier/.opencode/settings.json" in checks["wheel_private_paths"].detail


def test_sdist_private_path_gate_rejects_git_and_opencode_members(tmp_path):
    sdist = _write_sdist(
        tmp_path / "test.tar.gz",
        (),
        extra_members=(
            "grandgousier-1.2.3/.git/config",
            "grandgousier-1.2.3/package/.opencode/settings.json",
        ),
    )

    checks = _checks(release_check.inspect_sdist(sdist))
    assert checks["sdist_private_paths"].status == "FAIL"
    assert "grandgousier-1.2.3/.git/config" in checks["sdist_private_paths"].detail
    assert "grandgousier-1.2.3/package/.opencode/settings.json" in (
        checks["sdist_private_paths"].detail
    )


def test_wheel_metadata_normalizes_pep508_names_specifiers_markers_and_extras(
    monkeypatch, tmp_path
):
    project = tmp_path / "pyproject.toml"
    _write_project(
        project,
        dependencies=("Foo_Bar[Feature_X]>=1.0,<2; python_version >= '3.10'",),
        optional={"Fast_Mode": ("Baz>=2",)},
    )
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    metadata = _metadata_text(
        requires=(
            'foo-bar[feature-x]<2,>=1.0; python_version>="3.10"',
            'baz>=2; extra == "FAST_mode"',
        ),
        extras=("fast_mode",),
    )
    wheel = _write_wheel(tmp_path / "test.whl", metadata)
    checks = _checks(release_check.inspect_wheel(wheel, "1.2.3"))
    assert checks["wheel_dependencies_match_source"].status == "OK"
    assert checks["wheel_extras_match_source"].status == "OK"


def test_wheel_dependency_mismatch_fails_source_gate(monkeypatch, tmp_path):
    project = tmp_path / "pyproject.toml"
    _write_project(project, dependencies=("alpha>=1",))
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    wheel = _write_wheel(
        tmp_path / "test.whl", _metadata_text(requires=("alpha>=2",))
    )
    checks = _checks(release_check.inspect_wheel(wheel, "1.2.3"))
    assert checks["wheel_dependencies_match_source"].status == "FAIL"


def test_wheel_trailing_zero_specifier_versions_compare_semantically(monkeypatch, tmp_path):
    project = tmp_path / "pyproject.toml"
    _write_project(project, dependencies=("alpha>=1.0",))
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    wheel = _write_wheel(
        tmp_path / "test.whl", _metadata_text(requires=("alpha>=1.0.0",))
    )
    checks = _checks(release_check.inspect_wheel(wheel, "1.2.3"))
    assert checks["wheel_dependencies_match_source"].status == "OK"


def test_wheel_changed_marker_fails_source_gate(monkeypatch, tmp_path):
    project = tmp_path / "pyproject.toml"
    _write_project(project, dependencies=("alpha>=1; python_version >= '3.10'",))
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    wheel = _write_wheel(
        tmp_path / "test.whl",
        _metadata_text(requires=("alpha>=1; python_version >= '3.11'",)),
    )
    checks = _checks(release_check.inspect_wheel(wheel, "1.2.3"))
    assert checks["wheel_dependencies_match_source"].status == "FAIL"


def test_wheel_reordered_markers_match_with_optional_extra_gate(monkeypatch, tmp_path):
    project = tmp_path / "pyproject.toml"
    _write_project(
        project,
        optional={
            "Fast_Mode": (
                "Baz>=2; python_version >= '3.10' or sys_platform == 'win32'",
            )
        },
    )
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    wheel = _write_wheel(
        tmp_path / "test.whl",
        _metadata_text(
            requires=(
                "alpha>=1",
                'baz>=2; extra == "FAST_mode" and '
                '(sys_platform == "win32" or python_version >= "3.10")',
            ),
            extras=("fast_mode",),
        ),
    )
    checks = _checks(release_check.inspect_wheel(wheel, "1.2.3"))
    assert checks["wheel_dependencies_match_source"].status == "OK"
    assert checks["wheel_extras_match_source"].status == "OK"


def test_wheel_unexpected_dependency_fails_source_gate(monkeypatch, tmp_path):
    project = tmp_path / "pyproject.toml"
    _write_project(project, dependencies=("alpha>=1",))
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    wheel = _write_wheel(
        tmp_path / "test.whl",
        _metadata_text(requires=("alpha>=1", "surprise>=1")),
    )
    checks = _checks(release_check.inspect_wheel(wheel, "1.2.3"))
    assert checks["wheel_dependencies_match_source"].status == "FAIL"


def test_wheel_missing_extra_fails_source_gate(monkeypatch, tmp_path):
    project = tmp_path / "pyproject.toml"
    _write_project(project, optional={"fast": ("beta>=1",)})
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    wheel = _write_wheel(
        tmp_path / "test.whl",
        _metadata_text(requires=("alpha>=1", 'beta>=1; extra == "fast"')),
    )
    checks = _checks(release_check.inspect_wheel(wheel, "1.2.3"))
    assert checks["wheel_extras_match_source"].status == "FAIL"
    assert checks["wheel_dependencies_match_source"].status == "OK"


def test_wheel_unexpected_extra_fails_source_gate(monkeypatch, tmp_path):
    project = tmp_path / "pyproject.toml"
    _write_project(project)
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    wheel = _write_wheel(
        tmp_path / "test.whl", _metadata_text(extras=("other",))
    )
    checks = _checks(release_check.inspect_wheel(wheel, "1.2.3"))
    assert checks["wheel_extras_match_source"].status == "FAIL"
    assert checks["wheel_dependencies_match_source"].status == "OK"


def test_sdist_metadata_matches_source_dependencies_and_extras(monkeypatch, tmp_path):
    project = tmp_path / "pyproject.toml"
    _write_project(project, optional={"fast": ("beta>=1",)})
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    metadata = _metadata_text(
        requires=('alpha>=1', 'beta>=1; extra == "fast"'), extras=("fast",)
    )
    sdist = _write_sdist(tmp_path / "test.tar.gz", (metadata.encode(),))
    checks = _checks(release_check.inspect_sdist(sdist))
    assert checks["sdist_dependencies_match_source"].status == "OK"
    assert checks["sdist_extras_match_source"].status == "OK"
    assert checks["sdist_metadata_version"].status == "OK"


def test_sdist_missing_extra_fails_source_gate(monkeypatch, tmp_path):
    project = tmp_path / "pyproject.toml"
    _write_project(project, optional={"fast": ("beta>=1",)})
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    metadata = _metadata_text(
        requires=("alpha>=1", 'beta>=1; extra == "fast"')
    )
    sdist = _write_sdist(tmp_path / "test.tar.gz", (metadata.encode(),))
    checks = _checks(release_check.inspect_sdist(sdist))
    assert checks["sdist_dependencies_match_source"].status == "OK"
    assert checks["sdist_extras_match_source"].status == "FAIL"


def test_sdist_unexpected_extra_fails_source_gate(monkeypatch, tmp_path):
    project = tmp_path / "pyproject.toml"
    _write_project(project)
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    metadata = _metadata_text(extras=("other",))
    sdist = _write_sdist(tmp_path / "test.tar.gz", (metadata.encode(),))
    checks = _checks(release_check.inspect_sdist(sdist))
    assert checks["sdist_extras_match_source"].status == "FAIL"
    assert checks["sdist_dependencies_match_source"].status == "OK"


def test_sdist_unexpected_dependency_fails_source_gate(monkeypatch, tmp_path):
    project = tmp_path / "pyproject.toml"
    _write_project(project, dependencies=("alpha>=1",))
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    metadata = _metadata_text(requires=("alpha>=1", "surprise>=1"))
    sdist = _write_sdist(tmp_path / "test.tar.gz", (metadata.encode(),))
    checks = _checks(release_check.inspect_sdist(sdist))
    assert checks["sdist_dependencies_match_source"].status == "FAIL"
    assert checks["sdist_extras_match_source"].status == "OK"


def test_sdist_without_pkg_info_fails_metadata_gates(monkeypatch, tmp_path):
    project = tmp_path / "pyproject.toml"
    _write_project(project)
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    sdist = _write_sdist(tmp_path / "test.tar.gz", ())
    checks = _checks(release_check.inspect_sdist(sdist))
    assert checks["sdist_metadata_copies_consistent"].status == "FAIL"
    assert checks["sdist_dependencies_match_source"].status == "FAIL"
    assert checks["sdist_extras_match_source"].status == "FAIL"


def test_malformed_sdist_and_wheel_metadata_fail_gates(monkeypatch, tmp_path):
    project = tmp_path / "pyproject.toml"
    _write_project(project)
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    sdist = _write_sdist(tmp_path / "test.tar.gz", (b"not valid core metadata\n\n",))
    checks = _checks(release_check.inspect_sdist(sdist))
    assert checks["sdist_metadata_copies_consistent"].status == "FAIL"
    assert checks["sdist_dependencies_match_source"].status == "FAIL"

    for index, metadata in enumerate((None, "not valid core metadata\n\n")):
        wheel = tmp_path / f"bad-{index}.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            if metadata is not None:
                archive.writestr("grandgousier-1.2.3.dist-info/METADATA", metadata)
        wheel_checks = _checks(release_check.inspect_wheel(wheel, "1.2.3"))
        assert wheel_checks["wheel_metadata"].status == "FAIL"
        assert wheel_checks["wheel_dependencies_match_source"].status == "FAIL"


def test_two_equivalent_sdist_pkg_info_copies_are_allowed(monkeypatch, tmp_path):
    project = tmp_path / "pyproject.toml"
    _write_project(project, dependencies=("alpha>=1,<2",))
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    first = _metadata_text(requires=("alpha>=1,<2",)).encode()
    second = (
        "Version: 1.2.3\n"
        "Requires-Python: >=3.11\n"
        "Name: grandgousier\n"
        "Metadata-Version: 2.4\n"
        "Requires-Dist: alpha<2,>=1\n\n"
    ).encode()
    sdist = _write_sdist(tmp_path / "test.tar.gz", (first, second))
    checks = _checks(release_check.inspect_sdist(sdist))
    assert checks["sdist_metadata_copies_consistent"].status == "OK"
    assert checks["sdist_dependencies_match_source"].status == "OK"


def test_inconsistent_sdist_pkg_info_copies_fail_consistency(monkeypatch, tmp_path):
    project = tmp_path / "pyproject.toml"
    _write_project(project)
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    first = _metadata_text().encode()
    second = _metadata_text(requires=("alpha>=2",)).encode()
    sdist = _write_sdist(tmp_path / "test.tar.gz", (first, second))
    checks = _checks(release_check.inspect_sdist(sdist))
    assert checks["sdist_metadata_copies_consistent"].status == "FAIL"


def test_one_sdist_pkg_info_copy_missing_dependencies_fails_gates(monkeypatch, tmp_path):
    project = tmp_path / "pyproject.toml"
    _write_project(project, dependencies=("alpha>=1",))
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    first = _metadata_text(requires=("alpha>=1",)).encode()
    second = _metadata_text(requires=()).encode()
    sdist = _write_sdist(tmp_path / "test.tar.gz", (first, second))
    checks = _checks(release_check.inspect_sdist(sdist))
    assert checks["sdist_metadata_copies_consistent"].status == "FAIL"
    assert checks["sdist_dependencies_match_source"].status == "FAIL"


def test_wheel_sdist_dependency_mismatch_fails_cross_artifact_gate(
    monkeypatch, tmp_path
):
    project = tmp_path / "pyproject.toml"
    _write_project(project)
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    wheel = _write_wheel(tmp_path / "test.whl", _metadata_text())
    sdist = _write_sdist(
        tmp_path / "test.tar.gz", (_metadata_text(requires=("alpha>=2",)).encode(),)
    )
    checks = _checks(release_check.compare_wheel_sdist_metadata(wheel, sdist))
    assert checks["wheel_sdist_dependencies_match"].status == "FAIL"
    assert checks["wheel_sdist_extras_match"].status == "OK"


def test_wheel_sdist_extra_mismatch_fails_cross_artifact_gate(
    monkeypatch, tmp_path
):
    project = tmp_path / "pyproject.toml"
    _write_project(project, optional={"fast": ("beta>=1",)})
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    wheel_metadata = _metadata_text(
        requires=('alpha>=1', 'beta>=1; extra == "fast"'), extras=("fast",)
    )
    sdist_metadata = _metadata_text(
        requires=('alpha>=1', 'beta>=1; extra == "fast"'), extras=("other",)
    )
    wheel = _write_wheel(tmp_path / "test.whl", wheel_metadata)
    sdist = _write_sdist(tmp_path / "test.tar.gz", (sdist_metadata.encode(),))
    checks = _checks(release_check.compare_wheel_sdist_metadata(wheel, sdist))
    assert checks["wheel_sdist_extras_match"].status == "FAIL"


def test_metadata_distribution_version_mismatch_fails_wheel_and_sdist_checks(
    monkeypatch, tmp_path
):
    project = tmp_path / "pyproject.toml"
    _write_project(project)
    monkeypatch.setattr(release_check, "PYPROJECT", project)
    wrong = _metadata_text(version="1.2.4")
    wheel = _write_wheel(tmp_path / "test.whl", wrong)
    sdist = _write_sdist(tmp_path / "test.tar.gz", (wrong.encode(),))
    wheel_checks = _checks(release_check.inspect_wheel(wheel, "1.2.3"))
    sdist_checks = _checks(release_check.inspect_sdist(sdist))
    assert wheel_checks["wheel_metadata"].status == "FAIL"
    assert sdist_checks["sdist_metadata_version"].status == "FAIL"
